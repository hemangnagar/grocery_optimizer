"""LLM entity-resolution adjudicator (agentic layer #2).

RapidFuzz resolves the easy ~80% deterministically; the ambiguous remainder
sits in ``resolution_queue``. This adjudicator asks Claude, per queued pair,
whether the source product and the candidate canonical are the SAME purchasable
item for price comparison. A same-verdict at/above the gold trust threshold
links the product (so it flows to gold); everything else is rejected and stays
out of gold. The LLM only returns a verdict — the deterministic pipeline applies
it. It never writes to gold directly and never invents a price.

The decider is dependency-injected so tests run without network access.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Callable

import duckdb

from .. import config

# Default to the most capable model; override to Haiku for this high-volume,
# simple judgment to cut cost (~5x). Structured outputs are supported on both.
DEFAULT_MODEL = os.environ.get("GROCERY_ADJUDICATOR_MODEL", "claude-opus-4-8")

# Link only at/above the gold trust gate (0.85), so approved matches appear in
# gold rather than being linked-but-hidden.
LINK_THRESHOLD = 0.85

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "same_product": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["same_product", "confidence", "reason"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You adjudicate grocery product entity resolution for a price-comparison "
    "tool. Given a SOURCE product listing and a CANDIDATE canonical product, "
    "decide whether they are the SAME purchasable item for the purpose of "
    "comparing prices across stores.\n\n"
    "Rules:\n"
    "- Same food/product type with matching key attributes = SAME, even if the "
    "brands differ (a shopper treats them as substitutes). Attributes that must "
    "match: variety/flavor (e.g. whole vs 2% vs skim milk), organic vs "
    "conventional, and rough size/form (a gallon is not a pint).\n"
    "- Different product type, different variety, or clearly different size/form "
    "= DIFFERENT.\n"
    "- A prepared or derivative product is DIFFERENT from the base ingredient "
    "(banana bread is not bananas; rice pudding is not rice).\n"
    "Return same_product (bool), confidence (0-1), and a one-line reason."
)


class AdjudicatorError(RuntimeError):
    pass


def make_claude_decider(model: str = DEFAULT_MODEL, api_key: str | None = None) -> Callable:
    """Return a decide(source, candidate) -> verdict dict backed by Claude."""
    import anthropic  # imported lazily so no-network tests don't require it

    key = api_key or config.get_anthropic_api_key()
    if not key:
        raise AdjudicatorError(
            "ANTHROPIC_API_KEY is not set in the project .env "
            "(never set it globally — see CLAUDE.md)."
        )
    client = anthropic.Anthropic(api_key=key)

    def decide(source: dict, candidate: dict) -> dict:
        user = (
            f'SOURCE (store: {source.get("source")}): '
            f'"{source.get("raw_name")}"'
            + (f' | size: {source["raw_size_text"]}' if source.get("raw_size_text") else "")
            + (f' | brand: {source["raw_brand"]}' if source.get("raw_brand") else "")
            + f'\nCANDIDATE canonical: "{candidate.get("name")}"'
            + (f' | category: {candidate["category"]}' if candidate.get("category") else "")
        )
        resp = client.messages.create(
            model=model,
            max_tokens=512,
            system=_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _VERDICT_SCHEMA}},
            messages=[{"role": "user", "content": user}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "{}")
        data = json.loads(text)
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        return {
            "same_product": bool(data.get("same_product")),
            "confidence": confidence,
            "reason": str(data.get("reason", ""))[:500],
        }

    return decide


def _open_queue(con) -> list[dict]:
    # Prioritize pairs whose confirmation would ADD A NEW SOURCE to the candidate
    # canonical (i.e. create a cross-store comparison) — that's where adjudication
    # actually lifts the optimizer. Within that, highest fuzzy confidence first.
    rows = con.execute(
        """
        WITH cand_sources AS (
            SELECT canonical_id, source
            FROM source_products
            WHERE canonical_id IS NOT NULL
            GROUP BY canonical_id, source
        )
        SELECT rq.queue_id, rq.source_product_id, rq.candidate_canonical_id,
               rq.proposed_confidence,
               sp.source, sp.raw_name, sp.raw_size_text, sp.raw_brand,
               cp.name AS candidate_name, cp.category AS candidate_category,
               CASE WHEN NOT EXISTS (
                   SELECT 1 FROM cand_sources cs
                   WHERE cs.canonical_id = rq.candidate_canonical_id
                     AND cs.source = sp.source
               ) THEN 1 ELSE 0 END AS adds_source
        FROM resolution_queue rq
        JOIN source_products sp ON sp.source_product_id = rq.source_product_id
        JOIN canonical_products cp ON cp.canonical_id = rq.candidate_canonical_id
        WHERE rq.status = 'open'
          -- hard category guard: cross-category pairs are wrong by definition,
          -- never worth an LLM call (pairs queued before the taxonomy existed)
          AND (sp.coarse_category IS NULL OR cp.coarse_category IS NULL
               OR sp.coarse_category = cp.coarse_category)
        ORDER BY adds_source DESC, rq.proposed_confidence DESC, rq.queue_id
        """
    ).fetchall()
    cols = [
        "queue_id", "source_product_id", "candidate_canonical_id",
        "proposed_confidence", "source", "raw_name", "raw_size_text",
        "raw_brand", "candidate_name", "candidate_category", "adds_source",
    ]
    return [dict(zip(cols, r)) for r in rows]


def adjudicate_queue(
    con: duckdb.DuckDBPyConnection,
    decide: Callable,
    *,
    limit: int | None = None,
    link_threshold: float = LINK_THRESHOLD,
) -> dict:
    """Adjudicate open queue rows. Returns counts by outcome."""
    now = datetime.now(timezone.utc)
    rows = _open_queue(con)
    if limit:
        rows = rows[:limit]

    stats = {"processed": 0, "linked": 0, "rejected": 0, "errors": 0}
    for row in rows:
        source = {
            "source": row["source"],
            "raw_name": row["raw_name"],
            "raw_size_text": row["raw_size_text"],
            "raw_brand": row["raw_brand"],
        }
        candidate = {"name": row["candidate_name"], "category": row["candidate_category"]}
        try:
            verdict = decide(source, candidate)
        except Exception as exc:  # keep going; one bad call shouldn't abort the run
            stats["errors"] += 1
            stats["processed"] += 1
            _ = exc
            continue

        stats["processed"] += 1
        if verdict["same_product"] and verdict["confidence"] >= link_threshold:
            con.execute(
                """
                UPDATE source_products
                SET canonical_id = ?, match_confidence = ?, match_method = 'llm_adjudicator',
                    resolved_at = ?
                WHERE source_product_id = ?
                """,
                [row["candidate_canonical_id"], verdict["confidence"], now, row["source_product_id"]],
            )
            con.execute(
                """
                UPDATE resolution_queue
                SET status = 'approved', proposed_confidence = ?, reason = ?,
                    resolved_at = ?, resolved_by = 'llm_adjudicator'
                WHERE queue_id = ?
                """,
                [verdict["confidence"], verdict["reason"], now, row["queue_id"]],
            )
            stats["linked"] += 1
        else:
            con.execute(
                """
                UPDATE resolution_queue
                SET status = 'rejected', reason = ?, resolved_at = ?,
                    resolved_by = 'llm_adjudicator'
                WHERE queue_id = ?
                """,
                [verdict["reason"], now, row["queue_id"]],
            )
            stats["rejected"] += 1
    return stats

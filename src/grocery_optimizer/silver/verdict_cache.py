"""Verdict cache + LLM list-term adjudicator (v2 build step 4, "the library").

Adjudicates whether a canonical product satisfies a shopping-list term
("chicken" -> Boneless Chicken Breast: match; -> Chicken Breast Bites:
no_match). Three rules, per CLAUDE.md:

1. The cache is consulted BEFORE any LLM call — each (normalized_list_term,
   canonical_id) pair costs one LLM call EVER. Verdicts accumulate into a
   proprietary regional matching library.
2. Selection (basket matching -> gold) reads ONLY cached verdicts, never live
   LLM output, so every run replays deterministically with a warm cache.
3. Below-confidence verdicts land in a human-review queue: they sit in the
   cache but are ignored by selection until a human approves (or rejects,
   which flips the verdict at confidence 1.0).

The category guard (v2 step 3) outranks the cache: even a confident cached
match is ignored if it would cross coarse categories. The LLM never overrides
the deterministic layer.

Distinct from silver.adjudicate (the step-8 entity-resolution adjudicator,
which judges source_product <-> canonical pairs from resolution_queue).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Callable

import duckdb

from .. import config
from .normalize import normalize_name as normalize_term

# Same trust gate as gold: a verdict below this waits for a human.
CONFIDENT = 0.85

DEFAULT_MODEL = os.environ.get("GROCERY_ADJUDICATOR_MODEL", "claude-opus-4-8")

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["match", "no_match"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reason"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You adjudicate grocery shopping-list matching for a price-comparison "
    "tool. Given a LIST TERM a shopper wrote (generic, e.g. 'chicken', "
    "'bread') and a CANDIDATE store product, decide whether the candidate is "
    "what a typical shopper writing that term intends to buy.\n\n"
    "Rules:\n"
    "- match: the base item in its ordinary purchasable form, any brand "
    "(list term 'chicken' -> raw chicken breast/thighs/drumsticks; 'bread' -> "
    "a loaf of sandwich bread).\n"
    "- no_match: prepared, derivative, flavored, snack-form, or accessory "
    "products ('chicken' -> chicken crackers, chicken broth, chicken-flavored "
    "ramen; 'eggs' -> marshmallow eggs; 'rice' -> rice pudding).\n"
    "- no_match: a different item that merely shares words with the term.\n"
    "- Package size does not matter unless absurd for a household.\n"
    "Return verdict ('match'|'no_match'), confidence (0-1), one-line reason."
)


class VerdictCacheError(RuntimeError):
    pass


# ---- cache primitives ------------------------------------------------------

def get_verdict(
    con: duckdb.DuckDBPyConnection, norm_term: str, canonical_id: int
) -> dict | None:
    row = con.execute(
        """
        SELECT verdict_id, verdict, confidence, reason, decided_by
        FROM match_verdicts
        WHERE normalized_list_term = ? AND canonical_id = ?
        """,
        [norm_term, canonical_id],
    ).fetchone()
    if row is None:
        return None
    cols = ["verdict_id", "verdict", "confidence", "reason", "decided_by"]
    return dict(zip(cols, row))


def store_verdict(
    con: duckdb.DuckDBPyConnection,
    norm_term: str,
    canonical_id: int,
    verdict: str,
    confidence: float,
    reason: str,
    *,
    model: str | None = None,
    decided_by: str = "llm",
) -> int:
    if verdict not in ("match", "no_match"):
        raise VerdictCacheError(f"invalid verdict {verdict!r}")
    now = datetime.now(timezone.utc)
    return con.execute(
        """
        INSERT INTO match_verdicts (
            normalized_list_term, canonical_id, verdict, confidence, reason,
            model, decided_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (normalized_list_term, canonical_id) DO UPDATE SET
            verdict = excluded.verdict,
            confidence = excluded.confidence,
            reason = excluded.reason,
            model = excluded.model,
            decided_by = excluded.decided_by,
            updated_at = excluded.created_at
        RETURNING verdict_id
        """,
        [norm_term, canonical_id, verdict, confidence, reason, model, decided_by, now],
    ).fetchone()[0]


def confident_matches(
    con: duckdb.DuckDBPyConnection,
    norm_term: str,
    coarse: str | None = None,
    *,
    min_confidence: float = CONFIDENT,
) -> list[tuple[int, str]]:
    """Cached confident 'match' canonicals for a term that have a gold price,
    best first. The category guard outranks the cache: a cached match in a
    different known coarse category is never returned."""
    rows = con.execute(
        """
        SELECT mv.canonical_id, any_value(cp.name) AS name,
               max(mv.confidence) AS conf, min(g.price) AS mp
        FROM match_verdicts mv
        JOIN canonical_products cp ON cp.canonical_id = mv.canonical_id
        JOIN gold_current_prices g ON g.canonical_id = mv.canonical_id
        WHERE mv.normalized_list_term = ?
          AND mv.verdict = 'match' AND mv.confidence >= ?
          AND (? IS NULL OR cp.coarse_category IS NULL OR cp.coarse_category = ?)
        GROUP BY mv.canonical_id
        ORDER BY conf DESC, mp ASC, mv.canonical_id
        """,
        [norm_term, min_confidence, coarse, coarse],
    ).fetchall()
    return [(cid, name) for cid, name, _conf, _mp in rows]


# ---- human review ----------------------------------------------------------

def enqueue_review(con: duckdb.DuckDBPyConnection, verdict_id: int) -> None:
    open_row = con.execute(
        "SELECT 1 FROM match_review_queue WHERE verdict_id = ? AND status = 'open'",
        [verdict_id],
    ).fetchone()
    if open_row is None:
        con.execute(
            "INSERT INTO match_review_queue (verdict_id) VALUES (?)", [verdict_id]
        )


def review_verdict(
    con: duckdb.DuckDBPyConnection,
    verdict_id: int,
    approve: bool,
    *,
    resolved_by: str = "human",
) -> None:
    """Resolve a queued verdict: approve keeps it, reject flips it — either
    way it becomes a human verdict at confidence 1.0 (and so usable)."""
    row = con.execute(
        "SELECT verdict FROM match_verdicts WHERE verdict_id = ?", [verdict_id]
    ).fetchone()
    if row is None:
        raise VerdictCacheError(f"no verdict {verdict_id}")
    verdict = row[0] if approve else ("no_match" if row[0] == "match" else "match")
    now = datetime.now(timezone.utc)
    con.execute(
        """
        UPDATE match_verdicts
        SET verdict = ?, confidence = 1.0, decided_by = 'human', model = NULL,
            updated_at = ?
        WHERE verdict_id = ?
        """,
        [verdict, now, verdict_id],
    )
    con.execute(
        """
        UPDATE match_review_queue
        SET status = ?, resolved_at = ?, resolved_by = ?
        WHERE verdict_id = ? AND status = 'open'
        """,
        ["approved" if approve else "rejected", now, resolved_by, verdict_id],
    )


# ---- LLM decider -----------------------------------------------------------

def make_claude_decider(model: str = DEFAULT_MODEL, api_key: str | None = None) -> Callable:
    """Return decide(term, candidate) -> {verdict, confidence, reason}."""
    import anthropic  # lazy so no-network tests don't require it

    key = api_key or config.get_anthropic_api_key()
    if not key:
        raise VerdictCacheError(
            "ANTHROPIC_API_KEY is not set in the project .env "
            "(never set it globally — see CLAUDE.md)."
        )
    client = anthropic.Anthropic(api_key=key)

    def decide(term: dict, candidate: dict) -> dict:
        user = (
            f'LIST TERM: "{term["keyword"]}"'
            + (f' (list category: {term["category"]})' if term.get("category") else "")
            + f'\nCANDIDATE product: "{candidate["name"]}"'
            + (f' | category: {candidate["coarse_category"]}' if candidate.get("coarse_category") else "")
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
        return {
            "verdict": data.get("verdict", "no_match"),
            "confidence": max(0.0, min(1.0, float(data.get("confidence", 0.0)))),
            "reason": str(data.get("reason", ""))[:500],
        }

    return decide


# ---- adjudication loop -----------------------------------------------------

def adjudicate_pairs(
    con: duckdb.DuckDBPyConnection,
    decide: Callable,
    pairs: list[dict],
    *,
    model: str | None = None,
    review_threshold: float = CONFIDENT,
) -> dict:
    """Adjudicate (term, candidate) pairs, cache-first.

    Each pair dict: {keyword, category, candidate: {canonical_id, name,
    coarse_category}}. Cached pairs are skipped (that's the whole point);
    fresh verdicts below the threshold are queued for human review.
    """
    stats = {
        "pairs": 0, "cache_hits": 0, "llm_calls": 0,
        "matches": 0, "no_matches": 0, "queued_for_review": 0, "errors": 0,
    }
    for pair in pairs:
        stats["pairs"] += 1
        norm = normalize_term(pair["keyword"])
        cand = pair["candidate"]
        if get_verdict(con, norm, cand["canonical_id"]) is not None:
            stats["cache_hits"] += 1
            continue
        try:
            verdict = decide(
                {"keyword": pair["keyword"], "category": pair.get("category")}, cand
            )
        except Exception:  # one bad call shouldn't abort the run
            stats["errors"] += 1
            continue
        stats["llm_calls"] += 1
        vid = store_verdict(
            con, norm, cand["canonical_id"], verdict["verdict"],
            verdict["confidence"], verdict["reason"], model=model,
        )
        stats["matches" if verdict["verdict"] == "match" else "no_matches"] += 1
        if verdict["confidence"] < review_threshold:
            enqueue_review(con, vid)
            stats["queued_for_review"] += 1
    return stats

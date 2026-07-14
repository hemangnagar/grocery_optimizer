"""RapidFuzz entity resolution: source products -> canonical products.

Deterministic first pass (per the spec, RapidFuzz handles the easy ~80%):

  - score >= AUTO_THRESHOLD           -> auto-link to the best canonical
  - REVIEW_LOW <= score < AUTO        -> park in resolution_queue (ambiguous),
                                         leave unlinked (never silently to gold)
  - score < REVIEW_LOW                -> treat as a new distinct product and
                                         create a canonical for it

The below-AUTO band is where the LLM adjudicator agent will plug in later; for
now those rows wait in the human-review queue.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import duckdb
from rapidfuzz import fuzz

from .normalize import normalize_name

AUTO_THRESHOLD = 90
REVIEW_LOW = 78

_TRADEMARK = re.compile(r"[®™©]")


def _display_name(raw: str | None) -> str:
    return _TRADEMARK.sub("", raw or "").strip()


def _create_canonical(con, raw_name: str, category: str | None) -> int:
    return con.execute(
        """
        INSERT INTO canonical_products (name, category)
        VALUES (?, ?)
        RETURNING canonical_id
        """,
        [_display_name(raw_name), category],
    ).fetchone()[0]


def _link(con, spid: int, canonical_id: int, confidence: float, method: str, now) -> None:
    con.execute(
        """
        UPDATE source_products
        SET canonical_id = ?, match_confidence = ?, match_method = ?, resolved_at = ?
        WHERE source_product_id = ?
        """,
        [canonical_id, confidence, method, now, spid],
    )


def _enqueue(con, spid: int, candidate_id: int, confidence: float, reason: str, now) -> None:
    con.execute(
        """
        INSERT INTO resolution_queue (
            source_product_id, candidate_canonical_id, proposed_confidence,
            reason, status, created_at
        ) VALUES (?, ?, ?, ?, 'open', ?)
        """,
        [spid, candidate_id, confidence, reason, now],
    )


def resolve_products(
    con: duckdb.DuckDBPyConnection,
    *,
    auto: int = AUTO_THRESHOLD,
    review_low: int = REVIEW_LOW,
) -> dict:
    """Resolve all unlinked source_products. Returns counts by outcome."""
    now = datetime.now(timezone.utc)
    unresolved = con.execute(
        """
        SELECT source_product_id, raw_name, category_hint
        FROM source_products
        WHERE canonical_id IS NULL
        ORDER BY source_product_id
        """
    ).fetchall()

    stats = {"linked": 0, "queued": 0, "created": 0}
    for spid, raw_name, category in unresolved:
        norm = normalize_name(raw_name)
        if not norm:
            continue

        # Candidate canonicals (recomputed each iteration so items created
        # earlier in this pass are matchable). Filter by category when known.
        candidates = con.execute(
            "SELECT canonical_id, name, category FROM canonical_products"
        ).fetchall()
        best_id, best_name, best_score = None, None, -1
        for cid, cname, ccat in candidates:
            if category and ccat and category.lower() != ccat.lower():
                continue
            score = fuzz.token_set_ratio(norm, normalize_name(cname))
            if score > best_score:
                best_id, best_name, best_score = cid, cname, score

        if best_id is not None and best_score >= auto:
            _link(con, spid, best_id, round(best_score / 100, 3), "rapidfuzz", now)
            stats["linked"] += 1
        elif best_id is not None and best_score >= review_low:
            _enqueue(
                con, spid, best_id, round(best_score / 100, 3),
                f"fuzzy {best_score} vs '{best_name}'", now,
            )
            stats["queued"] += 1
        else:
            method = "seed" if not candidates else "new"
            cid = _create_canonical(con, raw_name, category)
            _link(con, spid, cid, 1.0, method, now)
            stats["created"] += 1
    return stats

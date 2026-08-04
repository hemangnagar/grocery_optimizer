"""Entry point: LLM-adjudicate shopping-list terms against gold candidates.

Fills the verdict cache (v2 step 4): for each shopping-list term, the top gold
candidates that have no cached verdict yet go to the LLM once; verdicts are
stored in ``match_verdicts`` and below-confidence ones queued for human review.
Re-running is free — cached pairs are skipped, so a warm cache costs zero
tokens and basket matching replays deterministically. Usage::

    uv run grocery-adjudicate-list [--limit N] [--dry-run]

Spends API credits on cache misses (set GROCERY_ADJUDICATOR_MODEL to a cheaper
model to cut cost). --dry-run lists the pairs that WOULD be sent, for free.
"""

from __future__ import annotations

import sys

from ..db import get_connection, init_db
from ..silver.basket import _LIST_TO_COARSE, SHOPPING_LIST, candidates_for_term
from ..silver.verdict_cache import (
    DEFAULT_MODEL,
    adjudicate_pairs,
    get_verdict,
    make_claude_decider,
    normalize_term,
)

CANDIDATES_PER_TERM = 5


def build_pairs(con, *, per_term: int = CANDIDATES_PER_TERM) -> list[dict]:
    """(term, candidate) pairs for the synthetic shopping list. Candidates are
    fetched WITHOUT the cache's no_match exclusion so the pair set is stable
    across runs — cached pairs then skip inside adjudicate_pairs."""
    pairs = []
    for category, _label, keyword, _qty in SHOPPING_LIST:
        coarse = _LIST_TO_COARSE.get(category)
        for cand in candidates_for_term(
            con, keyword, coarse, limit=per_term, exclude_no_match=False
        ):
            pairs.append({"keyword": keyword, "category": category, "candidate": cand})
    return pairs


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    dry_run = "--dry-run" in sys.argv

    con = get_connection()
    try:
        init_db(con)
        pairs = build_pairs(con)
        fresh = [
            p for p in pairs
            if get_verdict(
                con, normalize_term(p["keyword"]), p["candidate"]["canonical_id"]
            ) is None
        ]
        print(
            f"{len(pairs)} (term, candidate) pairs; "
            f"{len(pairs) - len(fresh)} cached (free), {len(fresh)} would need the LLM."
        )
        if dry_run:
            for p in fresh[: limit or len(fresh)]:
                print(f'  "{p["keyword"]}" vs "{p["candidate"]["name"]}"')
            return
        if not fresh:
            print("Cache is warm — nothing to adjudicate, zero tokens spent.")
            return

        if limit:
            pairs = pairs[:0] + fresh[:limit]
        stats = adjudicate_pairs(con, make_claude_decider(), pairs, model=DEFAULT_MODEL)
        print(
            f"Adjudicated: {stats['llm_calls']} LLM call(s) "
            f"({stats['cache_hits']} cache hits) -> {stats['matches']} match, "
            f"{stats['no_matches']} no_match; {stats['queued_for_review']} queued "
            f"for human review; {stats['errors']} error(s)."
        )
        open_reviews = con.execute(
            "SELECT count(*) FROM match_review_queue WHERE status = 'open'"
        ).fetchone()[0]
        print(f"Open review queue: {open_reviews}. Re-run grocery-optimize to use the library.")
    finally:
        con.close()


if __name__ == "__main__":
    main()

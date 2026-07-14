"""Run the LLM entity-resolution adjudicator over the review queue.

Consumes Anthropic API credits. Bounded by --limit (default 40) to cap cost;
pass --limit 0 to process the whole queue. Set GROCERY_ADJUDICATOR_MODEL to a
cheaper model (e.g. claude-haiku-4-5) for this high-volume simple judgment.

Usage::

    uv run grocery-adjudicate            # 40 highest-confidence queued pairs
    uv run grocery-adjudicate --limit 0  # entire queue
"""

from __future__ import annotations

import sys

from ..config import get_anthropic_api_key
from ..db import get_connection
from ..silver.adjudicate import DEFAULT_MODEL, adjudicate_queue, make_claude_decider

DEFAULT_LIMIT = 40


def _parse_limit(argv: list[str]) -> int:
    if "--limit" in argv:
        try:
            return int(argv[argv.index("--limit") + 1])
        except (IndexError, ValueError):
            pass
    return DEFAULT_LIMIT


def _multi_source_count(con) -> int:
    return con.execute(
        """
        SELECT count(*) FROM (
            SELECT canonical_id FROM gold_current_prices
            GROUP BY canonical_id HAVING count(DISTINCT source) >= 2
        )
        """
    ).fetchone()[0]


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if not get_anthropic_api_key():
        print("ANTHROPIC_API_KEY is not set - cannot run the adjudicator.\n")
        print("Add it to the project .env (NEVER set it globally - see CLAUDE.md):")
        print("  ANTHROPIC_API_KEY=sk-ant-...")
        print("Then re-run:  uv run grocery-adjudicate")
        return

    limit = _parse_limit(sys.argv)
    con = get_connection()
    try:
        open_n = con.execute(
            "SELECT count(*) FROM resolution_queue WHERE status = 'open'"
        ).fetchone()[0]
        if open_n == 0:
            print("Review queue is empty - nothing to adjudicate.")
            return

        before = _multi_source_count(con)
        scope = "entire queue" if limit == 0 else f"{min(limit, open_n)} of {open_n}"
        print(f"Adjudicating {scope} queued pair(s) with {DEFAULT_MODEL} ...")

        decide = make_claude_decider()
        stats = adjudicate_queue(con, decide, limit=(None if limit == 0 else limit))
        after = _multi_source_count(con)

        print(
            f"\nProcessed {stats['processed']}: {stats['linked']} linked (same product), "
            f"{stats['rejected']} rejected, {stats['errors']} errors."
        )
        print(
            f"Cross-store items (priced at 2+ stores): {before} -> {after} "
            f"(+{after - before})."
        )
        remaining = con.execute(
            "SELECT count(*) FROM resolution_queue WHERE status = 'open'"
        ).fetchone()[0]
        print(f"Remaining open queue: {remaining}")
    finally:
        con.close()


if __name__ == "__main__":
    main()

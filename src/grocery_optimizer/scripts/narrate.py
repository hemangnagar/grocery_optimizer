"""Entry point: write (or preview) this week's audited plan narration.

Builds the deterministic fact pack from gold for the latest basket, has Claude
write one paragraph from it, runs the number audit, and stores the result —
the PWA then serves the stored narration with zero further tokens. Usage::

    uv run grocery-narrate [--facts-only]

--facts-only prints the fact pack (free, no API call). A failed audit stores
nothing and exits non-zero: invented numbers never reach users.
"""

from __future__ import annotations

import json
import sys

from ..db import get_connection, init_db
from ..gold.narrator import (
    DEFAULT_MODEL,
    NarrationAuditError,
    build_facts,
    make_claude_writer,
    narrate,
)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    con = get_connection()
    try:
        init_db(con)
        row = con.execute(
            "SELECT basket_id, name FROM baskets ORDER BY basket_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            print("No basket found — run grocery-seed-demo or grocery-optimize first.")
            raise SystemExit(1)
        basket_id, name = row

        if "--facts-only" in sys.argv:
            print(json.dumps(build_facts(con, basket_id), indent=2))
            return

        print(f'Narrating basket {basket_id} ("{name}") with {DEFAULT_MODEL}…')
        try:
            result = narrate(
                con, basket_id, make_claude_writer(), model=DEFAULT_MODEL
            )
        except NarrationAuditError as exc:
            print(f"AUDIT FAILED — narration rejected, nothing stored: {exc}")
            raise SystemExit(2)

        print(f"\n{result['narration']}\n")
        print(f"Sources: {result['citations']}")
        print("Stored; the PWA will now show it (served cached, zero tokens per view).")
    finally:
        con.close()


if __name__ == "__main__":
    main()

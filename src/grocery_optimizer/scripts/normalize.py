"""Entry point: normalize captured bronze into silver, then resolve entities.

Idempotent — only unparsed bronze artifacts are ingested, and only unlinked
source products are resolved. Usage::

    uv run grocery-normalize
"""

from __future__ import annotations

import sys

from ..db import get_connection, init_db
from ..silver.normalize import ingest_bronze
from ..silver.resolve import resolve_products


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    con = get_connection()
    try:
        init_db(con)  # ensure schema + latest gold view definitions

        ing = ingest_bronze(con)
        print(
            f"Ingested {ing['manifests']} bronze artifact(s) -> "
            f"{ing['prices']} price rows; {ing['source_products']} source products total."
        )

        res = resolve_products(con)
        print(
            f"Resolution: {res['linked']} auto-linked, {res['created']} new "
            f"canonicals, {res['queued']} queued for review."
        )
        canon = con.execute("SELECT count(*) FROM canonical_products").fetchone()[0]
        queued = con.execute(
            "SELECT count(*) FROM resolution_queue WHERE status = 'open'"
        ).fetchone()[0]
        print(f"Canonical products: {canon}; open review queue: {queued}.")

        print("\nCheapest source per canonical item (gold):")
        rows = con.execute(
            """
            SELECT canonical_name, source, store_id, price, unit_price, unit
            FROM gold_cheapest_source_per_item
            ORDER BY category NULLS LAST, canonical_name
            LIMIT 25
            """
        ).fetchall()
        if not rows:
            print("  (none — no trusted resolved prices yet)")
        for name, source, store_id, price, up, unit in rows:
            price_s = f"${price:.2f}" if price is not None else "n/a"
            unit_s = f"{up} {unit}" if up is not None else ""
            print(f"  {source:<9} {price_s:>7}  {unit_s:>16}  {name}")
    finally:
        con.close()


if __name__ == "__main__":
    main()

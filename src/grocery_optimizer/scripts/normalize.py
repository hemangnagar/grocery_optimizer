"""Entry point: normalize captured bronze into silver, then resolve entities.

Idempotent — only unparsed bronze artifacts are ingested, and only unlinked
source products are resolved. Usage::

    uv run grocery-normalize
"""

from __future__ import annotations

import sys

from ..db import get_connection, init_db
from ..silver.normalize import ingest_bronze
from ..silver.resolve import reset_resolution, resolve_products
from ..silver.taxonomy import backfill_coarse_categories, enforce_category_guard


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    rebuild = "--rebuild" in sys.argv  # re-resolve all products from scratch

    con = get_connection()
    try:
        init_db(con)  # ensure schema + latest gold view definitions

        if rebuild:
            reset_resolution(con)
            print("Reset resolution; re-resolving all source products.")

        ing = ingest_bronze(con)
        print(
            f"Ingested {ing['manifests']} bronze artifact(s) -> "
            f"{ing['prices']} price rows; {ing['source_products']} source products total."
        )

        # Category guard (v2 step 3): classify rows that predate the taxonomy,
        # then sever any existing fuzzy links that cross coarse categories so
        # they re-resolve within their own category below.
        back = backfill_coarse_categories(con)
        if back["source_products"] or back["canonical_products"]:
            print(
                f"Taxonomy backfill: {back['source_products']} source products, "
                f"{back['canonical_products']} canonicals classified."
            )
        severed = enforce_category_guard(con)
        if severed:
            print(f"Category guard: unlinked {severed} cross-category fuzzy match(es).")

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

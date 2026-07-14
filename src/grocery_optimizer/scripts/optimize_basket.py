"""Generate a synthetic family-of-four basket and report the cheapest split.

Every number is read from the gold views. Usage::

    uv run grocery-optimize
"""

from __future__ import annotations

import sys

from ..db import get_connection, init_db
from ..silver.basket import generate_synthetic_basket
from ..silver.optimize import optimize_basket


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    con = get_connection()
    try:
        init_db(con)
        gen = generate_synthetic_basket(con)
        basket_id = gen["basket_id"]
        print(
            f"Synthetic family-of-four basket #{basket_id}: "
            f"{len(gen['matched'])}/{gen['requested']} items matched to real prices."
        )
        if gen["missing"]:
            print(f"  no in-range price found for: {', '.join(gen['missing'])}")

        opt = optimize_basket(con, basket_id)
        if opt["n_items"] == 0:
            print("\nNo priced items in basket - run the fetchers + grocery-normalize first.")
            return

        print("\n=== Cheapest store split (each item at its lowest-price store) ===")
        current_cat = None
        for ln in opt["lines"]:
            if ln["category"] != current_cat:
                current_cat = ln["category"]
                print(f"  [{current_cat}]")
            price = f"${ln['price']:.2f}"
            line = f"${ln['line_cost']:.2f}"
            print(
                f"    {ln['label']:<14} x{ln['quantity']}  {ln['source']:<10} "
                f"{price:>7} -> {line:>7}  ({ln['canonical_name'][:42]})"
            )

        stores = opt["stores_used"]
        print(f"\n  SPLIT TOTAL: ${opt['split_total']:.2f}  across {len(stores)} store(s): {', '.join(stores)}")

        print("\n=== Single-store option (buy the whole list at one store) ===")
        for st in opt["store_totals"]:
            tag = " (covers all)" if st["items_covered"] == opt["n_items"] else ""
            print(
                f"  {st['source']:<10} covers {st['items_covered']}/{opt['n_items']} "
                f"items = ${st['store_total']:.2f}{tag}"
            )

        print("\n=== Savings ===")
        if opt["savings_vs_single"] is not None:
            bs = opt["best_single_store"]
            print(
                f"  vs cheapest single store ({bs['source']} ${bs['store_total']:.2f}): "
                f"save ${opt['savings_vs_single']:.2f} by splitting."
            )
        else:
            print("  No single in-range store carries the whole basket -> splitting is required.")
        print(
            f"  On the {opt['multi_source_items']} item(s) available at 2+ stores, choosing the "
            f"cheapest saves ${opt['optimizer_value']:.2f}/week vs the priciest option."
        )
    finally:
        con.close()


if __name__ == "__main__":
    main()

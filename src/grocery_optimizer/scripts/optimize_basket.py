"""Generate a synthetic family-of-four basket and report the store verdict.

Primary output is ONE store recommendation for the whole list (build step 2,
v2 pivot); the multi-store split is a footnote, not the headline. Every number
is read from the gold views. Usage::

    uv run grocery-optimize
"""

from __future__ import annotations

import sys

from ..db import get_connection, init_db
from ..silver.basket import generate_synthetic_basket
from ..silver.optimize import optimize_basket
from ..silver.verdict import build_verdict


def _print_mode_verdict(label: str, mode: dict) -> None:
    print(f"\n=== Verdict ({label}) ===")
    stores = mode["stores"]
    if not stores:
        print("  No store has any in-range priced item at this confidence.")
        return

    winner = mode["winner"]
    if winner is None:
        print("  No single in-range store covers the whole list at this confidence.")
    else:
        print(
            f"  WINNER: {winner['source']} wins this week at ${winner['store_total']:.2f} "
            f"(covers all {winner['n_items']} items)"
        )

    for st in stores:
        if winner is not None and st is winner:
            continue
        if st["full_coverage"]:
            print(f"    {st['source']}: would've been ${st['store_total']:.2f}")
        else:
            missing = ", ".join(st["missing_labels"])
            print(
                f"    {st['source']}: ${st['store_total']:.2f} but missing "
                f"{len(st['missing_labels'])} of {st['n_items']} items ({missing})"
            )


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

        verdict = build_verdict(con, basket_id)
        if verdict["n_items"] == 0:
            print("\nNo priced items in basket - run the fetchers + grocery-normalize first.")
            return

        _print_mode_verdict("flexible - store brands OK", verdict["flexible"])
        _print_mode_verdict("exact brands only", verdict["exact"])

        footnote = verdict["split_footnote"]
        print("\n=== Split-savings footnote ===")
        if footnote is None:
            print("  Splitting across stores wouldn't beat the winning store.")
        elif footnote["savings"] is None:
            print(
                f"  No single store covers the whole list - the {len(footnote['stores_used'])}"
                f"-store split (${footnote['split_total']:.2f}) is the only full-coverage option."
            )
        else:
            worth = "" if footnote["worth_it"] else " - probably not worth the extra trips."
            print(
                f"  A {len(footnote['stores_used'])}-store split would save another "
                f"${footnote['savings']:.2f} (${footnote['split_total']:.2f} total){worth}"
            )

        opt = optimize_basket(con, basket_id)
        print("\n=== Detail: cheapest per-item split ===")
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
        print(
            f"\n  On the {opt['multi_source_items']} item(s) available at 2+ stores, choosing "
            f"the cheapest saves ${opt['optimizer_value']:.2f}/week vs the priciest option."
        )
    finally:
        con.close()


if __name__ == "__main__":
    main()

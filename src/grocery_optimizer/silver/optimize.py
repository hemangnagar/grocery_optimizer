"""Basket optimizer: cheapest store split vs single-store, and savings.

Reads only the gold views, so every number is queryable from gold (and via
price_id, traceable to silver + bronze).
"""

from __future__ import annotations

import duckdb


def optimize_basket(con: duckdb.DuckDBPyConnection, basket_id: int) -> dict:
    lines = con.execute(
        """
        SELECT category, label, quantity, canonical_name, source, store_id,
               price, line_cost, price_id
        FROM gold_basket_item_cost
        WHERE basket_id = ?
        ORDER BY category, label
        """,
        [basket_id],
    ).fetchall()
    cols = ["category", "label", "quantity", "canonical_name", "source",
            "store_id", "price", "line_cost", "price_id"]
    line_dicts = [dict(zip(cols, r)) for r in lines]

    split_total = con.execute(
        "SELECT round(sum(line_cost), 2) FROM gold_basket_item_cost WHERE basket_id = ?",
        [basket_id],
    ).fetchone()[0] or 0.0
    n_items = len(line_dicts)
    stores_used = sorted({d["source"] for d in line_dicts})

    store_totals = con.execute(
        """
        SELECT source, items_covered, store_total
        FROM gold_basket_store_totals
        WHERE basket_id = ?
        ORDER BY items_covered DESC, store_total ASC
        """,
        [basket_id],
    ).fetchall()

    # Best single store that covers the WHOLE basket (if any).
    full_coverage = [s for s in store_totals if s[1] == n_items]
    best_single = full_coverage[0] if full_coverage else None
    savings_vs_single = (
        round(best_single[2] - split_total, 2) if best_single else None
    )

    # Optimizer value on items that are actually available at 2+ stores:
    # the price spread the split captures.
    spread = con.execute(
        """
        WITH per AS (
            SELECT bi.canonical_id, bi.quantity,
                   count(DISTINCT g.source) AS ns,
                   min(g.price) AS mn, max(g.price) AS mx
            FROM basket_items bi
            JOIN gold_current_prices g ON g.canonical_id = bi.canonical_id
            WHERE bi.basket_id = ?
            GROUP BY bi.canonical_id, bi.quantity
        )
        SELECT
            count(*) FILTER (WHERE ns >= 2),
            round(coalesce(sum((mx - mn) * quantity) FILTER (WHERE ns >= 2), 0), 2)
        FROM per
        """,
        [basket_id],
    ).fetchone()

    return {
        "basket_id": basket_id,
        "lines": line_dicts,
        "n_items": n_items,
        "split_total": round(split_total, 2),
        "stores_used": stores_used,
        "store_totals": [
            {"source": s, "items_covered": c, "store_total": t}
            for s, c, t in store_totals
        ],
        "best_single_store": (
            {"source": best_single[0], "store_total": best_single[2]}
            if best_single else None
        ),
        "savings_vs_single": savings_vs_single,
        "multi_source_items": spread[0],
        "optimizer_value": spread[1],  # $ saved by choosing cheapest per item
    }

"""Step 6 tests on a controlled in-memory dataset (no live data), so the
optimizer math and the basket item-matching are proven deterministically."""

from __future__ import annotations

import duckdb

from grocery_optimizer.db import init_db
from grocery_optimizer.silver.basket import _match_canonical
from grocery_optimizer.silver.optimize import optimize_basket


def _canonical(con, name, category=None):
    return con.execute(
        "INSERT INTO canonical_products (name, category) VALUES (?, ?) RETURNING canonical_id",
        [name, category],
    ).fetchone()[0]


def _priced(con, source, sku, canonical_id, price):
    """A resolved source product with one current price (trusted)."""
    spid = con.execute(
        """
        INSERT INTO source_products (source, source_sku, raw_name, canonical_id, match_confidence)
        VALUES (?, ?, ?, ?, 0.95) RETURNING source_product_id
        """,
        [source, sku, f"{source} {sku}", canonical_id],
    ).fetchone()[0]
    con.execute(
        """
        INSERT INTO prices (source_product_id, source, observed_at, price, confidence)
        VALUES (?, ?, CURRENT_TIMESTAMP, ?, 0.95)
        """,
        [spid, source, price],
    )


def _fresh():
    con = duckdb.connect(":memory:")
    init_db(con)
    return con


def test_optimizer_math():
    con = _fresh()
    milk = _canonical(con, "Milk")
    eggs = _canonical(con, "Eggs")
    # Milk available at two stores; eggs at one.
    _priced(con, "kroger", "m1", milk, 3.00)
    _priced(con, "wholefoods", "m2", milk, 2.50)  # cheaper
    _priced(con, "kroger", "e1", eggs, 4.00)

    bid = con.execute(
        "INSERT INTO baskets (name) VALUES ('t') RETURNING basket_id"
    ).fetchone()[0]
    con.execute(
        "INSERT INTO basket_items (basket_id, canonical_id, quantity) VALUES (?, ?, 2)",
        [bid, milk],
    )
    con.execute(
        "INSERT INTO basket_items (basket_id, canonical_id, quantity) VALUES (?, ?, 1)",
        [bid, eggs],
    )

    opt = optimize_basket(con, bid)
    assert opt["n_items"] == 2
    # Milk from wholefoods (2.50*2=5.00) + eggs from kroger (4.00) = 9.00
    assert opt["split_total"] == 9.00
    assert opt["stores_used"] == ["kroger", "wholefoods"]

    # Single store covering both is kroger: 3.00*2 + 4.00 = 10.00
    assert opt["best_single_store"]["source"] == "kroger"
    assert opt["best_single_store"]["store_total"] == 10.00
    assert opt["savings_vs_single"] == 1.00  # 10.00 - 9.00

    # Milk is the one multi-source item; spread (3.00-2.50)*2 = 1.00
    assert opt["multi_source_items"] == 1
    assert opt["optimizer_value"] == 1.00


def test_item_matcher_avoids_false_friends():
    con = _fresh()
    apples = _canonical(con, "Envy Apples", "Produce")
    juice = _canonical(con, "Apple & Eve 100% Apple Juice")
    pineapple = _canonical(con, "Pineapple")
    _priced(con, "kroger", "a1", apples, 2.99)
    _priced(con, "kroger", "j1", juice, 3.00)
    _priced(con, "aldi_kcl", "p1", pineapple, 1.79)

    hit = _match_canonical(con, "apple", set())
    assert hit is not None
    # Whole-word + false-friend guard: real apples, not juice, not pineapple.
    assert hit[0] == apples
    assert hit[1] == "Envy Apples"

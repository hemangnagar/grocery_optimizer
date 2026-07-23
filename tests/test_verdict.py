"""Step-2 verdict engine tests on a controlled in-memory dataset: coverage
rule (partial-coverage stores never win), substitution modes (match-confidence
threshold), and the split-savings "worth it" heuristic."""

from __future__ import annotations

import duckdb

from grocery_optimizer.db import init_db
from grocery_optimizer.silver.verdict import build_verdict


def _canonical(con, name, category=None):
    return con.execute(
        "INSERT INTO canonical_products (name, category) VALUES (?, ?) RETURNING canonical_id",
        [name, category],
    ).fetchone()[0]


def _priced(con, source, sku, canonical_id, price, confidence=0.95):
    spid = con.execute(
        """
        INSERT INTO source_products (source, source_sku, raw_name, canonical_id, match_confidence)
        VALUES (?, ?, ?, ?, ?) RETURNING source_product_id
        """,
        [source, sku, f"{source} {sku}", canonical_id, confidence],
    ).fetchone()[0]
    con.execute(
        """
        INSERT INTO prices (source_product_id, source, observed_at, price, confidence)
        VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?)
        """,
        [spid, source, price, confidence],
    )


def _basket(con, items):
    """items: list of (canonical_id, label, quantity)."""
    bid = con.execute(
        "INSERT INTO baskets (name) VALUES ('t') RETURNING basket_id"
    ).fetchone()[0]
    for canonical_id, label, qty in items:
        con.execute(
            "INSERT INTO basket_items (basket_id, canonical_id, label, quantity) VALUES (?, ?, ?, ?)",
            [bid, canonical_id, label, qty],
        )
    return bid


def _fresh():
    con = duckdb.connect(":memory:")
    init_db(con)
    return con


def test_full_coverage_winner_beats_partial_even_when_partial_is_cheaper():
    con = _fresh()
    milk = _canonical(con, "Milk")
    eggs = _canonical(con, "Eggs")

    # wholefoods only carries milk, very cheap - must NOT win despite low total.
    _priced(con, "wholefoods", "m1", milk, 1.00)
    # kroger carries both, pricier per item, but covers the whole list.
    _priced(con, "kroger", "m2", milk, 3.00)
    _priced(con, "kroger", "e1", eggs, 4.00)
    # safeway also covers both but is the priciest full-coverage option.
    _priced(con, "safeway", "m3", milk, 3.50)
    _priced(con, "safeway", "e2", eggs, 4.50)

    bid = _basket(con, [(milk, "milk", 1), (eggs, "eggs", 1)])
    verdict = build_verdict(con, bid)

    winner = verdict["flexible"]["winner"]
    assert winner["source"] == "kroger"
    assert winner["store_total"] == 7.00

    wf = next(s for s in verdict["flexible"]["stores"] if s["source"] == "wholefoods")
    assert wf["full_coverage"] is False
    assert wf["missing_labels"] == ["eggs"]
    assert wf["store_total"] == 1.00  # shown, just flagged - not fabricated


def test_exact_mode_excludes_mid_confidence_match_flexible_includes_it():
    con = _fresh()
    milk = _canonical(con, "Milk")
    eggs = _canonical(con, "Eggs")

    # kroger's milk match is only 0.90 confidence (a fuzzy/store-brand-ish
    # substitute) - flexible (>=0.85) trusts it, exact (>=0.97) does not.
    _priced(con, "kroger", "m1", milk, 3.00, confidence=0.90)
    _priced(con, "kroger", "e1", eggs, 4.00, confidence=1.0)

    bid = _basket(con, [(milk, "milk", 1), (eggs, "eggs", 1)])
    verdict = build_verdict(con, bid)

    assert verdict["flexible"]["winner"]["source"] == "kroger"
    assert verdict["flexible"]["winner"]["store_total"] == 7.00

    # In exact mode, kroger no longer covers milk -> no full-coverage winner.
    assert verdict["exact"]["winner"] is None
    kroger_exact = next(s for s in verdict["exact"]["stores"] if s["source"] == "kroger")
    assert kroger_exact["missing_labels"] == ["milk"]


def test_worth_it_heuristic_flags_small_savings_as_not_worth_it():
    con = _fresh()
    milk = _canonical(con, "Milk")
    eggs = _canonical(con, "Eggs")

    # Single store covering both, at a total only slightly above the split.
    _priced(con, "kroger", "m1", milk, 3.00)
    _priced(con, "kroger", "e1", eggs, 4.00)
    # A cheaper milk elsewhere, small spread -> split saves less than $10 / 8%.
    _priced(con, "wholefoods", "m2", milk, 2.90)

    bid = _basket(con, [(milk, "milk", 1), (eggs, "eggs", 1)])
    verdict = build_verdict(con, bid)

    footnote = verdict["split_footnote"]
    assert footnote is not None
    assert footnote["savings"] == 0.10
    assert footnote["worth_it"] is False


def test_worth_it_heuristic_flags_large_savings_as_worth_it():
    con = _fresh()
    milk = _canonical(con, "Milk")
    eggs = _canonical(con, "Eggs")

    _priced(con, "kroger", "m1", milk, 3.00)
    _priced(con, "kroger", "e1", eggs, 4.00)
    # Much cheaper milk elsewhere -> split saves a lot vs the kroger total.
    _priced(con, "wholefoods", "m2", milk, 0.50)

    bid = _basket(con, [(milk, "milk", 1), (eggs, "eggs", 1)])
    verdict = build_verdict(con, bid)

    footnote = verdict["split_footnote"]
    assert footnote is not None
    assert footnote["savings"] == 2.50
    assert footnote["worth_it"] is True

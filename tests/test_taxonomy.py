"""Tests for v2 step 3: coarse taxonomy + hard cross-category match block."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

from grocery_optimizer.db import init_db
from grocery_optimizer.silver.resolve import resolve_products
from grocery_optimizer.silver.taxonomy import (
    COARSE_CATEGORIES,
    backfill_coarse_categories,
    coarse_category,
    enforce_category_guard,
)


# ---- classifier ------------------------------------------------------------

@pytest.mark.parametrize(
    "hint,name,expected",
    [
        # the founding bug: form beats ingredient
        (None, "Chicken Crackers", "snack"),
        (None, "Chicken Breast", "protein"),
        (None, "Chicken Drumsticks", "protein"),
        (None, "Chicken Broth", "pantry"),
        (None, "Banana Nut Bread", "pantry"),
        (None, "Bananas", "produce"),
        (None, "Rice Pudding", "snack"),
        (None, "Jasmine Rice", "pantry"),
        (None, "Milk Chocolate Bar", "snack"),
        (None, "Whole Milk", "dairy"),
        (None, "Egg Noodles", "pantry"),
        (None, "Large White Eggs", "dairy"),
        (None, "Frozen Broccoli Florets", "frozen"),
        (None, "Broccoli Crowns", "produce"),
        (None, "Vanilla Ice Cream", "frozen"),
        (None, "Lavender Dish Soap", "household"),
        (None, "Lemon Scented Cleaner", "household"),
        # hint takes precedence over name when informative
        ("Snacks", "Chicken & Waffle Chips", "snack"),
        ("Meat & Seafood", "Boneless Skinless Breast", "protein"),
        ("Produce", "Organic Honeycrisp", "produce"),
        ("Dairy", "Oat Beverage", "dairy"),
        ("Frozen", "Cheese Pizza", "frozen"),
        ("Cleaning Products", "Multi-Surface Spray", "household"),
        ("Bakery", "Sourdough Loaf", "pantry"),
        # uninformative hint falls through to the name
        ("Natural & Organic", "Greek Yogurt", "dairy"),
        # unknown stays unknown
        (None, "Gift Card", None),
        (None, None, None),
    ],
)
def test_coarse_category(hint, name, expected):
    assert coarse_category(hint, name) == expected


def test_classifier_only_emits_known_categories():
    for name in ("Chicken", "Milk", "Crackers", "Bananas", "Bleach", "Rice", "Frozen Peas"):
        cat = coarse_category(None, name)
        assert cat in COARSE_CATEGORIES


# ---- hard block in entity resolution ---------------------------------------

@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    init_db(c)
    yield c
    c.close()


def _add_source_product(con, name, source="kroger", hint=None, coarse=None):
    now = datetime.now(timezone.utc)
    return con.execute(
        """
        INSERT INTO source_products (
            source, source_sku, raw_name, category_hint, coarse_category,
            first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        RETURNING source_product_id
        """,
        [source, f"sku-{name}", name, hint, coarse or coarse_category(hint, name), now, now],
    ).fetchone()[0]


def test_resolution_never_crosses_categories(con):
    # "Chicken Crackers" (snack) resolves first and becomes a canonical; the
    # protein "Chicken" must NOT fuzzy-link to it despite the shared token.
    _add_source_product(con, "Chicken Crackers", source="wholefoods")
    _add_source_product(con, "Chicken", hint="Meat & Seafood")
    resolve_products(con)

    rows = con.execute(
        """
        SELECT sp.raw_name, cp.name, cp.coarse_category
        FROM source_products sp JOIN canonical_products cp USING (canonical_id)
        ORDER BY sp.raw_name
        """
    ).fetchall()
    assert ("Chicken", "Chicken", "protein") in rows
    assert ("Chicken Crackers", "Chicken Crackers", "snack") in rows
    # and nothing was parked in the queue — the guard is a hard skip, not a review
    assert con.execute("SELECT count(*) FROM resolution_queue").fetchone()[0] == 0


def test_resolution_still_links_within_category(con):
    _add_source_product(con, "Boneless Chicken Breast", source="wholefoods", hint="Meat")
    _add_source_product(con, "Chicken Breast Boneless", hint="Meat & Seafood")
    stats = resolve_products(con)
    assert stats["linked"] == 1  # token_sort_ratio 100 within the same category
    assert con.execute(
        "SELECT count(DISTINCT canonical_id) FROM source_products"
    ).fetchone()[0] == 1


def test_unknown_category_is_lenient(con):
    # NULL coarse on either side never blocks a match (recall over precision;
    # the adjudicator still reviews ambiguity).
    _add_source_product(con, "Gift Basket Deluxe", coarse=None)
    _add_source_product(con, "Deluxe Gift Basket", source="wholefoods", coarse=None)
    stats = resolve_products(con)
    assert stats["linked"] == 1


def test_new_canonicals_inherit_coarse_category(con):
    _add_source_product(con, "Chicken Drumsticks", hint="Meat & Seafood")
    resolve_products(con)
    assert con.execute(
        "SELECT coarse_category FROM canonical_products WHERE name = 'Chicken Drumsticks'"
    ).fetchone()[0] == "protein"


# ---- backfill + retroactive guard ------------------------------------------

def test_backfill_and_enforce_guard(con):
    now = datetime.now(timezone.utc)
    # Simulate a pre-taxonomy DB: NULL coarse everywhere, and a bad
    # cross-category fuzzy link already in place (chicken -> chicken crackers).
    bad_canonical = con.execute(
        "INSERT INTO canonical_products (name) VALUES ('Chicken Crackers') "
        "RETURNING canonical_id"
    ).fetchone()[0]
    spid = con.execute(
        """
        INSERT INTO source_products (
            source, raw_name, category_hint, canonical_id, match_confidence,
            match_method, first_seen_at, last_seen_at
        ) VALUES ('kroger', 'Chicken', 'Meat & Seafood', ?, 0.9, 'rapidfuzz', ?, ?)
        RETURNING source_product_id
        """,
        [bad_canonical, now, now],
    ).fetchone()[0]

    back = backfill_coarse_categories(con)
    assert back["source_products"] == 1
    assert back["canonical_products"] == 1
    assert con.execute(
        "SELECT coarse_category FROM canonical_products WHERE canonical_id = ?",
        [bad_canonical],
    ).fetchone()[0] == "snack"

    assert enforce_category_guard(con) == 1
    linked = con.execute(
        "SELECT canonical_id, match_method FROM source_products WHERE source_product_id = ?",
        [spid],
    ).fetchone()
    assert linked == (None, None)
    # idempotent: nothing left to sever
    assert enforce_category_guard(con) == 0


def test_enforce_guard_spares_llm_links(con):
    now = datetime.now(timezone.utc)
    cid = con.execute(
        "INSERT INTO canonical_products (name, coarse_category) "
        "VALUES ('Chicken Crackers', 'snack') RETURNING canonical_id"
    ).fetchone()[0]
    con.execute(
        """
        INSERT INTO source_products (
            source, raw_name, coarse_category, canonical_id, match_confidence,
            match_method, first_seen_at, last_seen_at
        ) VALUES ('kroger', 'Chicken', 'protein', ?, 0.95, 'llm_adjudicator', ?, ?)
        """,
        [cid, now, now],
    )
    assert enforce_category_guard(con) == 0  # trusted method, left alone

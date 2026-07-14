"""Tests for step 5 silver: unit normalization, bronze parsing, entity resolution."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from grocery_optimizer.db import init_db
from grocery_optimizer.silver.normalize import (
    normalize_name,
    parse_kcl_records,
    parse_kroger_products,
    parse_tj_products,
    parse_wfm_products,
)
from grocery_optimizer.silver.resolve import resolve_products
from grocery_optimizer.silver.units import parse_size, unit_price

FIXTURES = Path(__file__).parent / "fixtures"


# ---- units -----------------------------------------------------------------

@pytest.mark.parametrize(
    "text,base_unit,base_qty",
    [
        ("1 gal", "fl_oz", 128.0),
        ("1/2 gal", "fl_oz", 64.0),
        ("13 fl oz", "fl_oz", 13.0),
        ("19 oz", "oz", 19.0),
        ("2 lb", "oz", 32.0),
        ("per lb", "oz", 16.0),
        ("10 ct", "ct", 10.0),
    ],
)
def test_parse_size(text, base_unit, base_qty):
    parsed = parse_size(text)
    assert parsed is not None
    assert parsed["base_unit"] == base_unit
    assert parsed["base_qty"] == pytest.approx(base_qty)


def test_parse_size_unparseable():
    assert parse_size("Cucumber") is None
    assert parse_size(None) is None


def test_unit_price():
    assert unit_price(3.99, "1 gal") == (0.0312, "$/fl_oz")
    assert unit_price(4.99, "19 oz") == (0.2626, "$/oz")
    assert unit_price(2.99, None) == (None, None)


# ---- name normalization ----------------------------------------------------

def test_normalize_name_strips_trademark_and_size():
    assert normalize_name("Harris Teeter® Vitamin D Whole Milk") == (
        "harris teeter vitamin d whole milk"
    )
    assert "19" not in normalize_name("Mild Italian Sausage Links, 19 oz")
    assert normalize_name("Nectarines, 2 lb") == "nectarines"


# ---- bronze parsing --------------------------------------------------------

def test_parse_kroger_products():
    raw = (FIXTURES / "kroger_products_sample.json").read_bytes()
    recs = parse_kroger_products(raw)
    assert len(recs) == 2

    milk = recs[0]
    assert milk["source"] == "kroger"
    assert milk["source_sku"] == "0001"
    assert milk["price"] == 3.99
    assert milk["regular_price"] is None  # no promo
    assert milk["is_promo"] is False
    assert milk["unit_price"] == 0.0312
    assert milk["unit"] == "$/fl_oz"

    promo = recs[1]  # has a promo cheaper than regular
    assert promo["is_promo"] is True
    assert promo["price"] == 3.50
    assert promo["regular_price"] == 4.99
    assert promo["discount_pct"] == 30
    assert promo["category_hint"] == "Natural & Organic"


def test_parse_kcl_records():
    raw = (FIXTURES / "kcl_aldi_sample.html").read_bytes()
    recs = parse_kcl_records(raw)
    by_name = {r["raw_name"]: r for r in recs}

    cuke = by_name["Cucumber"]
    assert cuke["source"] == "aldi_kcl"
    assert cuke["source_sku"] == "3253345"
    assert cuke["unit_price"] is None  # no size -> no unit price

    mand = by_name["Mandarins, 3 lb"]
    assert mand["unit_price"] == pytest.approx(2.99 / 48, abs=1e-4)  # 3 lb = 48 oz
    assert mand["unit"] == "$/oz"


def test_parse_wfm_products():
    raw = (FIXTURES / "wfm_products_sample.json").read_bytes()
    recs = parse_wfm_products(raw)
    assert len(recs) == 2

    onion = recs[0]
    assert onion["source"] == "wholefoods"
    assert onion["source_sku"] == "produce-organic-white-onion-b0787z3t3b"
    assert onion["category_hint"] == "Produce"  # from breadcrumb
    assert onion["is_promo"] is False
    # $3.19/lb normalized to $/oz
    assert onion["unit"] == "$/oz"
    assert onion["unit_price"] == pytest.approx(3.19 / 16, abs=1e-4)

    milk = recs[1]  # has a salePrice below regular
    assert milk["is_promo"] is True
    assert milk["price"] == 3.49
    assert milk["regular_price"] == 4.29
    assert milk["discount_pct"] == 19
    assert milk["unit"] == "$/ct"  # uom "ea"


def test_parse_tj_products():
    raw = (FIXTURES / "tj_products_sample.json").read_bytes()
    recs = parse_tj_products(raw)
    assert len(recs) == 2

    crust = recs[0]
    assert crust["source"] == "traderjoes"
    assert crust["source_sku"] == "054225"
    assert crust["price"] == 3.99
    assert crust["category_hint"] == "Bakery"  # deepest hierarchy node
    assert crust["unit"] == "$/oz"
    assert crust["unit_price"] == pytest.approx(3.99 / 15, abs=1e-4)  # "15 Oz"

    stilton = recs[1]
    assert stilton["unit_price"] == pytest.approx(12.99 / 16, abs=1e-4)  # "1 Lb"


# ---- entity resolution -----------------------------------------------------

def _add_source_product(con, source, sku, name, category=None):
    con.execute(
        """
        INSERT INTO source_products (source, source_sku, raw_name, category_hint)
        VALUES (?, ?, ?, ?)
        """,
        [source, sku, name, category],
    )


def test_resolve_links_creates_and_dedupes():
    con = duckdb.connect(":memory:")
    init_db(con)
    con.execute(
        "INSERT INTO canonical_products (name, category) VALUES ('Whole Milk', 'Dairy')"
    )
    _add_source_product(con, "kroger", "A", "Whole Milk", "Dairy")  # identical -> link
    _add_source_product(con, "aldi_kcl", "B", "Bananas")  # distinct -> new canonical

    stats = resolve_products(con)
    assert stats["linked"] >= 1
    assert stats["created"] >= 1

    linked = con.execute(
        "SELECT canonical_id FROM source_products WHERE source_sku = 'A'"
    ).fetchone()[0]
    assert linked is not None
    # No unresolved rows remain (all either linked or turned into canonicals).
    assert con.execute(
        "SELECT count(*) FROM source_products WHERE canonical_id IS NULL"
    ).fetchone()[0] == 0


def test_resolve_queues_ambiguous():
    con = duckdb.connect(":memory:")
    init_db(con)
    con.execute(
        "INSERT INTO canonical_products (name, category) VALUES ('Cheddar Cheese', 'Dairy')"
    )
    _add_source_product(con, "aldi_kcl", "C", "Chedar Cheese", "Dairy")  # typo -> <100

    # auto so high nothing auto-links; review_low so low the near-match queues.
    stats = resolve_products(con, auto=100, review_low=1)
    assert stats["queued"] == 1
    assert con.execute(
        "SELECT count(*) FROM resolution_queue WHERE status = 'open'"
    ).fetchone()[0] == 1
    # Ambiguous row stays OUT of gold (unlinked).
    assert con.execute(
        "SELECT canonical_id FROM source_products WHERE source_sku = 'C'"
    ).fetchone()[0] is None

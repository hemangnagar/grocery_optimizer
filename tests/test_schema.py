"""Schema smoke tests: init a throwaway DuckDB and assert the medallion objects
exist and the source dimension is seeded."""

from __future__ import annotations

import duckdb

from grocery_optimizer.db import init_db

EXPECTED_TABLES = {
    "bronze_manifest",
    "sources",
    "stores",
    "canonical_products",
    "source_products",
    "prices",
    "resolution_queue",
}

EXPECTED_VIEWS = {
    "gold_current_prices",
    "gold_cheapest_source_per_item",
}


def _objects(con, table_type):
    rows = con.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'main' AND table_type = ?
        """,
        [table_type],
    ).fetchall()
    return {r[0] for r in rows}


def test_schema_creates_all_objects():
    con = duckdb.connect(":memory:")
    init_db(con)

    tables = _objects(con, "BASE TABLE")
    views = _objects(con, "VIEW")

    assert EXPECTED_TABLES <= tables, f"missing tables: {EXPECTED_TABLES - tables}"
    assert EXPECTED_VIEWS <= views, f"missing views: {EXPECTED_VIEWS - views}"


def test_sources_seeded():
    con = duckdb.connect(":memory:")
    init_db(con)
    count = con.execute("SELECT count(*) FROM sources").fetchone()[0]
    assert count == 6


def test_init_is_idempotent():
    con = duckdb.connect(":memory:")
    init_db(con)
    # Re-running must not error and must not duplicate seeded rows.
    init_db(con)
    count = con.execute("SELECT count(*) FROM sources").fetchone()[0]
    assert count == 6


def test_gold_views_query_empty_cleanly():
    con = duckdb.connect(":memory:")
    init_db(con)
    # With no prices, gold views should return zero rows without error.
    assert con.execute("SELECT count(*) FROM gold_current_prices").fetchone()[0] == 0
    assert (
        con.execute("SELECT count(*) FROM gold_cheapest_source_per_item").fetchone()[0]
        == 0
    )

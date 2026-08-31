"""Geolocation tests: haversine, home geocode, the gold radius filter (with
its NULL-never-blocks rule), verdict distance chips, and the Kroger Locations
parser."""

from __future__ import annotations

from pathlib import Path

import duckdb

from grocery_optimizer import config
from grocery_optimizer.db import init_db
from grocery_optimizer.scripts.kroger_locations import parse_locations, upsert_stores
from grocery_optimizer.silver.geo import (
    ZIP_CENTROIDS,
    haversine_miles,
    home_latlon,
    refresh_home_location,
)
from grocery_optimizer.silver.verdict import build_verdict

FIXTURES = Path(__file__).parent / "fixtures"

VIENNA = ZIP_CENTROIDS["22180"]
DC = ZIP_CENTROIDS["20001"]
BALTIMORE = (39.2904, -76.6122)  # ~45 mi from Vienna, well out of range


def _fresh():
    con = duckdb.connect(":memory:")
    init_db(con)
    return con


def _store(con, source, store_id, lat=None, lon=None):
    con.execute(
        "INSERT INTO stores (source, store_id, name, lat, lon) VALUES (?, ?, ?, ?, ?)",
        [source, store_id, f"{source} {store_id}", lat, lon],
    )


def _priced(con, source, sku, canonical_id, price, store_id=None):
    spid = con.execute(
        "INSERT INTO source_products (source, source_sku, raw_name, canonical_id, match_confidence) "
        "VALUES (?, ?, ?, ?, 0.95) RETURNING source_product_id",
        [source, sku, f"{source} {sku}", canonical_id],
    ).fetchone()[0]
    con.execute(
        "INSERT INTO prices (source_product_id, source, store_id, observed_at, price, confidence) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, 0.95)",
        [spid, source, store_id, price],
    )


def _canonical(con, name):
    return con.execute(
        "INSERT INTO canonical_products (name) VALUES (?) RETURNING canonical_id", [name]
    ).fetchone()[0]


# -- haversine + geocode ----------------------------------------------------

def test_haversine_vienna_to_dc_is_plausible():
    d = haversine_miles(*VIENNA, *DC)
    assert 11 < d < 15


def test_haversine_zero_distance():
    assert haversine_miles(*VIENNA, *VIENNA) == 0.0


def test_home_latlon_env_override_beats_centroid(monkeypatch):
    monkeypatch.setattr(config, "HOME_LAT", 39.0)
    monkeypatch.setattr(config, "HOME_LON", -77.5)
    assert home_latlon() == (39.0, -77.5)


def test_home_latlon_unknown_zip_without_override_is_none(monkeypatch):
    monkeypatch.setattr(config, "HOME_LAT", None)
    monkeypatch.setattr(config, "HOME_LON", None)
    monkeypatch.setattr(config, "HOME_ZIP", "99999")
    assert home_latlon() is None


def test_init_db_writes_home_location_row(monkeypatch):
    monkeypatch.setattr(config, "HOME_LAT", None)
    monkeypatch.setattr(config, "HOME_LON", None)
    monkeypatch.setattr(config, "HOME_ZIP", "22180")
    monkeypatch.setattr(config, "SEARCH_RADIUS_MILES", 5)
    con = _fresh()
    zip_, lat, lon, radius = con.execute(
        "SELECT home_zip, lat, lon, radius_miles FROM home_location"
    ).fetchone()
    assert zip_ == "22180"
    assert (lat, lon) == VIENNA
    assert radius == 5.0


def test_refresh_home_location_ungeocodable_leaves_table_empty(monkeypatch):
    con = _fresh()
    monkeypatch.setattr(config, "HOME_LAT", None)
    monkeypatch.setattr(config, "HOME_LON", None)
    monkeypatch.setattr(config, "HOME_ZIP", "99999")
    assert refresh_home_location(con) is None
    assert con.execute("SELECT count(*) FROM home_location").fetchone()[0] == 0


# -- gold radius filter -----------------------------------------------------

def _radius_dataset(monkeypatch):
    """Milk at three kroger stores: in-range, out-of-range, and geo-less."""
    monkeypatch.setattr(config, "HOME_LAT", None)
    monkeypatch.setattr(config, "HOME_LON", None)
    monkeypatch.setattr(config, "HOME_ZIP", "22180")
    monkeypatch.setattr(config, "SEARCH_RADIUS_MILES", 5)
    con = _fresh()
    _store(con, "kroger", "near", *ZIP_CENTROIDS["22027"])  # ~2 mi
    _store(con, "kroger", "far", *BALTIMORE)                # ~45 mi
    _store(con, "kroger", "nogeo")                          # NULL lat/lon
    milk = _canonical(con, "Milk")
    _priced(con, "kroger", "m-near", milk, 3.00, store_id="near")
    _priced(con, "kroger", "m-far", milk, 1.00, store_id="far")
    _priced(con, "kroger", "m-nogeo", milk, 4.00, store_id="nogeo")
    return con, milk


def test_out_of_range_store_excluded_from_gold(monkeypatch):
    con, _ = _radius_dataset(monkeypatch)
    stores = {
        r[0] for r in con.execute("SELECT store_id FROM gold_current_prices").fetchall()
    }
    assert "near" in stores
    assert "far" not in stores  # cheapest, but 45 miles away
    assert "nogeo" in stores    # unknown distance never excludes


def test_gold_distance_and_empty_home_disables_filter(monkeypatch):
    con, _ = _radius_dataset(monkeypatch)
    d = con.execute(
        "SELECT distance_miles FROM gold_current_prices WHERE store_id = 'near'"
    ).fetchone()[0]
    assert 1 < d < 4

    # No home row -> every store (even 'far') is back in range.
    con.execute("DELETE FROM home_location")
    stores = {
        r[0] for r in con.execute("SELECT store_id FROM gold_current_prices").fetchall()
    }
    assert stores == {"near", "far", "nogeo"}


def test_verdict_carries_nearest_store_distance(monkeypatch):
    con, milk = _radius_dataset(monkeypatch)
    bid = con.execute(
        "INSERT INTO baskets (name) VALUES ('t') RETURNING basket_id"
    ).fetchone()[0]
    con.execute(
        "INSERT INTO basket_items (basket_id, canonical_id, label, quantity) VALUES (?, ?, 'milk', 1)",
        [bid, milk],
    )
    verdict = build_verdict(con, bid)
    kroger = next(s for s in verdict["flexible"]["stores"] if s["source"] == "kroger")
    assert kroger["distance_miles"] is not None
    assert 1 < kroger["distance_miles"] < 4
    # The far store's $1.00 offer must not leak into the total.
    assert kroger["store_total"] == 3.00


# -- Kroger Locations parser ------------------------------------------------

def test_parse_locations_fixture():
    raw = (FIXTURES / "kroger_locations_sample.json").read_bytes()
    rows = parse_locations(raw)
    assert len(rows) == 2  # row without locationId dropped
    ht = rows[0]
    assert ht["store_id"] == "09700308"
    assert ht["banner"] == "HARRISTEETER"
    assert ht["zip"] == "22027"
    assert ht["lat"] == 38.8834 and ht["lon"] == -77.2271
    assert ht["address"] == "2425 Centreville Rd, Vienna, VA"
    assert rows[1]["lat"] is None  # geo-less location kept, coordinates NULL


def test_upsert_stores_updates_in_place():
    con = _fresh()
    raw = (FIXTURES / "kroger_locations_sample.json").read_bytes()
    rows = parse_locations(raw)
    upsert_stores(con, rows)
    upsert_stores(con, rows)  # idempotent
    got = con.execute(
        "SELECT store_id, lat FROM stores WHERE source = 'kroger' ORDER BY store_id"
    ).fetchall()
    assert got == [("02900511", None), ("09700308", 38.8834)]

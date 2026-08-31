"""Geolocation: home centroid, haversine distance, store radius filtering.

The store comparison set is constrained to stores within
``config.SEARCH_RADIUS_MILES`` of ``config.HOME_ZIP`` — comparing prices
across the whole metro is meaningless if the user won't drive there.

Design (all deterministic, zero external services):

- HOME: a static zip -> centroid table below geocodes ``HOME_ZIP``; a
  ``HOME_LAT``/``HOME_LON`` .env override wins when set (or when the zip is
  not in the table). ``refresh_home_location`` writes the result to the
  single-row ``home_location`` table so GOLD VIEWS can compute distance in
  SQL — the radius filter lives in gold, not in Python.
- STORES: lat/lon comes from chain locator APIs (Kroger Locations API is
  official; see ``scripts/kroger_locations.py``) or the demo seed.
- NULL never blocks (same recall-over-precision rule as the taxonomy guard):
  a store with no lat/lon, or a DB with no home row, is always in range.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import duckdb

from .. import config

# Approximate zip centroids for the DC-metro coverage area (NoVA + DC core).
# Good to ~a mile — fine for a "would I drive there" radius, not for routing.
# Zips outside this table need the HOME_LAT/HOME_LON .env override.
ZIP_CENTROIDS: dict[str, tuple[float, float]] = {
    # Vienna / Oakton / Dunn Loring
    "22180": (38.8960, -77.2555), "22181": (38.8945, -77.2870),
    "22182": (38.9310, -77.2730), "22027": (38.8945, -77.2225),
    "22124": (38.8880, -77.3200),
    # Fairfax / Merrifield
    "22031": (38.8590, -77.2600), "22030": (38.8460, -77.3270),
    "22032": (38.8180, -77.2900), "22033": (38.8750, -77.3840),
    # McLean / Falls Church / Annandale
    "22101": (38.9390, -77.1720), "22102": (38.9530, -77.2290),
    "22042": (38.8650, -77.1940), "22043": (38.9010, -77.1980),
    "22044": (38.8600, -77.1550), "22046": (38.8870, -77.1810),
    "22003": (38.8300, -77.2130), "22041": (38.8490, -77.1410),
    # Reston / Herndon / Great Falls
    "20190": (38.9600, -77.3400), "20191": (38.9330, -77.3520),
    "20170": (38.9800, -77.3860), "22066": (39.0090, -77.3010),
    # Arlington / Alexandria / DC core
    "22201": (38.8870, -77.0950), "22203": (38.8740, -77.1110),
    "22205": (38.8830, -77.1400), "22207": (38.9060, -77.1240),
    "22314": (38.8060, -77.0550), "20001": (38.9100, -77.0180),
    # Springfield / Burke / Centreville / Chantilly
    "22150": (38.7720, -77.1860), "22015": (38.7900, -77.2840),
    "20120": (38.8580, -77.4640), "20151": (38.8870, -77.4460),
}

EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def home_latlon() -> tuple[float, float] | None:
    """Home coordinates: .env override first, then the zip centroid table."""
    if config.HOME_LAT is not None and config.HOME_LON is not None:
        return config.HOME_LAT, config.HOME_LON
    return ZIP_CENTROIDS.get(config.HOME_ZIP)


def refresh_home_location(con: duckdb.DuckDBPyConnection) -> dict | None:
    """(Re)write the single ``home_location`` row from config.

    Called by ``init_db`` so the gold radius filter always reflects the
    current .env. Returns the row written, or None when the home zip can't be
    geocoded (the table is left empty and every store stays in range).
    """
    latlon = home_latlon()
    con.execute("DELETE FROM home_location")
    if latlon is None:
        return None
    lat, lon = latlon
    row = {
        "home_zip": config.HOME_ZIP,
        "lat": lat,
        "lon": lon,
        "radius_miles": float(config.SEARCH_RADIUS_MILES),
    }
    con.execute(
        "INSERT INTO home_location (home_zip, lat, lon, radius_miles, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [row["home_zip"], lat, lon, row["radius_miles"], datetime.now(timezone.utc)],
    )
    return row

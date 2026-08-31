"""Populate ``stores`` lat/lon via the official Kroger Locations API.

Fetches locations near HOME_ZIP (all banners the key exposes — Harris Teeter
is chain code HARRISTEETER), saves the raw response to bronze first (time
machine), then upserts ``stores`` rows with coordinates so the gold radius
filter and PWA distance chips have real data. Usage::

    uv run grocery-kroger-locations

Other chains have no official locator API; their store rows keep NULL lat/lon
(which never filters them out) until seeded another way.
"""

from __future__ import annotations

import json
import sys

import duckdb

from ..bronze.kroger import KrogerAuthError, KrogerClient
from ..bronze.manifest import save_artifact
from ..config import HOME_ZIP, SEARCH_RADIUS_MILES, get_kroger_credentials
from ..db import get_connection, init_db

# Fetch wider than the verdict radius: capturing nearby-but-out-of-range
# stores is cheap, and the gold filter (not the fetch) decides what counts.
FETCH_RADIUS_MILES = max(15, SEARCH_RADIUS_MILES)


def parse_locations(raw: bytes) -> list[dict]:
    """Kroger Locations payload -> store rows (one per location)."""
    payload = json.loads(raw)
    rows: list[dict] = []
    for loc in payload.get("data", []):
        address = loc.get("address") or {}
        geo = loc.get("geolocation") or {}
        parts = [address.get("addressLine1"), address.get("city"), address.get("state")]
        rows.append(
            {
                "store_id": loc.get("locationId"),
                "banner": loc.get("chain"),
                "name": loc.get("name"),
                "address": ", ".join(p for p in parts if p) or None,
                "zip": address.get("zipCode"),
                "lat": geo.get("latitude"),
                "lon": geo.get("longitude"),
            }
        )
    return [r for r in rows if r["store_id"]]


def upsert_stores(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> int:
    for r in rows:
        con.execute(
            """
            INSERT INTO stores (source, store_id, banner, name, address, zip, region, lat, lon)
            VALUES ('kroger', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, store_id) DO UPDATE SET
                banner = excluded.banner, name = excluded.name,
                address = excluded.address, zip = excluded.zip,
                lat = excluded.lat, lon = excluded.lon
            """,
            [r["store_id"], r["banner"], r["name"], r["address"], r["zip"],
             f"zip:{HOME_ZIP}", r["lat"], r["lon"]],
        )
    return len(rows)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    client_id, client_secret = get_kroger_credentials()
    if not client_id or not client_secret:
        print("Kroger credentials not set; run grocery-kroger-keytest for setup help.")
        return

    con = get_connection()
    try:
        init_db(con)
        with KrogerClient(client_id, client_secret) as client:
            try:
                resp = client.get_locations(HOME_ZIP, radius_miles=FETCH_RADIUS_MILES)
            except KrogerAuthError as exc:
                print(f"Auth FAILED: {exc}")
                return
            save_artifact(
                con,
                source="kroger",
                source_kind="api",
                content=resp.content,
                request_url=str(resp.request.url),
                request_params={
                    "endpoint": "locations",
                    "zip": HOME_ZIP,
                    "radius": FETCH_RADIUS_MILES,
                },
                http_status=resp.status_code,
                content_type=resp.headers.get("content-type"),
                region=f"zip:{HOME_ZIP}",
                parse_status="parsed",  # parsed inline below, not by grocery-normalize
                notes="store locations",
            )
            if resp.status_code != 200:
                print(f"Locations request failed: HTTP {resp.status_code}")
                return
            rows = parse_locations(resp.content)
            n = upsert_stores(con, rows)
            with_geo = sum(1 for r in rows if r["lat"] is not None)
            print(
                f"Upserted {n} Kroger-family stores near {HOME_ZIP} "
                f"(radius {FETCH_RADIUS_MILES} mi), {with_geo} with coordinates."
            )
            for r in rows[:10]:
                print(f"  {r['store_id']}  {r['banner'] or '?':<14} {r['name']}")
    finally:
        con.close()


if __name__ == "__main__":
    main()

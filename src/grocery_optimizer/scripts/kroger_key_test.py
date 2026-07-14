"""Kroger key test (Build Step 2): what Kroger-family stores are in range, and
does the API expose Harris Teeter with pricing?

The Kroger Developer API is our gold-standard source. This script:

  1. Verifies OAuth2 client-credentials auth works.
  2. Queries stores within SEARCH_RADIUS_MILES of the user's HOME_ZIP — the
     realistic comparison set (no point pricing a store you won't drive to).
  3. Lists the in-range stores and flags Harris Teeter.
  4. Probes location-filtered product pricing at the nearest in-range store.

Every raw API response is preserved to bronze first (never parse-and-discard).

Usage::

    uv run grocery-kroger-keytest
"""

from __future__ import annotations

import sys

from ..bronze.kroger import KrogerAuthError, KrogerClient
from ..bronze.manifest import save_artifact
from ..config import HOME_ZIP, SEARCH_RADIUS_MILES, get_kroger_credentials
from ..db import get_connection

# Kroger's location API returns Harris Teeter as the 4-letter chain code "HART"
# (not "HARRISTEETER"); also match on the store name as a safety net.
HT_CHAIN_CODE = "HART"
PROBE_TERM = "milk"


def _is_harris_teeter(loc: dict) -> bool:
    chain = (loc.get("chain") or "").upper()
    name = (loc.get("name") or "").upper()
    return chain == HT_CHAIN_CODE or "HARRIS TEETER" in name


def _fmt_store(loc: dict) -> str:
    addr = loc.get("address", {})
    return (
        f"{loc.get('name')} [{loc.get('chain')}] "
        f"(locationId {loc.get('locationId')})\n"
        f"        {addr.get('addressLine1')}, "
        f"{addr.get('city')} {addr.get('state')} {addr.get('zipCode')}"
    )


def _print_missing_credentials() -> None:
    print("Kroger credentials are not set - cannot run the key test.\n")
    print("To get a key (free, ~10 min):")
    print("  1. Register at https://developer.kroger.com/")
    print("  2. Create an app; copy its Client ID and Client Secret.")
    print("  3. In the project .env (copy from .env.example) set:")
    print("       KROGER_CLIENT_ID=...")
    print("       KROGER_CLIENT_SECRET=...")
    print("  4. Re-run:  uv run grocery-kroger-keytest")


def main() -> None:
    # Product descriptions contain trademark symbols; render them cleanly on
    # Windows consoles instead of mojibake (bronze already stores UTF-8).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    client_id, client_secret = get_kroger_credentials()
    if not client_id or not client_secret:
        _print_missing_credentials()
        return

    con = get_connection()
    try:
        with KrogerClient(client_id, client_secret) as client:
            # 1. Auth check (raises KrogerAuthError on bad creds).
            try:
                client._ensure_token()
            except KrogerAuthError as exc:
                print(f"Auth FAILED: {exc}")
                return
            print("Auth OK - token acquired.\n")

            # 2. Stores within the user's realistic driving radius.
            print(
                f"Stores within {SEARCH_RADIUS_MILES} miles of {HOME_ZIP} "
                "(the comparison set):"
            )
            resp = client.get_locations(
                HOME_ZIP, radius_miles=SEARCH_RADIUS_MILES, limit=50
            )
            manifest_id, path = save_artifact(
                con,
                source="kroger",
                source_kind="api",
                content=resp.content,
                request_url=str(resp.request.url),
                request_params={
                    "zip": HOME_ZIP,
                    "radius_miles": SEARCH_RADIUS_MILES,
                    "endpoint": "locations",
                },
                http_status=resp.status_code,
                content_type=resp.headers.get("content-type"),
                region=f"zip:{HOME_ZIP}",
                notes="step2 key test: in-range locations",
            )
            if resp.status_code != 200:
                print(f"  HTTP {resp.status_code} (saved bronze manifest {manifest_id})")
                return

            # API returns locations nearest-first.
            locations = resp.json().get("data", [])
            ht_locations = [loc for loc in locations if _is_harris_teeter(loc)]

            if not locations:
                print("  (no Kroger-family stores in range)")
            for loc in locations:
                flag = "  <-- Harris Teeter" if _is_harris_teeter(loc) else ""
                print(f"  {_fmt_store(loc)}{flag}")
            print(f"\n  saved bronze {path.name} (manifest {manifest_id})")

            # 3. Verdict.
            print("\n" + "=" * 60)
            if ht_locations:
                print(
                    f"RESULT: {len(ht_locations)} Harris Teeter store(s) within "
                    f"{SEARCH_RADIUS_MILES} mi of {HOME_ZIP}."
                )
            elif locations:
                print(
                    f"RESULT: No Harris Teeter within {SEARCH_RADIUS_MILES} mi of "
                    f"{HOME_ZIP}, but {len(locations)} other Kroger-family store(s) are."
                )
            else:
                print(
                    f"RESULT: No Kroger-family stores within {SEARCH_RADIUS_MILES} mi "
                    f"of {HOME_ZIP}. Try a larger radius or different ZIP."
                )
            print("=" * 60)

            # 4. Probe pricing at the nearest in-range store (prefer an HT store).
            probe_loc = ht_locations[0] if ht_locations else (
                locations[0] if locations else None
            )
            if probe_loc is not None:
                _probe_pricing(con, client, probe_loc)
    finally:
        con.close()


def _probe_pricing(con, client: KrogerClient, loc: dict) -> None:
    location_id = loc.get("locationId")
    print(
        f"\nProbing location-filtered pricing at {loc.get('name')} "
        f"({location_id}) for '{PROBE_TERM}':"
    )
    resp = client.get_products(PROBE_TERM, location_id=location_id, limit=5)
    manifest_id, _ = save_artifact(
        con,
        source="kroger",
        source_kind="api",
        content=resp.content,
        request_url=str(resp.request.url),
        request_params={
            "endpoint": "products",
            "term": PROBE_TERM,
            "locationId": location_id,
        },
        http_status=resp.status_code,
        content_type=resp.headers.get("content-type"),
        store_id=str(location_id),
        notes="step2 key test: pricing probe",
    )
    if resp.status_code != 200:
        print(f"  products HTTP {resp.status_code} (bronze manifest {manifest_id})")
        return
    priced = 0
    for product in resp.json().get("data", []):
        desc = product.get("description", "?")
        items = product.get("items", [])
        price = items[0].get("price") if items else None
        if price:
            priced += 1
            reg = price.get("regular")
            promo = price.get("promo")
            promo_txt = f", promo ${promo}" if promo else ""
            print(f"  ${reg}{promo_txt}  {desc}")
        else:
            print(f"  (no price)  {desc}")
    if priced:
        print(f"\nLocation-filtered pricing works: {priced} priced item(s) returned.")
    else:
        print("\nNo prices returned for this location/term.")


if __name__ == "__main__":
    main()

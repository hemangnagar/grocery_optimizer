"""Kroger key test (Build Step 2): does the Kroger API expose Harris Teeter?

The Kroger Developer API is our gold-standard source, but Harris Teeter is a
Kroger banner and it's unknown whether HT locations (and their location-filtered
pricing) are reachable with a plain developer key. This script answers that:

  1. Verify OAuth2 client-credentials auth works.
  2. Query store locations near several DC-metro ZIPs.
  3. Report every chain/banner seen; flag whether Harris Teeter appears.
  4. If HT appears, probe location-filtered product pricing for one HT store.

Every raw API response is preserved to bronze first (never parse-and-discard).

Usage::

    uv run grocery-kroger-keytest
"""

from __future__ import annotations

from ..bronze.kroger import KrogerAuthError, KrogerClient
from ..bronze.manifest import save_artifact
from ..config import get_kroger_credentials
from ..db import get_connection

# DC-metro ZIPs where Harris Teeter has a strong presence.
DC_METRO_ZIPS = ["20009", "22201", "20814"]  # DC (Dupont), Arlington VA, Bethesda MD
HT_CANON = "HARRISTEETER"
PROBE_TERM = "milk"


def _norm_chain(chain: str) -> str:
    return (chain or "").upper().replace(" ", "").replace("-", "")


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
    client_id, client_secret = get_kroger_credentials()
    if not client_id or not client_secret:
        _print_missing_credentials()
        return

    con = get_connection()
    chains_seen: dict[str, int] = {}
    ht_locations: list[dict] = []

    try:
        with KrogerClient(client_id, client_secret) as client:
            # 1. Auth check (raises KrogerAuthError on bad creds).
            try:
                client._ensure_token()
            except KrogerAuthError as exc:
                print(f"Auth FAILED: {exc}")
                return
            print("Auth OK - token acquired.\n")

            # 2 + 3. Locations sweep across DC-metro ZIPs.
            print(f"Querying locations near ZIPs: {', '.join(DC_METRO_ZIPS)}")
            for zip_code in DC_METRO_ZIPS:
                resp = client.get_locations(zip_code, radius_miles=15)
                manifest_id, path = save_artifact(
                    con,
                    source="kroger",
                    source_kind="api",
                    content=resp.content,
                    request_url=str(resp.request.url),
                    request_params={"zip": zip_code, "endpoint": "locations"},
                    http_status=resp.status_code,
                    content_type=resp.headers.get("content-type"),
                    region=f"zip:{zip_code}",
                    notes="step2 key test: locations",
                )
                if resp.status_code != 200:
                    print(
                        f"  zip {zip_code}: HTTP {resp.status_code} "
                        f"(saved bronze manifest {manifest_id})"
                    )
                    continue
                data = resp.json().get("data", [])
                for loc in data:
                    chain = loc.get("chain", "?")
                    chains_seen[chain] = chains_seen.get(chain, 0) + 1
                    if _norm_chain(chain) == HT_CANON:
                        ht_locations.append(loc)
                print(
                    f"  zip {zip_code}: {len(data)} locations "
                    f"-> bronze {path.name} (manifest {manifest_id})"
                )

            # Report chains seen.
            print("\nChains/banners seen near DC metro:")
            for chain, count in sorted(chains_seen.items(), key=lambda x: -x[1]):
                marker = "  <-- Harris Teeter" if _norm_chain(chain) == HT_CANON else ""
                print(f"  {count:>3}  {chain}{marker}")

            # Verdict.
            print("\n" + "=" * 60)
            if ht_locations:
                print(f"RESULT: Harris Teeter IS exposed ({len(ht_locations)} locations).")
                sample = ht_locations[0]
                addr = sample.get("address", {})
                print(
                    f"  sample: {sample.get('name')} "
                    f"(locationId {sample.get('locationId')})\n"
                    f"          {addr.get('addressLine1')}, "
                    f"{addr.get('city')} {addr.get('state')} {addr.get('zipCode')}"
                )
            else:
                print("RESULT: Harris Teeter is NOT exposed by this key.")
                print("  Fallback (per spec): use Kroger-banner stores as calibration data.")
            print("=" * 60)

            # 4. Probe location-filtered pricing for one HT store (or fall back
            #    to any store seen) to confirm pricing is reachable.
            probe_loc = ht_locations[0] if ht_locations else None
            if probe_loc is None:
                # Fall back to the first store from the last successful response.
                probe_loc = _first_any_location(con, client)
            if probe_loc is not None:
                _probe_pricing(con, client, probe_loc)
    finally:
        con.close()


def _first_any_location(con, client: KrogerClient) -> dict | None:
    """Grab any single location (for a pricing probe when no HT store exists)."""
    resp = client.get_locations(DC_METRO_ZIPS[0], radius_miles=15, limit=1)
    save_artifact(
        con,
        source="kroger",
        source_kind="api",
        content=resp.content,
        request_url=str(resp.request.url),
        request_params={"zip": DC_METRO_ZIPS[0], "endpoint": "locations", "limit": 1},
        http_status=resp.status_code,
        content_type=resp.headers.get("content-type"),
        region=f"zip:{DC_METRO_ZIPS[0]}",
        notes="step2 key test: fallback location for pricing probe",
    )
    if resp.status_code != 200:
        return None
    data = resp.json().get("data", [])
    return data[0] if data else None


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

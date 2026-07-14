"""Fetch a per-store Kroger (Harris Teeter) catalog into bronze.

The Kroger Products API is search-based (no full-catalog dump), so we drive it
from a curated list of common family-basket search terms and save each raw
response to bronze. Parsing into silver is done by grocery-normalize.

Usage::

    uv run grocery-kroger-fetch
"""

from __future__ import annotations

import sys

from ..bronze.kroger import KrogerAuthError, KrogerClient
from ..bronze.manifest import save_artifact
from ..config import HOME_ZIP, KROGER_STORE_ID, get_kroger_credentials
from ..db import get_connection

# Curated staple terms spanning a family weekly basket. This is the bridge to
# step 6, where fetches will be driven by the predicted basket itself.
STAPLE_TERMS = [
    "milk", "eggs", "butter", "bread", "cheese", "yogurt", "orange juice",
    "chicken breast", "ground beef", "bacon", "salmon",
    "bananas", "apples", "strawberries", "potatoes", "onions", "tomatoes",
    "lettuce", "carrots", "broccoli",
    "rice", "pasta", "pasta sauce", "cereal", "oats", "flour", "sugar",
    "olive oil", "peanut butter", "coffee", "canned beans", "frozen vegetables",
]
PAGE_LIMIT = 50


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
    total = 0
    try:
        with KrogerClient(client_id, client_secret) as client:
            try:
                client._ensure_token()
            except KrogerAuthError as exc:
                print(f"Auth FAILED: {exc}")
                return
            print(f"Kroger store {KROGER_STORE_ID} (home {HOME_ZIP}) - {len(STAPLE_TERMS)} terms:")
            for term in STAPLE_TERMS:
                resp = client.get_products(
                    term, location_id=KROGER_STORE_ID, limit=PAGE_LIMIT
                )
                count = None
                if resp.status_code == 200:
                    try:
                        count = len(resp.json().get("data", []))
                    except ValueError:
                        count = None
                manifest_id, _ = save_artifact(
                    con,
                    source="kroger",
                    source_kind="api",
                    content=resp.content,
                    request_url=str(resp.request.url),
                    request_params={
                        "endpoint": "products",
                        "term": term,
                        "locationId": KROGER_STORE_ID,
                    },
                    http_status=resp.status_code,
                    content_type=resp.headers.get("content-type"),
                    store_id=KROGER_STORE_ID,
                    region=f"zip:{HOME_ZIP}",
                    notes=f"catalog term: {term}",
                )
                if count is not None:
                    total += count
                got = f"{count}" if count is not None else f"HTTP {resp.status_code}"
                print(f"  {term:<20} {got:>5}")
            print(f"\nTotal product rows captured: {total}")
    finally:
        con.close()


if __name__ == "__main__":
    main()

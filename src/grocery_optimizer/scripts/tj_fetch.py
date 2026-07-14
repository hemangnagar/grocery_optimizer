"""Fetch Trader Joe's product prices (national) to bronze via the GraphQL API.

Pulls pages of the Food category and saves each raw response to bronze. Parsing
into silver is done by grocery-normalize.

ToS NOTE: TJ's is Akamai bot-protected; this is PERSONAL-USE only. Review Trader
Joe's terms before any commercial use. See bronze/traderjoes.py.

Usage::

    uv run grocery-tj-fetch
"""

from __future__ import annotations

import sys

from ..bronze.manifest import save_artifact
from ..bronze.traderjoes import TraderJoesClient
from ..db import get_connection

FOOD_CATEGORY_ID = 8
PAGE_SIZE = 100
MAX_PAGES = 3  # bounded, polite (~300 products)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    con = get_connection()
    total = 0
    try:
        with TraderJoesClient() as client:
            print(f"Trader Joe's Food category (national pricing, store {client.store_code}):")
            for page in range(1, MAX_PAGES + 1):
                resp = client.fetch_category_page(
                    FOOD_CATEGORY_ID, page=page, page_size=PAGE_SIZE
                )
                body = resp.body()
                count = None
                if resp.status == 200:
                    try:
                        items = ((resp.json().get("data") or {}).get("products") or {}).get("items") or []
                        count = len(items)
                    except Exception:
                        count = None
                manifest_id, path = save_artifact(
                    con,
                    source="traderjoes",
                    source_kind="scrape",
                    content=body,
                    request_url=resp.url,
                    request_params={
                        "endpoint": "products",
                        "category_id": FOOD_CATEGORY_ID,
                        "page": page,
                        "storeCode": client.store_code,
                    },
                    http_status=resp.status,
                    content_type=resp.headers.get("content-type"),
                    region="national",
                    notes=f"step4b: tj food page {page}",
                )
                if count is not None:
                    total += count
                got = f"{count} products" if count is not None else f"HTTP {resp.status}"
                print(f"  page {page}: {got:>14} -> bronze {path.name} (manifest {manifest_id})")
                if count == 0:
                    break
            print(f"\nTotal product rows captured: {total}")
    finally:
        con.close()


if __name__ == "__main__":
    main()

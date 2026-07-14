"""Fetch Whole Foods store-filtered product prices to bronze.

Pulls a handful of grocery categories for the configured WFM store and saves
each raw JSON page to bronze. Parsing into silver is done by grocery-normalize.

Usage::

    uv run grocery-wfm-fetch
"""

from __future__ import annotations

import sys

from ..bronze.manifest import save_artifact
from ..bronze.wholefoods import WholeFoodsClient
from ..config import HOME_ZIP, WFM_STORE_ID
from ..db import get_connection

# Categories most relevant to a family weekly basket (bounded, polite pull).
CATEGORIES = ["produce", "dairy-eggs", "meat"]
PAGE_LIMIT = 60


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    con = get_connection()
    try:
        with WholeFoodsClient() as client:
            print(f"WFM store {WFM_STORE_ID} (home {HOME_ZIP}):")
            for category in CATEGORIES:
                resp = client.get_category(
                    category, store_id=WFM_STORE_ID, limit=PAGE_LIMIT
                )
                count = None
                if resp.status_code == 200:
                    try:
                        count = len(resp.json().get("results", []))
                    except ValueError:
                        count = None
                manifest_id, path = save_artifact(
                    con,
                    source="wholefoods",
                    source_kind="scrape",
                    content=resp.content,
                    request_url=str(resp.request.url),
                    request_params={
                        "endpoint": "products",
                        "category": category,
                        "store": WFM_STORE_ID,
                    },
                    http_status=resp.status_code,
                    content_type=resp.headers.get("content-type"),
                    store_id=WFM_STORE_ID,
                    region=f"zip:{HOME_ZIP}",
                    notes=f"step4: wfm category {category}",
                )
                got = f"{count} products" if count is not None else f"HTTP {resp.status_code}"
                print(f"  {category:<20} {got:>14} -> bronze {path.name} (manifest {manifest_id})")
    finally:
        con.close()


if __name__ == "__main__":
    main()

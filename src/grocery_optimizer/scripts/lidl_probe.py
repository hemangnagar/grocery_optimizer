"""Probe Lidl ESI flyer endpoints and preserve raw HTML to bronze.

Fetches the leaflet overview for a store/region, then (optionally) the first
linked flyer detail page. Parsing into silver records is done separately by
grocery_optimizer.silver.lidl.

Usage::

    uv run grocery-lidl-probe            # uses default demo store
"""

from __future__ import annotations

from ..bronze.lidl import LidlClient
from ..bronze.manifest import save_artifact
from ..db import get_connection
from ..silver.lidl import parse_overview

# Demo store from Lidl's own docs example. Real DC-metro store enumeration is a
# later refinement (store-locator probe).
DEFAULT_REGION = "F4A"
DEFAULT_STORE_ID = "1215"


def main() -> None:
    con = get_connection()
    try:
        with LidlClient() as client:
            resp = client.get_esi_overview(
                region_shortname=DEFAULT_REGION, store_id=DEFAULT_STORE_ID
            )
            manifest_id, path = save_artifact(
                con,
                source="lidl",
                source_kind="scrape",
                content=resp.content,
                request_url=str(resp.request.url),
                request_params={
                    "region_shortname": DEFAULT_REGION,
                    "store_id": DEFAULT_STORE_ID,
                    "endpoint": "esi-overview",
                },
                http_status=resp.status_code,
                content_type=resp.headers.get("content-type"),
                region=DEFAULT_REGION,
                store_id=DEFAULT_STORE_ID,
                notes="step3: lidl esi overview",
            )
            print(
                f"overview: HTTP {resp.status_code}, {len(resp.content)} bytes "
                f"-> bronze {path.name} (manifest {manifest_id})"
            )
            if resp.status_code != 200:
                return

            flyers = parse_overview(resp.content)
            print(f"parsed {len(flyers)} flyer(s):")
            for f in flyers:
                print(f"  {f['title']}  ->  {f['detail_url']}")

            # Capture the first flyer detail page too, for parser development.
            if flyers:
                detail = client.get_esi_flyer(flyers[0]["detail_url"])
                d_id, d_path = save_artifact(
                    con,
                    source="lidl",
                    source_kind="scrape",
                    content=detail.content,
                    request_url=str(detail.request.url),
                    request_params={"endpoint": "esi-flyer"},
                    http_status=detail.status_code,
                    content_type=detail.headers.get("content-type"),
                    region=DEFAULT_REGION,
                    store_id=DEFAULT_STORE_ID,
                    notes="step3: lidl esi flyer detail",
                )
                print(
                    f"flyer detail: HTTP {detail.status_code}, "
                    f"{len(detail.content)} bytes -> bronze {d_path.name} "
                    f"(manifest {d_id})"
                )
    finally:
        con.close()


if __name__ == "__main__":
    main()

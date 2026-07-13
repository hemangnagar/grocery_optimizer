"""Fetch KCL Aldi deals, preserve raw HTML to bronze, and parse to records.

Usage::

    uv run grocery-kcl-fetch
"""

from __future__ import annotations

from ..bronze.kcl import KCLClient
from ..bronze.manifest import save_artifact
from ..db import get_connection
from ..silver.kcl import parse_deals


def main() -> None:
    con = get_connection()
    try:
        with KCLClient() as client:
            resp = client.get_store_deals("aldi")
            manifest_id, path = save_artifact(
                con,
                source="aldi_kcl",
                source_kind="aggregator",
                content=resp.content,
                request_url=str(resp.request.url),
                request_params={"store": "aldi", "endpoint": "coupons-for"},
                http_status=resp.status_code,
                content_type=resp.headers.get("content-type"),
                notes="step3: kcl aldi deals",
            )
            print(
                f"aldi deals: HTTP {resp.status_code}, {len(resp.content)} bytes "
                f"-> bronze {path.name} (manifest {manifest_id})"
            )
            if resp.status_code != 200:
                return

            deals = parse_deals(resp.content)
            print(f"parsed {len(deals)} deal(s):")
            for d in deals[:20]:
                reg = f" (reg ${d['regular_price']})" if d.get("regular_price") else ""
                pct = f" -{d['discount_pct']}%" if d.get("discount_pct") else ""
                print(f"  ${d.get('price')}{reg}{pct}  {d.get('name')}")
    finally:
        con.close()


if __name__ == "__main__":
    main()

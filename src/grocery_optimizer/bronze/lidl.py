"""Lidl weekly-ad (ESI flyer) prober.

Lidl's storefront is JS-walled, but the flyer backend exposes parameterized
ESI endpoints that return plain HTML:

  Overview (list of leaflets for a store/region):
    GET /flyer/esi-overview/overview
        ?client_locale=lidl/en-US&mode=iframe
        &region_shortname=<REGION>&store_id=<ID>

  Flyer detail (a single leaflet's pages/products), linked from the overview:
    GET /flyer/esi-flyer/<slug>/ar/<store_code>

robots.txt allows everything (Disallow: empty), but we still throttle.
"""

from __future__ import annotations

import httpx

from .fetcher import PoliteClient

LIDL_BASE = "https://www.lidl.com"
_OVERVIEW_PATH = "/flyer/esi-overview/overview"
DEFAULT_LOCALE = "lidl/en-US"


class LidlClient(PoliteClient):
    def __init__(self, *, min_request_interval: float = 2.0, **kwargs) -> None:
        super().__init__(min_request_interval=min_request_interval, **kwargs)

    def get_esi_overview(
        self,
        *,
        region_shortname: str,
        store_id: str | int,
        locale: str = DEFAULT_LOCALE,
    ) -> httpx.Response:
        """Fetch the leaflet overview HTML for a region/store."""
        return self.get(
            LIDL_BASE + _OVERVIEW_PATH,
            params={
                "client_locale": locale,
                "mode": "iframe",
                "region_shortname": region_shortname,
                "store_id": str(store_id),
            },
        )

    def get_esi_flyer(self, flyer_url: str) -> httpx.Response:
        """Fetch a single flyer detail page.

        Accepts either an absolute URL (as linked from the overview) or a path
        beginning with '/flyer/'.
        """
        if flyer_url.startswith("/"):
            flyer_url = LIDL_BASE + flyer_url
        return self.get(flyer_url)

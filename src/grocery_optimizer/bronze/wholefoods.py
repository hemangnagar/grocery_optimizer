"""Whole Foods Market fetcher.

WFM's storefront is a Next.js shell that renders prices client-side per store,
but the frontend's own JSON API is reachable directly (discovered via Playwright
network capture, then replicated with httpx — far lighter than driving a
browser on every pull):

  GET /api/products/category/{category_slug}?store={store_id}&limit=&offset=

Returns JSON with a ``results`` array of products (name, regularPrice,
salePrice, brand, slug, uom). robots.txt allows /products and /api; only
/locations/ (the store finder) is disallowed and we don't touch it.
"""

from __future__ import annotations

import httpx

from .fetcher import PoliteClient

WFM_BASE = "https://www.wholefoodsmarket.com"
_CATEGORY_PATH = "/api/products/category/{slug}"


class WholeFoodsClient(PoliteClient):
    def __init__(self, *, min_request_interval: float = 2.0, **kwargs) -> None:
        super().__init__(
            min_request_interval=min_request_interval,
            extra_headers={"Accept": "application/json"},
            **kwargs,
        )

    def get_category(
        self,
        category_slug: str,
        *,
        store_id: str,
        limit: int = 60,
        offset: int = 0,
    ) -> httpx.Response:
        """Fetch one page of a category's products for a specific store."""
        url = WFM_BASE + _CATEGORY_PATH.format(slug=category_slug)
        return self.get(
            url,
            params={"store": store_id, "limit": limit, "offset": offset},
        )

"""Trader Joe's fetcher via the public GraphQL API.

TJ's ``/api/graphql`` needs no auth, but Akamai blocks bare HTTP clients AND
bundled/headless Chromium (403). A real installed browser (Edge channel) passes,
so we warm Akamai cookies with a homepage load and then replay the site's own
``SearchProducts`` query via the browser's request context.

TJ's pricing is national (storeCode "TJ"), so there's no per-store variation.

ToS NOTE: TJ's is behind bot protection and driving a browser through it is
PERSONAL-USE only here; review Trader Joe's terms before any commercial use.
Kroger (official API) and Whole Foods (open API) are the commercially-safe core.
"""

from __future__ import annotations

import time

from playwright.sync_api import sync_playwright

TJ_GRAPHQL = "https://www.traderjoes.com/api/graphql"
TJ_HOME = "https://www.traderjoes.com/home"
DEFAULT_STORE_CODE = "TJ"  # national pricing
_REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# The site's own SearchProducts query (captured via network recon).
_SEARCH_QUERY = (
    "query SearchProducts($categoryId: String, $currentPage: Int, $pageSize: Int, "
    '$storeCode: String = "TJ", $availability: String = "1", $published: String = "1") '
    "{ products(filter: {store_code: {eq: $storeCode}, published: {eq: $published}, "
    "availability: {match: $availability}, category_id: {eq: $categoryId}}, "
    "currentPage: $currentPage, pageSize: $pageSize) "
    "{ items { sku item_title category_hierarchy { id name } sales_size "
    "sales_uom_description retail_price price_range { minimum_price { final_price "
    "{ currency value } } } } total_count } }"
)


class TraderJoesClient:
    def __init__(
        self,
        *,
        store_code: str = DEFAULT_STORE_CODE,
        min_request_interval: float = 1.5,
        channel: str = "msedge",
        headless: bool = True,
    ) -> None:
        self.store_code = store_code
        self._min_interval = min_request_interval
        self._last_request = 0.0
        self._channel = channel
        self._headless = headless
        self._pw = self._browser = self._ctx = self._page = None

    def __enter__(self) -> "TraderJoesClient":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            channel=self._channel,
            headless=self._headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._ctx = self._browser.new_context(
            user_agent=_REAL_UA, viewport={"width": 1366, "height": 900}
        )
        self._page = self._ctx.new_page()
        # Warm Akamai cookies with a real page load before hitting the API.
        self._page.goto(TJ_HOME, wait_until="domcontentloaded", timeout=60000)
        self._page.wait_for_timeout(3000)
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def fetch_category_page(self, category_id: int, *, page: int = 1, page_size: int = 100):
        """Fetch one page of a category. Returns a Playwright APIResponse."""
        self._throttle()
        return self._page.request.post(
            TJ_GRAPHQL,
            data={
                "operationName": "SearchProducts",
                "variables": {
                    "categoryId": category_id,
                    "currentPage": page,
                    "pageSize": page_size,
                    "storeCode": self.store_code,
                    "availability": "1",
                    "published": "1",
                },
                "query": _SEARCH_QUERY,
            },
        )

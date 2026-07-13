"""Krazy Coupon Lady (KCL) aggregator fetcher — Aldi deals.

Aldi's own storefront hard-blocks scraping (Instacart, strict robots), so per
the spec we read Aldi deals from KCL, which serves structured deal cards.

robots.txt for thekrazycouponlady.com (User-agent: *) DISALLOWS: /tag, /api/,
/search, /webview, and any URL with a ?...kclt=... tracking param. Deal content
pages such as /coupons-for/<store> are allowed. We honor a >=5s crawl-delay
(their stated delay for other bots) and never touch /api/.
"""

from __future__ import annotations

import httpx

from .fetcher import PoliteClient

KCL_BASE = "https://thekrazycouponlady.com"
# Path prefixes disallowed by robots.txt — never fetch these.
DISALLOWED_PREFIXES = ("/tag", "/api/", "/search", "/webview")


class KCLRobotsError(RuntimeError):
    """Raised when a request would violate KCL's robots.txt."""


def _assert_allowed(path: str) -> None:
    if any(path.startswith(prefix) for prefix in DISALLOWED_PREFIXES):
        raise KCLRobotsError(f"path '{path}' is disallowed by KCL robots.txt")
    if "kclt=" in path:
        raise KCLRobotsError("URLs with the kclt tracking param are disallowed")


class KCLClient(PoliteClient):
    def __init__(self, *, min_request_interval: float = 5.0, **kwargs) -> None:
        # Default 5s honors KCL's stated crawl-delay for bots.
        super().__init__(min_request_interval=min_request_interval, **kwargs)

    def get_store_deals(self, store: str = "aldi") -> httpx.Response:
        """Fetch the deals page for a store (default Aldi)."""
        path = f"/coupons-for/{store}"
        _assert_allowed(path)
        return self.get(KCL_BASE + path)

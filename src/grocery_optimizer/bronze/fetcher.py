"""Polite HTTP client base for scraping fetchers (Lidl, KCL aggregator).

Applies an honest, identifiable User-Agent and a minimum inter-request
interval. Respect robots.txt is enforced per-source in the subclasses; this
base only provides transport + throttling. Returns raw ``httpx.Response`` so
callers preserve verbatim bytes to bronze before parsing.
"""

from __future__ import annotations

import time

import httpx

from .. import __version__

# Honest, identifiable UA — we are not pretending to be a browser or GPTBot.
DEFAULT_USER_AGENT = (
    f"grocery-optimizer/{__version__} "
    "(personal grocery price research; "
    "+https://github.com/hemangnagar/grocery_optimizer)"
)


class PoliteClient:
    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        min_request_interval: float = 2.0,
        timeout: float = 30.0,
        extra_headers: dict | None = None,
    ) -> None:
        self._min_interval = min_request_interval
        self._last_request_monotonic = 0.0
        headers = {"User-Agent": user_agent, "Accept": "*/*"}
        if extra_headers:
            headers.update(extra_headers)
        self._http = httpx.Client(
            timeout=timeout, headers=headers, follow_redirects=True
        )

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request_monotonic)
        if wait > 0:
            time.sleep(wait)
        self._last_request_monotonic = time.monotonic()

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        self._throttle()
        return self._http.get(url, params=params)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

"""Kroger Developer API client.

Official, free API using OAuth2 client-credentials. Used for gold-standard
location-filtered pricing. The key question this supports (Build Step 2): are
Harris Teeter banner locations exposed, or only Kroger-banner stores?

Docs: https://developer.kroger.com/  (Locations + Products/Catalog APIs)
The client returns raw ``httpx.Response`` objects so callers can preserve the
verbatim bytes to bronze before parsing.
"""

from __future__ import annotations

import base64
import time

import httpx

DEFAULT_BASE_URL = "https://api.kroger.com/v1"
_TOKEN_PATH = "/connect/oauth2/token"
# product.compact is the client-credentials scope that unlocks product data
# (including location-filtered pricing).
DEFAULT_SCOPE = "product.compact"


class KrogerAuthError(RuntimeError):
    """Raised when credentials are missing or the token request fails."""


class KrogerClient:
    """Thin, polite client over the Kroger API.

    Handles the client-credentials token dance (with in-memory caching and
    refresh) and applies a minimum inter-request interval to stay well within
    rate limits.
    """

    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        scope: str = DEFAULT_SCOPE,
        min_request_interval: float = 0.34,
        timeout: float = 30.0,
    ) -> None:
        if not client_id or not client_secret:
            raise KrogerAuthError(
                "Kroger credentials missing. Set KROGER_CLIENT_ID and "
                "KROGER_CLIENT_SECRET in the project .env "
                "(register at https://developer.kroger.com/)."
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.scope = scope
        self._min_interval = min_request_interval
        self._http = httpx.Client(timeout=timeout, headers={"Accept": "application/json"})
        self._token: str | None = None
        self._token_expiry_monotonic: float = 0.0
        self._last_request_monotonic: float = 0.0

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "KrogerClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- internals -------------------------------------------------------
    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request_monotonic)
        if wait > 0:
            time.sleep(wait)
        self._last_request_monotonic = time.monotonic()

    def _ensure_token(self) -> str:
        # Refresh 30s before expiry to avoid edge-of-expiry 401s.
        if self._token and time.monotonic() < self._token_expiry_monotonic - 30:
            return self._token
        basic = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        resp = self._http.post(
            self.base_url + _TOKEN_PATH,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": self.scope},
        )
        if resp.status_code != 200:
            raise KrogerAuthError(
                f"token request failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry_monotonic = time.monotonic() + int(
            payload.get("expires_in", 1800)
        )
        return self._token

    # -- requests --------------------------------------------------------
    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        """Authenticated GET. Returns the raw response (may be non-2xx)."""
        token = self._ensure_token()
        self._throttle()
        return self._http.get(
            self.base_url + path,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )

    def get_locations(
        self,
        zip_code: str | int,
        *,
        radius_miles: int = 15,
        chain: str | None = None,
        limit: int = 50,
    ) -> httpx.Response:
        """Stores near a ZIP. Optionally filter by chain/banner (e.g. HARRISTEETER)."""
        params: dict = {
            "filter.zipCode.near": str(zip_code),
            "filter.radiusInMiles": radius_miles,
            "filter.limit": limit,
        }
        if chain:
            params["filter.chain"] = chain
        return self.get("/locations", params)

    def get_products(
        self,
        term: str,
        *,
        location_id: str,
        limit: int = 10,
        start: int = 0,
    ) -> httpx.Response:
        """Products matching ``term`` with pricing for a specific location.

        The Kroger API is search-based (no full-catalog dump): max 50 per page,
        paginated via ``start`` up to a 250-result ceiling per term.
        """
        params = {
            "filter.term": term,
            "filter.locationId": location_id,
            "filter.limit": limit,
        }
        if start:
            params["filter.start"] = start
        return self.get("/products", params)

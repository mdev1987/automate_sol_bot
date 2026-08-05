"""Jupiter Price API client — a reliable USD fallback when DexScreener is stale.

`GET /price/v3?ids={mints}` returns heuristic-filtered USD prices for up to
50 tokens. It's used as the *fallback* price source during position
monitoring: DexScreener remains primary, but a token with no fresh
DexScreener pair (e.g. between bonding-curve migration and AMM indexing)
can still be priced via Jupiter.

The client requires the ``x-api-key`` header (from ``JUPITER_API_KEY``) and
is resilient: transient HTTP errors trigger a short backoff, and a token
that simply has no reliable price returns ``None`` (never raises).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# Free tier allows ~1 RPS; keep a safe floor so a busy loop never trips 429s.
_MIN_REQUEST_INTERVAL_SEC = 1.1
_RETRY_ATTEMPTS = 3


class JupiterPrice:
    """Async wrapper for ``GET /price/v3``."""

    def __init__(self, settings: Settings) -> None:
        self._base = settings.jupiter_api
        self._headers = {"accept": "application/json"}
        if settings.jupiter_api_key:
            self._headers["x-api-key"] = settings.jupiter_api_key
        self._client = httpx.AsyncClient(timeout=10.0)
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """Release the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------ throttle
    async def _throttle(self) -> None:
        """Enforce a minimum spacing between requests."""
        async with self._lock:
            wait = self._last_call + _MIN_REQUEST_INTERVAL_SEC - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    # ---------------------------------------------------------------------- get
    async def _get_prices(self, mints: list[str]) -> Optional[dict]:
        """Fetch raw price payload for ``mints``, retrying transient errors."""
        url = f"{self._base}/price/v3"
        params = {"ids": ",".join(mints)}
        for attempt in range(_RETRY_ATTEMPTS):
            await self._throttle()
            try:
                resp = await self._client.get(url, params=params, headers=self._headers)
                if resp.status_code == 200:
                    return resp.json().get("data") or {}
                log.debug(
                    "Jupiter price -> HTTP %s (attempt %d)", resp.status_code, attempt + 1
                )
            except httpx.HTTPError as exc:
                log.debug("Jupiter price request error: %s", exc)
            await asyncio.sleep(2**attempt)
        return None

    # ---------------------------------------------------------------- public API
    async def get_prices(self, mints: list[str]) -> dict[str, float]:
        """USD price per mint; mints without a reliable price are omitted."""
        mints = [m for m in mints if m]
        if not mints:
            return {}
        data = await self._get_prices(mints)
        if not data:
            return {}
        return {
            mint: float(info["usdPrice"])
            for mint, info in data.items()
            if isinstance(info, dict) and info.get("usdPrice") is not None
        }

    async def get_price(self, mint: str) -> Optional[float]:
        """USD price for a single mint (``None`` when unavailable)."""
        prices = await self.get_prices([mint])
        return prices.get(mint)
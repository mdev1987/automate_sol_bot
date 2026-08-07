"""DexScreener REST client: pair data (liquidity, volume, txns) and prices.

Used for two purposes:

1. **Scanner** — after PumpDev announces a new launch, we wait for
   DexScreener to index the pair (~25s) and then read liquidity, 5-minute
   volume, and buy/sell counts to decide if the token qualifies.
2. **Price monitoring** — the exit loop polls the best pair's ``priceUsd``
   to trigger take-profit / stop-loss / dead-pool exits.

The client is **rate-limit aware** (60 req/min public tier) by throttling
every request to a minimum spacing, and **resilient** (retries with backoff
on 429/5xx, returns ``None`` instead of raising on transient failures).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# Public-tier hard limit is 60 req/min; stay safely below it.
_MIN_REQUEST_INTERVAL_SEC = 1.1
_RETRY_ATTEMPTS = 3

# Upper bound for the buy/sell ratio (see ``Pair.buy_sell_ratio``).
_RATIO_CAP = 10.0


@dataclass(frozen=True)
class Pair:
    """Normalised subset of a DexScreener pair that the strategy needs."""

    address: str
    dex_id: str
    base_symbol: str
    quote_symbol: str
    price_usd: Optional[float]
    price_native: Optional[float]
    liquidity_usd: float
    volume_m5: float
    buys_m5: int
    sells_m5: int
    market_cap: Optional[float]
    fdv: Optional[float]
    pair_created_at_ms: Optional[int]

    @property
    def txns_m5(self) -> int:
        """Total number of trades in the last 5 minutes."""
        return self.buys_m5 + self.sells_m5

    @property
    def buy_sell_ratio(self) -> float:
        """Buy/sell ratio, capped so a zero-sell (or near-zero) launch can't
        report an inflated, unbounded value that dominates filters/scoring."""
        if self.sells_m5 <= 0:
            return float(min(self.buys_m5, _RATIO_CAP))
        return min(self.buys_m5 / self.sells_m5, _RATIO_CAP)

    def is_dead(self, threshold_usd: float) -> bool:
        """True when liquidity collapsed below ``threshold_usd``."""
        return self.liquidity_usd <= threshold_usd


class DexScreener:
    """Async REST wrapper with throttling and retry-on-flaky-responses."""

    def __init__(self, settings: Settings) -> None:
        self._base = settings.dex_screener_api
        self._client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """Release the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------ throttle
    async def _throttle(self) -> None:
        """Enforce a minimum spacing between requests (rate-limit guard)."""
        async with self._lock:
            wait = self._last_call + _MIN_REQUEST_INTERVAL_SEC - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    # ---------------------------------------------------------------------- get
    async def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """Perform a throttled GET, retrying transient failures with backoff.

        Returns the JSON body, or ``None`` when the request ultimately failed
        (callers treat ``None`` as "data not ready yet").
        """
        url = f"{self._base}{path}"
        for attempt in range(_RETRY_ATTEMPTS):
            await self._throttle()
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 404:
                    return None  # not indexed yet — the common case right after launch
                log.debug(
                    "DexScreener %s -> HTTP %s (attempt %d)",
                    url,
                    resp.status_code,
                    attempt + 1,
                )
            except httpx.HTTPError as exc:
                log.debug("DexScreener request error: %s", exc)
            await asyncio.sleep(2**attempt)  # 1s, 2s, 4s
        return None

    # -------------------------------------------------------------- pair parsing
    @staticmethod
    def _parse_pair(data: dict) -> Optional[Pair]:
        """Build a :class:`Pair` from a raw DexScreener pair dict."""
        base = data.get("baseToken", {}) or {}
        quote = data.get("quoteToken", {}) or {}
        txns = data.get("txns", {}).get("m5", {}) or {}
        volume = data.get("volume", {}) or {}
        liquidity = data.get("liquidity", {}) or {}

        def fnum(value) -> Optional[float]:
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        try:
            pair = Pair(
                address=str(data.get("pairAddress", "")),
                dex_id=str(data.get("dexId", "")),
                base_symbol=str(base.get("symbol", "")),
                quote_symbol=str(quote.get("symbol", "")),
                price_usd=fnum(data.get("priceUsd")),
                price_native=fnum(data.get("priceNative")),
                liquidity_usd=fnum(liquidity.get("usd")) or 0.0,
                volume_m5=fnum(volume.get("m5")) or 0.0,
                buys_m5=int(txns.get("buys", 0) or 0),
                sells_m5=int(txns.get("sells", 0) or 0),
                market_cap=fnum(data.get("marketCap")),
                fdv=fnum(data.get("fdv")),
                pair_created_at_ms=fnum(data.get("pairCreatedAt")),
            )
        except (TypeError, ValueError):
            log.warning("Could not parse DexScreener pair: %s", data.get("pairAddress"))
            return None
        return pair

    # ---------------------------------------------------------------- public API
    async def get_pairs(self, mint: str) -> list[Pair]:
        """Return every pair DexScreener knows for ``mint`` (best first).

        Pairs are sorted by liquidity, highest first, so the caller can pick
        the deepest market.
        """
        data = await self._get(f"/latest/dex/tokens/{mint}")
        if not data or not isinstance(data.get("pairs"), list):
            return []
        pairs = [p for p in (self._parse_pair(d) for d in data["pairs"]) if p]
        pairs.sort(key=lambda p: p.liquidity_usd, reverse=True)
        return pairs

    async def best_pair(self, mint: str) -> Optional[Pair]:
        """The most liquid pair for ``mint`` (or ``None`` if not indexed)."""
        pairs = await self.get_pairs(mint)
        return pairs[0] if pairs else None

    async def price_usd(self, mint: str) -> Optional[float]:
        """Best-available USD price for ``mint`` from its deepest pair."""
        pair = await self.best_pair(mint)
        return pair.price_usd if pair else None
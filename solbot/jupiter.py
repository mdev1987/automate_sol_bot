"""Jupiter Swap API V2 client — /order + /execute managed swaps.

Two API calls, no RPC needed:

1. ``GET /swap/v2/order``  — Jupiter quotes and assembles a transaction
   (``transaction`` base64 + ``requestId``) with all routing engines
   competing for the best price.
2. ``POST /swap/v2/execute`` — we sign the transaction locally and Jupiter
   lands it with confirmation and retry.

We trade **USDC** only. Amounts are passed in raw base units (USDC has 6
decimals). Buy proceeds are captured from ``/execute``'s raw
``totalOutputAmount`` so we never need the token's decimals to sell later.

**Quote gate** — before ever executing a buy we hit ``/order`` and validate
the assembled route: we reject when there is no usable route, the
``actualOutAmount`` is zero, or price impact exceeds the configured cap. A
new launch often briefly has no route, so we retry with a short delay. All
quote requests are throttled (global rate limit) and briefly cached to
collapse launch bursts, with latency measured to catch the next bottleneck.
Slippage is chosen dynamically from liquidity via configurable tiers.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from .config import USDC_DECIMALS, USDC_MINT, Settings

log = logging.getLogger(__name__)

# Slippage escalation ladder when a sell keeps failing (basis points).
SELL_SLIPPAGE_ESCALATION = (200, 300, 500, 1000)


class JupiterError(RuntimeError):
    """Raised when a swap order/execute fails and cannot be retried."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SwapResult:
    """Outcome of an executed swap."""

    success: bool
    signature: str
    input_amount: int   # raw: what went in
    output_amount: int  # raw: what came out
    error: str = ""


@dataclass(frozen=True)
class QuoteResult:
    """Outcome of a quote-gate call (verified order or a skip reason)."""

    success: bool
    order: Optional[dict]     # valid ``/order`` payload, ready to execute
    input_amount: int         # raw USDC
    output_amount: int        # raw expected out
    price_impact_pct: float
    route_count: int
    latency_ms: float
    reason: str = ""               # see skip taxonomy below
    fetched_at: float = 0.0        # time.monotonic() when the order was fetched

    # Skip taxonomy — the trader counts these, so an API outage is never
    # mistaken for "no route":
    #   "ok"                     verified order, ready to execute
    #   "quote_no_route"         Jupiter found no usable route (400 "No route")
    #   "quote_impact"           route exists but price impact above the cap
    #   "quote_timeout"          the /order request timed out
    #   "quote_http_error"       transport/5xx/other 4xx failure
    #   "quote_invalid_response" 200 but payload unusable (bad JSON, no tx)
    #   "quote_rate_limited"     HTTP 429
    #   "quote_insufficient_funds" taker lacks balance to assemble the order
    #   "quote_exception"        unexpected error while quoting

    @property
    def retryable(self) -> bool:
        """True when a retry could plausibly succeed (route not ready yet)."""
        return self.reason in (
            "quote_no_route", "quote_timeout",
            "quote_http_error", "quote_rate_limited", "quote_exception",
        )


class JupiterSwap:
    """Async wrapper around the managed /order + /execute swap path."""

    def __init__(self, settings: Settings) -> None:
        self._base = settings.jupiter_api
        self._headers = {"accept": "application/json"}
        if settings.jupiter_api_key:
            self._headers["x-api-key"] = settings.jupiter_api_key
        self._slippage_bps = settings.slippage_bps
        self._qcfg = settings.quote
        self._keypair: Optional[Keypair] = settings.keypair
        if self._keypair is None and settings.dry_run:
            # Paper mode with no real wallet: derive a throwaway keypair purely
            # so the quote-gate runs (/order needs a taker pubkey). It never
            # signs or executes anything.
            self._keypair = Keypair()
            log.info("PAPER QUOTE KEYPAIR ACTIVE pubkey=%s execution=disabled "
                     "(throwaway key; never signs)", self._keypair.pubkey())
        # In paper mode (no real wallet) the throwaway key has no balance, so
        # /order would always fail with "Insufficient funds". Omit the taker
        # instead: /order returns the quote with no transaction (docs), which
        # validates tradability without any funded wallet.
        self._paper_quoting = settings.keypair is None and settings.dry_run
        self._client = httpx.AsyncClient(timeout=20.0)

        # -- quote gate state -------------------------------------------------
        self._quote_lock = asyncio.Lock()
        self._next_quote_ts: float = 0.0
        self._quote_cache: dict = {}          # (mint, amount, slippage) -> (ts, result)
        self._qstats: dict[str, int] = {
            "quotes": 0, "ok": 0,
            "quote_no_route": 0, "quote_impact": 0,
            "quote_timeout": 0, "quote_http_error": 0,
            "quote_invalid_response": 0, "quote_rate_limited": 0,
            "quote_insufficient_funds": 0,
            "quote_exception": 0,
        }
        self._lat_sum = 0.0
        self._lat_count = 0
        self._lat_max = 0.0

    async def close(self) -> None:
        """Release the underlying HTTP client."""
        await self._client.aclose()

    @property
    def ready(self) -> bool:
        """True when we can quote/sign; paper mode uses a throwaway keypair."""
        return self._keypair is not None

    def quote_summary(self) -> str:
        """One-line quote-gate + latency summary."""
        q = self._qstats
        avg = self._lat_sum / self._lat_count if self._lat_count else 0.0
        return (
            f"quotes quotes={q['quotes']} ok={q['ok']} "
            f"no_route={q['quote_no_route']} impact={q['quote_impact']} "
            f"timeout={q['quote_timeout']} http={q['quote_http_error']} "
            f"invalid={q['quote_invalid_response']} "
            f"no_funds={q['quote_insufficient_funds']} "
            f"rate_limit={q['quote_rate_limited']} exc={q['quote_exception']} "
            f"latency avg={avg:.0f}ms max={self._lat_max:.0f}ms"
        )

    # ------------------------------------------------------------------- order
    async def _order(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int,
        taker: Optional[str] = None,
    ) -> dict:
        """Request a swap quote (and, with a taker, an assembled transaction).

        Live mode passes ``taker`` so Jupiter builds an executable transaction;
        paper mode omits it, which returns just the quote (transaction null) —
        tradability without needing a funded wallet, and without the
        "Insufficient funds" failures an empty throwaway taker triggers.
        """
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage_bps,
        }
        if taker is not None:
            params["taker"] = str(taker)
        resp = await self._client.get(
            f"{self._base}/swap/v2/order", params=params, headers=self._headers
        )
        if resp.status_code != 200:
            raise JupiterError(
                f"order HTTP {resp.status_code}: {resp.text[:200]}",
                status=resp.status_code,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise JupiterError(
                f"order invalid JSON: {resp.text[:200]}", status=200
            ) from exc
        transaction = data.get("transaction")
        if taker is not None and not transaction:
            raise JupiterError(
                f"order failed: {data.get('errorMessage') or data.get('error') or data}",
                status=200,
            )
        return data

    # ---------------------------------------------------------------- signing
    def _sign(self, b64_transaction: str) -> str:
        """Sign a base64 transaction with the wallet keypair; return base64."""
        if self._keypair is None:
            raise JupiterError("no wallet key configured; cannot sign (dry-run?)")
        raw = base64.b64decode(b64_transaction)
        tx = VersionedTransaction.from_bytes(raw)
        if any(sig != b"\x00" * 64 for sig in tx.signatures):
            log.warning("transaction already partially signed")
        signature = self._keypair.sign_message(tx.message.serialize())
        signed = VersionedTransaction.populate(tx.message, [signature])
        return base64.b64encode(bytes(signed)).decode()

    # ----------------------------------------------------------------- execute
    async def execute(self, order: dict) -> SwapResult:
        """POST the signed transaction to /execute managed landing."""
        signed = self._sign(order["transaction"])
        body = {
            "signedTransaction": signed,
            "requestId": order.get("requestId", ""),
        }
        resp = await self._client.post(
            f"{self._base}/swap/v2/execute", json=body, headers=self._headers
        )
        data = resp.json() if resp.content else {}
        if resp.status_code != 200 or data.get("status") != "Success":
            return SwapResult(
                success=False,
                signature=data.get("signature", ""),
                input_amount=int(data.get("totalInputAmount") or 0),
                output_amount=int(data.get("totalOutputAmount") or 0),
                error=data.get("error") or f"execute HTTP {resp.status_code}",
            )
        return SwapResult(
            success=True,
            signature=data.get("signature", ""),
            input_amount=int(data.get("totalInputAmount") or 0),
            output_amount=int(data.get("totalOutputAmount") or 0),
        )

    # ------------------------------------------------------------------- quote
    async def _quote_slot(self) -> None:
        """Throttle all quote requests to the configured per-second rate."""
        interval = 1.0 / max(self._qcfg.rate_per_sec, 0.1)
        async with self._quote_lock:
            now = time.monotonic()
            wait = self._next_quote_ts - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_quote_ts = now + interval

    def _record_latency(self, ms: float) -> None:
        self._lat_sum += ms
        self._lat_count += 1
        self._lat_max = max(self._lat_max, ms)

    def _classify_error(self, exc: JupiterError) -> str:
        """Map a Jupiter order failure to a skip-taxonomy reason."""
        st = exc.status
        if "insufficient" in str(exc).lower():
            # /order succeeded HTTP-wise, but the (live) taker lacks balance
            # (USDC / SOL / ATA) to assemble the transaction.
            return "quote_insufficient_funds"
        if st == 429:
            return "quote_rate_limited"
        if st and 500 <= st < 600:
            return "quote_http_error"
        if st and 400 <= st < 500:
            # 400 with "no route" means Jupiter simply can't route the trade;
            # anything else is an API/param problem we must not mask.
            return "quote_no_route" if "route" in str(exc).lower() else "quote_http_error"
        if "route" in str(exc).lower():
            return "quote_no_route"
        return "quote_invalid_response"

    async def _do_quote(self, mint: str, amount_raw: int, slippage_bps: int) -> QuoteResult:
        """Fetch one order and validate it against the quote-gate rules."""
        self._qstats["quotes"] += 1
        await self._quote_slot()
        t0 = time.monotonic()
        try:
            if self._paper_quoting:
                order = await self._order(USDC_MINT, mint, amount_raw, slippage_bps)
            else:
                order = await self._order(
                    USDC_MINT, mint, amount_raw, slippage_bps,
                    str(self._keypair.pubkey()),
                )
        except httpx.TimeoutException as exc:
            reason = "quote_timeout"
            self._qstats[reason] += 1
            log.warning("quote timeout for %s: %s", mint, exc)
            return QuoteResult(False, None, amount_raw, 0, 0.0, 0, 0.0, reason)
        except JupiterError as exc:
            reason = self._classify_error(exc)
            self._qstats[reason] += 1
            log.info("quote %s for %s: %s", reason, mint, exc)
            return QuoteResult(False, None, amount_raw, 0, 0.0, 0, 0.0, reason)
        except httpx.RequestError as exc:
            self._qstats["quote_http_error"] += 1
            log.warning("quote http error for %s: %s", mint, exc)
            return QuoteResult(False, None, amount_raw, 0, 0.0, 0, 0.0, "quote_http_error")
        except Exception as exc:  # noqa: BLE001
            self._qstats["quote_exception"] += 1
            log.exception("quote exception for %s: %s", mint, exc)
            return QuoteResult(False, None, amount_raw, 0, 0.0, 0, 0.0, "quote_exception")

        latency_ms = (time.monotonic() - t0) * 1000
        self._record_latency(latency_ms)
        fetched_at = time.monotonic()

        out = int(order.get("outAmount") or order.get("actualOutAmount") or 0)
        # priceImpact is in percentage points (e.g. -0.1 == -0.1%); the old
        # priceImpactPct is a decimal ratio, so scale it up to match.
        impact = abs(float(order.get("priceImpact") or 0.0))
        if impact == 0.0 and order.get("priceImpactPct"):
            impact = abs(float(order["priceImpactPct"])) * 100.0
        route = order.get("routePlan") or order.get("routes") or []
        if out <= 0 or not route:
            self._qstats["quote_no_route"] += 1
            return QuoteResult(False, order, amount_raw, out, impact, len(route), latency_ms, "quote_no_route", fetched_at)
        if impact > self._qcfg.max_price_impact_pct:
            self._qstats["quote_impact"] += 1
            return QuoteResult(False, order, amount_raw, out, impact, len(route), latency_ms, "quote_impact", fetched_at)
        self._qstats["ok"] += 1
        return QuoteResult(True, order, amount_raw, out, impact, len(route), latency_ms, "ok", fetched_at)

    async def quote(
        self,
        mint: str,
        amount_raw: int,
        liquidity_usd: float = 0.0,
        force: bool = False,
    ) -> Optional[QuoteResult]:
        """Verify tradability for ``mint`` and return a ready-to-execute order.

        Chooses slippage from ``liquidity_usd`` tiers, retries "no route"
        briefly (new launches race their liquidity), and caches the result
        briefly to collapse simultaneous evaluations of the same token.
        ``force=True`` bypasses the cache (used when a cached quote is stale).
        """
        slippage = self._qcfg.slippage_for(max(liquidity_usd, 0.0))
        key = (mint, amount_raw, slippage)

        if not force:
            now = time.monotonic()
            cached = self._quote_cache.get(key)
            if cached and now - cached[0] < self._qcfg.cache_ttl_sec:
                return cached[1]

        result: Optional[QuoteResult] = None
        for attempt in range(max(self._qcfg.retries, 1)):
            result = await self._do_quote(mint, amount_raw, slippage)
            if result.success or not result.retryable:
                break
            log.info("quote %s for %s (attempt %d)", result.reason, mint, attempt + 1)
            if attempt + 1 < self._qcfg.retries:
                await asyncio.sleep(self._qcfg.retry_delay_sec)

        if result is not None:
            self._quote_cache[key] = (time.monotonic(), result)
        return result

    # ------------------------------------------------------------- high level
    async def buy(self, mint: str, amount_usdc: float, liquidity_usd: float = 0.0) -> SwapResult:
        """Buy ``amount_usdc`` worth of ``mint`` (USDC in), via a verified quote."""
        amount_raw = int(amount_usdc * (10 ** USDC_DECIMALS))
        quote = await self.quote(mint, amount_raw, liquidity_usd)
        if quote is None or not quote.success:
            return SwapResult(False, "", amount_raw, 0, quote.reason if quote else "no quote")
        if not (quote.order and quote.order.get("transaction")):
            # Paper quoting (no taker) returns a verified route but no
            # transaction — there is nothing to sign or execute.
            return SwapResult(False, "", amount_raw, 0, "paper quote: no transaction to execute")
        return await self.execute(quote.order)

    async def sell(self, mint: str, amount_raw: int) -> SwapResult:
        """Sell ``amount_raw`` of ``mint`` for USDC, escalating slippage."""
        last: Optional[SwapResult] = None
        for slippage in (self._slippage_bps,) + SELL_SLIPPAGE_ESCALATION:
            try:
                order = await self._order(
                    mint, USDC_MINT, amount_raw, slippage,
                    str(self._keypair.pubkey()),
                )
            except JupiterError as exc:
                log.warning("sell order @%dbps failed: %s", slippage, exc)
                last = SwapResult(False, "", amount_raw, 0, str(exc))
                continue
            result = await self.execute(order)
            if result.success:
                return result
            last = result
            log.warning("sell execute @%dbps failed: %s", slippage, result.error)
            # Keep climbing the ladder on any failure — a wider slippage bound
            # can still land on a thin book, and a failed exit is worse than
            # a slightly worse fill. Only the final rung stops.
            if slippage >= 1000:
                break
        return last or SwapResult(False, "", amount_raw, 0, "sell failed")
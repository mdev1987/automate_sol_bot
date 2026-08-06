"""Candidate selection pipeline (the "scanner").

The scanner turns raw PumpDev launch frames into qualified buy signals:

    create frame -> wait for DexScreener to index the pair -> filter ->
    rug-check -> score -> emit Signal

DexScreener only publishes a pair a couple of seconds after a Pump.fun
launch (typically ~25s), so each candidate waits (with polling) for its
pair to appear before anything else happens. Launches are evaluated
concurrently, bounded by a semaphore so one slow candidate can't stall a
newer, hotter one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .config import Settings
from .dex_screener import DexScreener, Pair
from .rugcheck import check_token
from .scoring import TokenScore, score_token

log = logging.getLogger(__name__)

# Poll DexScreener this often while waiting for a pair to be indexed.
PAIR_POLL_SEC = 3.0
# Hard cap on how long one candidate may wait for its pair.
MAX_SCAN_WINDOW_SEC = 15 * 60  # 15 minutes, per the strategy notes
# Maximum number of launches evaluated at once.
MAX_CONCURRENT_EVALUATIONS = 5

# How much older than the age filter we allow before dropping a create
# before it is even evaluated (keeps the queue free of zombies).
_MAX_STALE_HANDLE = 2.0

# How often the scanner self-reports its funnel counters (seconds).
FUNNEL_REPORT_SEC = 300.0

@dataclass(frozen=True)
class Signal:
    """A qualified, scored candidate that a trader may act on."""

    mint: str
    name: str
    symbol: str
    pair: Pair
    score: TokenScore
    rug_verdict: str
    age_seconds: float
    create: dict  # the original launch event (for audit / reporting)


class Scanner:
    """Consumes Pump events and emits :class:`Signal` objects."""

    def __init__(self, settings: Settings, dex: DexScreener) -> None:
        self._settings = settings
        self._thresholds = settings.scanner
        self._dex = dex
        self._sem = asyncio.Semaphore(MAX_CONCURRENT_EVALUATIONS)
        self._out_queue: Optional["asyncio.Queue[Signal]"] = None
        # Funnel counters: where candidates go before becoming a signal.
        self._stats = {"events": 0, "stale": 0, "no_pair": 0,
                       "filtered": 0, "rug_blocked": 0, "signals": 0}
        self._next_report = time.monotonic() + FUNNEL_REPORT_SEC

    def _bump(self, key: str) -> None:
        self._stats[key] = self._stats.get(key, 0) + 1

    def counters_summary(self) -> str:
        """One-line funnel summary (events -> ... -> signals)."""
        s = self._stats
        return (
            f"funnel events={s['events']} stale={s['stale']} "
            f"no_pair={s['no_pair']} filtered={s['filtered']} "
            f"rug_blocked={s['rug_blocked']} signals={s['signals']}"
        )

    # ------------------------------------------------------------------ run
    async def run(self, in_queue: "asyncio.Queue[dict]", out_queue: "asyncio.Queue[Signal]") -> None:
        """Start scanning; consume events from ``in_queue`` and emit signals."""
        self._out_queue = out_queue
        log.info("scanner started")
        while True:
            event = await in_queue.get()
            if event.get("txType") != "create":
                continue
            self._bump("events")
            created_ms = event.get("timestamp") or time.time() * 1000
            age = time.time() - created_ms / 1000
            if age > self._thresholds.max_age_seconds * _MAX_STALE_HANDLE:
                self._bump("stale")
                log.debug("dropping stale create for %s", event.get("mint"))
                continue
            task = asyncio.create_task(self._evaluate(event))
            task.add_done_callback(self._on_done)

            if time.monotonic() >= self._next_report:
                self._next_report += FUNNEL_REPORT_SEC
                log.info("%s", self.counters_summary())

    def _on_done(self, task: "asyncio.Task") -> None:
        """Surface unexpected errors from an evaluation task (ignore cancels)."""
        try:
            task.result()
        except asyncio.CancelledError:
            pass  # normal during shutdown
        except Exception as exc:  # noqa: BLE001
            log.exception("scanner evaluation failed: %s", exc)

    # ------------------------------------------------------------ evaluation
    async def _evaluate(self, create: dict) -> Optional[Signal]:
        """Evaluate one candidate under the concurrency bound."""
        async with self._sem:
            since = time.monotonic()
            pair = await self._wait_for_pair(create.get("mint", ""))
            if pair is None:
                self._bump("no_pair")
                log.info("NO_PAIR %s (%s): not indexed in %.0fs",
                         create.get("symbol"), create.get("mint"),
                         time.monotonic() - since)
                return None
            signal = self._qualify(create, pair)
            if signal is not None and self._out_queue is not None:
                await self._out_queue.put(signal)
            return signal

    async def _wait_for_pair(self, mint: str) -> Optional[Pair]:
        """Poll DexScreener until the best pair appears, or the window ends."""
        deadline = time.monotonic() + MAX_SCAN_WINDOW_SEC
        while time.monotonic() < deadline:
            pair = await self._dex.best_pair(mint)
            if pair and pair.liquidity_usd > 0:
                return pair
            await asyncio.sleep(PAIR_POLL_SEC)
        return None

    # ---------------------------------------------------------------- qualify
    def _qualify(self, create: dict, pair: Pair) -> Optional[Signal]:
        """Run filters + rug check + score; return a signal or ``None``."""
        mint = create.get("mint") or ""
        name = str(create.get("name") or "")
        symbol = str(create.get("symbol") or "")

        created_ms = create.get("timestamp") or time.time() * 1000
        age_seconds = time.time() - created_ms / 1000

        if reason := self._filter_reason(pair, age_seconds):
            self._bump("filtered")
            log.info("FILTERED %s (%s): %s liq=$%.0f vol=$%.0f txns=%d "
                     "buys=%d ratio=%.2f mcap=$%.0f",
                     symbol, mint, reason, pair.liquidity_usd, pair.volume_m5,
                     pair.txns_m5, pair.buys_m5, pair.buy_sell_ratio,
                     pair.market_cap or 0.0)
            return None

        rug = check_token(create, pair)
        if rug.verdict == "Rug":
            self._bump("rug_blocked")
            log.info("rug-blocked %s (%s): %s", symbol, mint, rug.flags)
            return None

        dev_sol = float(create.get("initialQuoteAmount") or create.get("solAmount") or 0.0)
        s = score_token(
            age_seconds=age_seconds,
            volume_5m=pair.volume_m5,
            buy_ratio=pair.buy_sell_ratio,
            dev_sol=dev_sol,
            liquidity_usd=pair.liquidity_usd,
        )
        self._bump("signals")
        log.info("QUALIFIED %s (%s) score=%.1f", symbol, mint, s.total)
        return Signal(
            mint=mint,
            name=name,
            symbol=symbol,
            pair=pair,
            score=s,
            rug_verdict=rug.verdict,
            age_seconds=age_seconds,
            create=create,
        )

    def _filter_reason(self, pair: Pair, age_seconds: float) -> Optional[str]:
        """Name of the first threshold the pair fails, or ``None`` if it passes."""
        t = self._thresholds
        market_cap = pair.market_cap if pair.market_cap is not None else pair.fdv or 0.0
        if age_seconds > t.max_age_seconds:
            return "age"
        if pair.liquidity_usd < t.min_liquidity_usd:
            return "liquidity_low"
        if pair.liquidity_usd > t.max_liquidity_usd:
            return "liquidity_high"
        if pair.txns_m5 < t.min_txns_5m:
            return "txns"
        if pair.buys_m5 < t.min_buys_5m:
            return "buys"
        if pair.buy_sell_ratio < t.min_buy_sell_ratio:
            return "ratio"
        if pair.volume_m5 < t.min_volume_5m:
            return "volume"
        if market_cap > t.max_market_cap:
            return "mcap"
        return None
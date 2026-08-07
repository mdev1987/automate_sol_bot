"""Candidate selection pipeline (the "scanner").

The scanner turns raw PumpDev launch frames into qualified buy signals:

    create frame -> (best-effort DexScreener pair) -> filter ->
    rug-check -> score -> emit Signal

DexScreener is now only a **supplemental** source, not the gate. We wait a
short, bounded ``max_scan_window_sec`` (default 30s) for a pair; if none
appears the candidate still goes forward to the Jupiter quote-gate, which —
not DexScreener — is the authority on tradability. A bounded pending
queue feeds a fixed worker pool so memory and concurrency stay
deterministic under launch bursts.
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

# How much older than the age filter we allow before dropping a create
# before it is even evaluated (keeps the queue free of zombies).
_MAX_STALE_HANDLE = 2.0

# How often the scanner self-reports its funnel counters (seconds).
FUNNEL_REPORT_SEC = 300.0

@dataclass(frozen=True)
class Signal:
    """A qualified candidate that a trader may act on.

    ``pair`` is optional: a token with no DexScreener pair yet still emits a
    signal and is left to the Jupiter quote-gate to decide tradability.
    """

    mint: str
    name: str
    symbol: str
    pair: Optional[Pair]
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
        self._out_queue: Optional["asyncio.Queue[Signal]"] = None
        # Funnel counters: where candidates go before becoming a signal.
        self._stats: dict[str, int] = {
            "events": 0, "stale": 0, "dropped": 0, "no_pair": 0,
            "filtered": 0, "rug_blocked": 0, "signals": 0,
        }
        self._next_report = time.monotonic() + FUNNEL_REPORT_SEC

    def _bump(self, key: str) -> None:
        self._stats[key] = self._stats.get(key, 0) + 1

    def counters_summary(self) -> str:
        """One-line funnel summary (events -> ... -> signals)."""
        s = self._stats
        return (
            f"funnel events={s['events']} stale={s['stale']} "
            f"dropped={s['dropped']} no_pair={s['no_pair']} "
            f"filtered={s['filtered']} rug_blocked={s['rug_blocked']} "
            f"signals={s['signals']}"
        )

    # ------------------------------------------------------------------ run
    async def run(self, in_queue: "asyncio.Queue[dict]", out_queue: "asyncio.Queue[Signal]") -> None:
        """Consume events from ``in_queue`` and emit signals.

        A **bounded** pending queue feeds a fixed pool of evaluation workers,
        so neither memory nor concurrency can grow without bound under bursts.
        When the queue is full the oldest candidate is dropped for the newest.
        """
        self._out_queue = out_queue
        workers = self._thresholds.max_evaluation_workers
        pending: asyncio.Queue = asyncio.Queue(
            maxsize=self._thresholds.max_pending_evaluations
        )
        self._stats["workers"] = workers
        worker_tasks = [
            asyncio.create_task(self._worker(i, pending)) for i in range(workers)
        ]
        log.info("scanner started: %d workers, pending cap=%d",
                 workers, self._thresholds.max_pending_evaluations)
        try:
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
                self._enqueue(pending, event)

                if time.monotonic() >= self._next_report:
                    self._next_report += FUNNEL_REPORT_SEC
                    log.info("%s", self.counters_summary())
        finally:
            for worker in worker_tasks:
                worker.cancel()
            await asyncio.gather(*worker_tasks, return_exceptions=True)

    def _enqueue(self, pending: "asyncio.Queue", event: dict) -> None:
        """Enqueue a candidate; if full, drop the oldest to make room."""
        try:
            pending.put_nowait(event)
        except asyncio.QueueFull:
            self._bump("dropped")
            try:
                pending.get_nowait()
            except asyncio.QueueEmpty:
                pass
            pending.put_nowait(event)

    async def _worker(self, index: int, pending: "asyncio.Queue") -> None:
        """Pull candidates off the bounded queue and evaluate them forever."""
        try:
            while True:
                create = await pending.get()
                await self._evaluate(create)
        except asyncio.CancelledError:
            pass  # normal during shutdown
        except Exception as exc:  # noqa: BLE001
            log.exception("scanner worker %d failed: %s", index, exc)

    # ------------------------------------------------------------ evaluation
    async def _evaluate(self, create: dict) -> Optional[Signal]:
        """Evaluate one candidate; a missing DexScreener pair is not fatal."""
        pair = await self._pair_liquidity(create.get("mint", ""))
        if pair is None:
            self._bump("no_pair")
            log.info("NO_PAIR %s: no DexScreener pair in %.0fs — "
                     "proceeding to quote-gate (pair optional)",
                     create.get("symbol"),
                     self._thresholds.max_scan_window_sec)
        signal = self._qualify(create, pair)
        if signal is not None and self._out_queue is not None:
            await self._out_queue.put(signal)
        return signal

    async def _pair_liquidity(self, mint: str) -> Optional[Pair]:
        """Best-effort DexScreener lookup within the short scan window."""
        deadline = time.monotonic() + self._thresholds.max_scan_window_sec
        while time.monotonic() < deadline:
            pair = await self._dex.best_pair(mint)
            if pair and pair.liquidity_usd > 0:
                return pair
            await asyncio.sleep(PAIR_POLL_SEC)
        return None

# ---------------------------------------------------------------- qualify
    def _qualify(self, create: dict, pair: Optional[Pair]) -> Optional[Signal]:
        """Run filters + rug check + score; return a signal or ``None``."""
        mint = create.get("mint") or ""
        name = str(create.get("name") or "")
        symbol = str(create.get("symbol") or "")

        created_ms = create.get("timestamp") or time.time() * 1000
        age_seconds = time.time() - created_ms / 1000

        # Market filters need a pair; without one we defer entirely to the
        # Jupiter quote-gate rather than reject on missing DexScreener data.
        if pair is not None and (reason := self._filter_reason(pair, age_seconds)):
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
            volume_5m=pair.volume_m5 if pair else 0.0,
            buy_ratio=pair.buy_sell_ratio if pair else 0.0,
            dev_sol=dev_sol,
            liquidity_usd=pair.liquidity_usd if pair else 0.0,
        )
        self._bump("signals")
        log.info("QUALIFIED %s (%s) score=%.1f pair=%s",
                 symbol, mint, s.total, "yes" if pair else "none")
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

    def _filter_reason(self, pair: Optional[Pair], age_seconds: float) -> Optional[str]:
        """Name of the first threshold the pair fails, or ``None`` if it passes."""
        if pair is None:
            return None  # no DexScreener data to filter on
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
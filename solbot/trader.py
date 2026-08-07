"""Trade execution & position lifecycle — the heart of the bot.

Owns the *bankroll* (in USDC) and decides **when** to enter and **when** to
exit. All the small strategy files (risk management, exit strategy,
compounding) are consolidated here so the module reads top-to-bottom like a
trading plan.

**Risk rules**

- **Bankroll** — the stake is *reserved* at entry (``play_amount`` -> 0); on
  close the proceeds flow back: wins split by the compounding ratio, losses
  return the remainder (so a loss actually reduces the bankroll).
- **Play floor** — if the per-trade amount drops below the floor, reset to
  the starting amount (avoids a death spiral).
- **Loss pause** — after N consecutive losses we cool down for a while
  before taking the next trade (circuit breaker for bad markets).
- **Dead pool** — a position whose liquidity collapses below the threshold
  is exited (or written off) instead of retried forever.
- **Dry run** — no real swaps: positions are simulated with DexScreener /
  Jupiter prices so the whole loop (including exits) can be validated.

**Exit reasons** — ``tp`` (take profit), ``sl`` (stop loss), ``dead``
(dead pool), ``ttl`` (max hold time).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import USDC_DECIMALS, Settings
from .dex_screener import DexScreener
from .jupiter import JupiterSwap, QuoteResult
from .prices import JupiterPrice
from .reporter import TelegramNotifier
from .scanner import Signal

log = logging.getLogger(__name__)

# Exit reasons used everywhere (reporter icons key off these).
EXIT_TP = "tp"
EXIT_SL = "sl"
EXIT_DEAD = "dead"
EXIT_TTL = "ttl"

# How many consecutive failed price reads before a position is force-exited.
_MAX_STALE_PRICE_READS = 3


@dataclass
class Position:
    """A currently open (or just-closed) trade."""

    mint: str
    symbol: str
    entry_price_usd: float
    entry_amount_usd: float
    entry_time: float
    score: float
    risk: str = ""
    token_raw_amount: int = 0       # raw units to sell (live mode)
    token_qty: float = 0.0          # token quantity (paper mode)
    exit_reason: str = ""
    exit_price_usd: float = 0.0
    exit_amount_usd: float = 0.0
    hold_seconds: float = 0.0
    signature: str = ""
    # -- entry context (for analytics) ---------------------------------------
    entry_liquidity_usd: float = 0.0
    entry_age_seconds: float = 0.0
    # -- exit diagnostics ------------------------------------------------------
    max_price_usd: float = 0.0      # highest price seen while holding
    exit_attempts: int = 0           # sell attempts before success / write-off
    last_sell_price: float = 0.0
    last_quote_time: float = 0.0

    @property
    def pnl_usd(self) -> float:
        return self.exit_amount_usd - self.entry_amount_usd

    @property
    def roi_pct(self) -> float:
        return self.pnl_usd / self.entry_amount_usd * 100 if self.entry_amount_usd else 0.0

    @property
    def max_roi_pct(self) -> float:
        """Peak unrealized gain (or ``0.0`` when no peak was observed)."""
        if self.entry_price_usd <= 0:
            return 0.0
        return (self.max_price_usd / self.entry_price_usd - 1.0) * 100

    def to_dict(self) -> dict:
        """Serialize for crash-recovery (see :meth:`Trader.restore_state`)."""
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "entry_price_usd": self.entry_price_usd,
            "entry_amount_usd": self.entry_amount_usd,
            "entry_time": self.entry_time,
            "score": self.score,
            "risk": self.risk,
            "token_raw_amount": self.token_raw_amount,
            "token_qty": self.token_qty,
            "signature": self.signature,
            "entry_liquidity_usd": self.entry_liquidity_usd,
            "entry_age_seconds": self.entry_age_seconds,
            "max_price_usd": self.max_price_usd,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        """Rebuild a position from a journal snapshot."""
        return cls(
            mint=str(data.get("mint", "")),
            symbol=str(data.get("symbol", "")),
            entry_price_usd=float(data.get("entry_price_usd", 0.0)),
            entry_amount_usd=float(data.get("entry_amount_usd", 0.0)),
            entry_time=float(data.get("entry_time", 0.0)),
            score=float(data.get("score", 0.0)),
            risk=str(data.get("risk", "")),
            token_raw_amount=int(data.get("token_raw_amount", 0)),
            token_qty=float(data.get("token_qty", 0.0)),
            signature=str(data.get("signature", "")),
            entry_liquidity_usd=float(data.get("entry_liquidity_usd", 0.0)),
            entry_age_seconds=float(data.get("entry_age_seconds", 0.0)),
            max_price_usd=float(data.get("max_price_usd", 0.0)),
        )


# Score bands used for the "does score predict profit?" bucket analysis.
SCORE_BANDS = (
    (0, 30, "0-30"),
    (30, 50, "30-50"),
    (50, 60, "50-60"),
    (60, 70, "60-70"),
    (70, 999, "70+"),
)


def _score_band(score: float) -> str:
    """Map a score to its bucket label."""
    for lo, hi, name in SCORE_BANDS:
        if lo <= score < hi:
            return name
    return "70+"


@dataclass
class TraderStats:
    """Rolling statistics kept across the whole run.

    ``exit_stats`` and ``score_buckets`` accumulate un-aggregated ROIs so we
    can report averages, medians, and peak unrealized ROI per segment — the
    inputs to the trading-edge analysis.
    """

    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_usd: float = 0.0
    exit_stats: dict = field(default_factory=dict)     # reason -> stats dict
    score_buckets: dict = field(default_factory=dict)  # band    -> stats dict

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0

    @property
    def exit_counts(self) -> dict:
        """Reason -> count, for compact cards (back-compat with old UI)."""
        return {k: v["count"] for k, v in self.exit_stats.items()}

    def record(self, position: Position) -> None:
        """Count a closed trade, per exit-reason and score band.

        Zero-pnl ties count as trades but neither win nor loss — a flat exit
        is "no edge", not a victory.
        """
        pnl = position.pnl_usd
        self.trades += 1
        if pnl > 0:
            self.wins += 1
        elif pnl < 0:
            self.losses += 1
        self.total_pnl_usd += pnl

        ex = self.exit_stats.setdefault(position.exit_reason, {"rois": []})
        ex["count"] = ex.get("count", 0) + 1
        ex["wins"] = ex.get("wins", 0) + (1 if pnl > 0 else 0)
        ex["roi_sum"] = ex.get("roi_sum", 0.0) + position.roi_pct
        ex["max_roi"] = max(ex.get("max_roi", 0.0), position.max_roi_pct)
        ex["rois"].append(position.roi_pct)

        band = _score_band(position.score)
        bk = self.score_buckets.setdefault(band, {"count": 0, "wins": 0, "roi_sum": 0.0})
        bk["count"] += 1
        bk["wins"] += (1 if pnl > 0 else 0)
        bk["roi_sum"] += position.roi_pct

    def exit_stats_summary(self) -> str:
        """Per-exit-reason: count, avg/median final ROI, peak unrealized ROI."""
        parts = []
        for reason in sorted(self.exit_stats):
            d = self.exit_stats[reason]
            n = d["count"]
            if n == 0:
                continue
            avg = d["roi_sum"] / n
            ordered = sorted(d["rois"])
            if n % 2:
                med = ordered[n // 2]
            else:
                med = (ordered[n // 2 - 1] + ordered[n // 2]) / 2
            parts.append(
                f"{reason}: n={n} avg={avg:+.1f}% med={med:+.1f}% "
                f"peak={d['max_roi']:+.0f}%"
            )
        return "exits " + (" | ".join(parts) if parts else "none")

    def bucket_summary(self) -> str:
        """Avg ROI and win rate per score band (does score predict profit?)."""
        parts = []
        for _lo, _hi, name in SCORE_BANDS:
            d = self.score_buckets.get(name)
            if not d or d["count"] == 0:
                continue
            avg = d["roi_sum"] / d["count"]
            wr = d["wins"] / d["count"] * 100
            parts.append(f"score{name}: n={d['count']} avg={avg:+.1f}% win={wr:.0f}%")
        return "buckets " + (" | ".join(parts) if parts else "none")


class Trader:
    """Single-position-at-a-time trader driven by scanner signals."""

    def __init__(
        self,
        settings: Settings,
        dex: DexScreener,
        jup_price: JupiterPrice,
        jupiter: JupiterSwap,
        reporter: TelegramNotifier,
        on_trade_closed: Optional[object] = None,
    ) -> None:
        self._settings = settings
        self._dex = dex
        self._jup_price = jup_price
        self._jupiter = jupiter
        self._reporter = reporter
        # Optional object exposing ``async record_trade(position)`` — used by
        # the monitoring journal. Kept loosely typed to avoid an import cycle.
        self._journal = on_trade_closed
        self._risk = settings.risk
        self._exit_cfg = settings.exit

        # -- bankroll ----------------------------------------------------------
        self.play_amount: float = float(settings.starting_amount_usdc)
        self.saved_amount: float = 0.0

        # -- runtime state -------------------------------------------------------
        self.position: Optional[Position] = None
        self.stats = TraderStats()
        self._consec_losses = 0
        self._paused_until: float = 0.0
        self._started_at = time.monotonic()
        # Why signals were skipped at the quote-gate (never reached a buy).
        self._skip_stats: dict[str, int] = {}

    def skip_summary(self) -> str:
        """Breakdown of signals skipped at the quote gate (if any)."""
        if not self._skip_stats:
            return "skips none"
        parts = ", ".join(f"{k}={v}" for k, v in sorted(self._skip_stats.items()))
        return f"skips {parts}"

    # ------------------------------------------------------------------ accessors
    @property
    def balance_usd(self) -> float:
        """Available capital: play amount + saved profits."""
        return self.play_amount + self.saved_amount

    @property
    def in_position(self) -> bool:
        return self.position is not None

    def is_paused(self, now: Optional[float] = None) -> bool:
        """True while the loss-pause cooldown is active (wall-clock based)."""
        now = time.time() if now is None else now
        return now < self._paused_until

    # -------------------------------------------------------------------- risk gate
    def _can_trade(self) -> bool:
        if self.in_position:
            return False
        if self.is_paused():
            log.info("risk gate: paused for %.0fs", self._paused_until - time.time())
            return False
        if self.play_amount < self._risk.play_floor_usd:
            log.warning("play floor hit: resetting %s -> %s",
                        self.play_amount, self._settings.starting_amount_usdc)
            self.play_amount = float(self._settings.starting_amount_usdc)
        return True

    def _apply_compounding(self, position: Position) -> None:
        """Split a winning trade: reinvest ratio stays in play, rest saved.

        Only called for actual winners (``pnl > 0``), so ``saved_amount``
        tracks genuinely extracted profit — never a 40% cut of a flat trade.
        The reinvested stake is clamped to ``[min, max]_play_amount_usd``;
        anything above the cap (or below the floor) is swept to/from savings,
        so a hot streak can never grow the position size unboundedly.
        """
        proceeds = position.exit_amount_usd
        ratio = self._settings.compounding.reinvest_ratio
        raw = proceeds * ratio
        c = self._settings.compounding
        play = min(max(raw, c.min_play_amount_usd), c.max_play_amount_usd)
        self.play_amount = play
        self.saved_amount += proceeds - play
        log.info("compounded: play=%.4f saved=%.4f", self.play_amount, self.saved_amount)

    def _settle(self, position: Position) -> None:
        """Record a closed position into stats, risk counters, and bankroll.

        The stake was reserved at entry (``play_amount`` -> 0), so closing a
        position must hand the proceeds back to the bankroll. Wins are split
        by the compounding ratio; losses and flat ties return the proceeds
        wholesale — losses therefore *reduce* the bankroll by the lost
        amount, ties add nothing.
        """
        self.stats.record(position)
        if position.pnl_usd > 0:
            self._consec_losses = 0
            self._apply_compounding(position)
        else:
            # Loss or tie: return whatever capital is left, no savings cut.
            if position.pnl_usd < 0:
                self._consec_losses += 1
            else:
                self._consec_losses = 0
            self.play_amount = position.exit_amount_usd
            if self._consec_losses >= self._risk.max_consec_losses:
                self._paused_until = time.time() + self._risk.pause_seconds
                log.warning("loss pause: %d losses in a row, pausing %ds",
                            self._consec_losses, self._risk.pause_seconds)

    # ----------------------------------------------------------------------- prices
    async def _get_price(self, mint: str) -> Optional[float]:
        """Primary: DexScreener. Fallback: Jupiter /price/v3."""
        price = await self._dex.price_usd(mint)
        if price:
            return price
        return await self._jup_price.get_price(mint)

    # ------------------------------------------------------------------- run loop
    async def run(self, signals: "asyncio.Queue[Signal]") -> None:
        """Main trader loop: open a position, monitor it to exit, repeat."""
        log.info("trader started (play=%.4f USDC, dry_run=%s)",
                 self.play_amount, self._settings.dry_run)
        while True:
            # 1) If we're in a position, babysit it until it closes.
            if self.in_position:
                await self._monitor_position()
                continue
            # 2) Otherwise wait for the next qualifying signal.
            if not self._can_trade():
                await asyncio.sleep(5.0)
                continue
            signal = await signals.get()
            # Several tokens may have qualified while we were busy. We're a
            # single-position bot, so accept them all (nothing is dropped) and
            # take the highest-scored one first.
            best = signal
            while True:
                try:
                    nxt = signals.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt.score.total > best.score.total:
                    best = nxt
            await self._open(best)

    # ------------------------------------------------------------- position open
    async def _open(self, signal: Signal) -> None:
        """Execute the entry for a qualified signal, gated by a verified quote.

        We do **not** reserve capital until a quote proves the token is
        tradable (route exists, output > 0, impact within cap). Unbuyable
        tokens are skipped and counted, never force-bought.
        """
        amount = self.play_amount
        price = await self._get_price(signal.mint)
        liq = signal.pair.liquidity_usd if signal.pair else 0.0
        vol = signal.pair.volume_m5 if signal.pair else 0.0
        ratio = signal.pair.buy_sell_ratio if signal.pair else 0.0
        pair_price = signal.pair.price_usd if signal.pair else 0.0

        # Quote-gate. Live mode needs a wallet key (the taker pubkey is part of the
        # /order request); paper mode (dry-run, no real wallet) validates the
        # same gate against /order without a taker — a quote, no transaction.
        quote: Optional[QuoteResult] = None
        if self._jupiter.ready:
            quote = await self._jupiter.quote(
                signal.mint, int(amount * (10 ** USDC_DECIMALS)), liq
            )
            if quote is None or not quote.success:
                reason = quote.reason if quote else "no_quote"
                self._skip_stats[reason] = self._skip_stats.get(reason, 0) + 1
                log.info("SKIP %s (%s): quote-gate %s", signal.symbol, signal.mint, reason)
                return

        if self._settings.dry_run:
            # Paper entry: simulate, no real swap.
            entry_price = price if price else pair_price
            if entry_price <= 0:
                log.info("skip %s: no entry price", signal.symbol)
                return
            self.position = Position(
                mint=signal.mint, symbol=signal.symbol,
                entry_price_usd=entry_price, entry_amount_usd=amount,
                entry_time=time.time(), score=signal.score.total,
                risk=signal.rug_verdict,
                token_qty=amount / entry_price,
                signature="paper",
                entry_liquidity_usd=liq, entry_age_seconds=signal.age_seconds,
                max_price_usd=entry_price,
            )
            self.play_amount = 0.0  # stake reserved into the position
            log.info("PAPER BUY %s @ $%.8f for $%.4f", signal.symbol, entry_price, amount)
            await self._reporter.send_buy(
                signal.mint, signal.symbol, signal.score.total, entry_price,
                liq, vol, ratio, signal.age_seconds, amount,
                self.balance_usd, signal.rug_verdict,
            )
            return

        if not self._jupiter.ready:
            log.error("cannot trade live: no wallet key configured")
            return
        # Stale-quote guard: re-quote once if the order is too old to submit.
        max_age_ms = self._settings.quote.max_quote_age_ms
        if quote is not None and max_age_ms > 0:
            age_ms = (time.monotonic() - quote.fetched_at) * 1000
            if age_ms > max_age_ms:
                log.info("quote stale for %s (%.0fms > %dms) — re-quoting",
                         signal.symbol, age_ms, max_age_ms)
                fresh = await self._jupiter.quote(
                    signal.mint, int(amount * (10 ** USDC_DECIMALS)), liq, force=True
                )
                if fresh is None or not fresh.success:
                    reason = fresh.reason if fresh else "quote_stale"
                    self._skip_stats[reason] = self._skip_stats.get(reason, 0) + 1
                    log.info("SKIP %s (%s): re-quote %s", signal.symbol, signal.mint, reason)
                    return
                quote = fresh
        result = await self._jupiter.execute(quote.order)
        if not result.success:
            log.error("buy failed %s: %s", signal.symbol, result.error)
            await self._reporter.send_alert("Buy failed", signal.symbol)
            return
        entry_price = price or pair_price or 0.0
        self.position = Position(
            mint=signal.mint, symbol=signal.symbol,
            entry_price_usd=entry_price, entry_amount_usd=amount,
            entry_time=time.time(), score=signal.score.total,
            risk=signal.rug_verdict,
            token_raw_amount=result.output_amount,
            signature=result.signature,
            entry_liquidity_usd=liq, entry_age_seconds=signal.age_seconds,
            max_price_usd=entry_price,
        )
        self.play_amount = 0.0  # stake reserved into the position
        log.info("LIVE BUY %s sig=%s", signal.symbol, result.signature)
        await self._reporter.send_buy(
            signal.mint, signal.symbol, signal.score.total, entry_price,
            liq, vol, ratio, signal.age_seconds, amount,
            self.balance_usd, signal.rug_verdict,
        )

    # -------------------------------------------------------------- monitor/exit
    async def _monitor_position(self) -> None:
        """Poll the open position's price until an exit triggers."""
        position = self.position
        assert position is not None
        poll = self._exit_cfg.poll_interval_sec
        stale_reads = 0

        while self.in_position:
            await asyncio.sleep(poll)
            price = await self._get_price(position.mint)
            if price is None:
                stale_reads += 1
                if stale_reads >= _MAX_STALE_PRICE_READS:
                    await self._close_position(position, EXIT_TTL)
                    return
                continue
            stale_reads = 0
            # Track peak price so we can measure unrealized gain given back.
            position.max_price_usd = max(position.max_price_usd, price)

            ratio = price / position.entry_price_usd if position.entry_price_usd else 0.0
            hold = time.time() - position.entry_time

            reason = None
            if ratio >= self._exit_cfg.take_profit_mult:
                reason = EXIT_TP
            elif ratio <= self._exit_cfg.stop_loss_mult:
                reason = EXIT_SL
            elif hold >= self._exit_cfg.max_hold_seconds:
                reason = EXIT_TTL
            else:
                # Dead-pool guard: check current pair liquidity.
                pair = await self._dex.best_pair(position.mint)
                if pair and pair.is_dead(self._exit_cfg.dead_pool_liquidity_usd):
                    reason = EXIT_DEAD

            if reason:
                await self._close_position(position, reason, exit_price=price)
                return

    async def _close_position(self, position: Position, reason: str, exit_price: Optional[float] = None) -> None:
        """Execute the sell and settle the position."""
        if self._settings.dry_run:
            price = exit_price or await self._get_price(position.mint) or position.entry_price_usd
            position.exit_price_usd = price
            position.exit_amount_usd = position.token_qty * price
        else:
            result = await self._jupiter.sell(position.mint, position.token_raw_amount)
            if not result.success:
                # Dead pool / stuck: write off as a loss rather than retry forever.
                position.exit_attempts += 1
                log.error("sell failed %s: %s — writing off", position.symbol, result.error)
                position.exit_amount_usd = 0.0
                await self._reporter.send_alert("Sell failed", position.symbol)
            else:
                position.exit_attempts += 1
                position.exit_amount_usd = result.output_amount / 10**6
                position.signature = result.signature
                position.last_sell_price = position.exit_amount_usd / position.token_raw_amount if position.token_raw_amount else 0.0

        position.exit_reason = reason
        position.hold_seconds = time.time() - position.entry_time
        self._settle(position)
        log.info("%s %s pnl=%+.4f roi=%+.1f%%", reason.upper(), position.symbol,
                 position.pnl_usd, position.roi_pct)
        if self._journal is not None:
            await self._journal.record_trade(position)
        await self._reporter.send_sell(
            position.mint, position.symbol, reason,
            position.pnl_usd, position.roi_pct,
            position.entry_amount_usd, position.exit_amount_usd,
            position.hold_seconds, self.balance_usd, position.risk,
            max_roi_pct=position.max_roi_pct,
        )
        self.position = None

    def restore_state(self, state) -> None:
        """Resume bankroll & stats from a previously saved journal snapshot."""
        if not state:
            return
        try:
            self.play_amount = float(state.get("play_amount", self.play_amount))
            self.saved_amount = float(state.get("saved_amount", self.saved_amount))
            stats = state.get("stats") or {}
            self.stats.trades = int(stats.get("trades", self.stats.trades))
            self.stats.wins = int(stats.get("wins", self.stats.wins))
            self.stats.losses = int(stats.get("losses", self.stats.losses))
            self.stats.total_pnl_usd = float(stats.get("total_pnl_usd", self.stats.total_pnl_usd))
            self.stats.exit_stats = dict(stats.get("exit_stats") or {})
            self.stats.score_buckets = dict(stats.get("score_buckets") or {})
            # Old journals only recorded counts; rebuild without ROI detail.
            if not self.stats.exit_stats and stats.get("exit_counts"):
                for reason, count in dict(stats["exit_counts"]).items():
                    self.stats.exit_stats[reason] = {"count": count, "rois": []}
            self._consec_losses = int(state.get("consec_losses", 0))
            self._paused_until = float(state.get("paused_until", 0.0))
            pos = state.get("position")
            self.position = Position.from_dict(pos) if pos else None
            log.info("restored state: play=%.4f saved=%.4f trades=%d position=%s",
                     self.play_amount, self.saved_amount, self.stats.trades,
                     self.position.symbol if self.position else "none")
        except Exception as exc:  # noqa: BLE001
            log.warning("partial state restore: %s", exc)

    # ------------------------------------------------------------------ lifecycle
    def summary(self) -> dict:
        """Snapshot used by the monitor/reporter for periodic summaries."""
        s = self.stats
        return {
            "runtime_s": time.monotonic() - self._started_at,
            "trades": s.trades,
            "win_rate": s.win_rate,
            "pnl_usdc": s.total_pnl_usd,
            "balance_usdc": self.balance_usd,
            "exit_counts": s.exit_counts,
            "skips": self.skip_summary(),
        }
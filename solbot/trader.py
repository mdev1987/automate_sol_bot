"""Trade execution & position lifecycle — the heart of the bot.

Owns the *bankroll* (in USDC) and decides **when** to enter and **when** to
exit. All the small strategy files (risk management, exit strategy,
compounding) are consolidated here so the module reads top-to-bottom like a
trading plan.

**Risk rules**

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

from .config import Settings
from .dex_screener import DexScreener
from .jupiter import JupiterSwap
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

    @property
    def pnl_usd(self) -> float:
        return self.exit_amount_usd - self.entry_amount_usd

    @property
    def roi_pct(self) -> float:
        return self.pnl_usd / self.entry_amount_usd * 100 if self.entry_amount_usd else 0.0


@dataclass
class TraderStats:
    """Rolling statistics kept across the whole run."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_usd: float = 0.0
    exit_counts: dict = field(default_factory=dict)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0

    def record(self, position: Position) -> None:
        self.trades += 1
        if position.pnl_usd >= 0:
            self.wins += 1
        else:
            self.losses += 1
        self.total_pnl_usd += position.pnl_usd
        self.exit_counts[position.exit_reason] = self.exit_counts.get(position.exit_reason, 0) + 1


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

    # ------------------------------------------------------------------ accessors
    @property
    def balance_usd(self) -> float:
        """Available capital: play amount + saved profits."""
        return self.play_amount + self.saved_amount

    @property
    def in_position(self) -> bool:
        return self.position is not None

    def is_paused(self, now: Optional[float] = None) -> bool:
        """True while the loss-pause cooldown is active."""
        return time.monotonic() if now is None else now < self._paused_until

    # -------------------------------------------------------------------- risk gate
    def _can_trade(self) -> bool:
        now = time.monotonic()
        if self.in_position:
            return False
        if now < self._paused_until:
            log.info("risk gate: paused for %.0fs", self._paused_until - now)
            return False
        if self.play_amount < self._risk.play_floor_usd:
            log.warning("play floor hit: resetting %s -> %s",
                        self.play_amount, self._settings.starting_amount_usdc)
            self.play_amount = float(self._settings.starting_amount_usdc)
        return True

    def _apply_compounding(self, position: Position) -> None:
        """Split a win: reinvest ratio stays in play, the rest is saved."""
        proceeds = position.exit_amount_usd
        ratio = self._settings.compounding.reinvest_ratio
        self.play_amount = proceeds * ratio
        self.saved_amount += proceeds * (1.0 - ratio)
        log.info("compounded: play=%.4f saved=%.4f", self.play_amount, self.saved_amount)

    def _settle(self, position: Position) -> None:
        """Record a closed position into stats and risk counters."""
        self.stats.record(position)
        if position.pnl_usd >= 0:
            self._consec_losses = 0
            self._apply_compounding(position)
        else:
            self._consec_losses += 1
            if self._consec_losses >= self._risk.max_consec_losses:
                self._paused_until = time.monotonic() + self._risk.pause_seconds
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
            await self._open(signal)

    # ------------------------------------------------------------- position open
    async def _open(self, signal: Signal) -> None:
        """Execute the entry for a qualified signal."""
        amount = self.play_amount
        price = await self._get_price(signal.mint)

        if self._settings.dry_run:
            # Paper entry: simulate, no real swap.
            entry_price = price if price else signal.pair.price_usd or 0.0
            if entry_price <= 0:
                log.info("skip %s: no entry price", signal.symbol)
                return
            self.position = Position(
                mint=signal.mint, symbol=signal.symbol,
                entry_price_usd=entry_price, entry_amount_usd=amount,
                entry_time=time.monotonic(), score=signal.score.total,
                risk=signal.rug_verdict,
                token_qty=amount / entry_price,
                signature="paper",
            )
            log.info("PAPER BUY %s @ $%.8f for $%.4f", signal.symbol, entry_price, amount)
            await self._reporter.send_buy(
                signal.mint, signal.symbol, signal.score.total, entry_price,
                signal.pair.liquidity_usd, signal.pair.volume_m5,
                signal.pair.buy_sell_ratio, signal.age_seconds, amount,
                self.balance_usd, signal.rug_verdict,
            )
            return

        # Live entry via Jupiter.
        if not self._jupiter.ready:
            log.error("cannot trade live: no wallet key configured")
            return
        result = await self._jupiter.buy(signal.mint, amount)
        if not result.success:
            log.error("buy failed %s: %s", signal.symbol, result.error)
            await self._reporter.send_alert("Buy failed", signal.symbol)
            return
        entry_price = price or signal.pair.price_usd or 0.0
        self.position = Position(
            mint=signal.mint, symbol=signal.symbol,
            entry_price_usd=entry_price, entry_amount_usd=amount,
            entry_time=time.monotonic(), score=signal.score.total,
            risk=signal.rug_verdict,
            token_raw_amount=result.output_amount,
            signature=result.signature,
        )
        log.info("LIVE BUY %s sig=%s", signal.symbol, result.signature)
        await self._reporter.send_buy(
            signal.mint, signal.symbol, signal.score.total, entry_price,
            signal.pair.liquidity_usd, signal.pair.volume_m5,
            signal.pair.buy_sell_ratio, signal.age_seconds, amount,
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

            ratio = price / position.entry_price_usd if position.entry_price_usd else 0.0
            hold = time.monotonic() - position.entry_time

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
                log.error("sell failed %s: %s — writing off", position.symbol, result.error)
                position.exit_amount_usd = 0.0
                await self._reporter.send_alert("Sell failed", position.symbol)
            else:
                position.exit_amount_usd = result.output_amount / 10**6
                position.signature = result.signature

        position.exit_reason = reason
        position.hold_seconds = time.monotonic() - position.entry_time
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
            self.stats.exit_counts = dict(stats.get("exit_counts") or {})
            self._consec_losses = int(state.get("consec_losses", 0))
            self._paused_until = float(state.get("paused_until", 0.0))
            log.info("restored state: play=%.4f saved=%.4f trades=%d",
                     self.play_amount, self.saved_amount, self.stats.trades)
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
        }
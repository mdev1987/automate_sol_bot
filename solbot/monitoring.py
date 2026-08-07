"""Monitoring & ops: logging, persistence, health watchdog, summaries.

Keeps the bot observable and self-healing:

- **Logging** — console + rotating ``bot.log``.
- **Persistence** — every closed trade lands in ``trade_log.csv``; running
  state (bankroll, stats, risk counters, and the *open position* if any) is
  snapshotted to ``journal.json`` so a restart can resume where the previous
  run left off.
- **Health watchdog** — watches the PumpDev stream heartbeat and raises a
  Telegram alert if it goes silent (dead connection the bot can't see).
- **Periodic summary** — a ``send_status`` card every N minutes.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Optional

from .config import ROOT_DIR, Settings
from .data_stream import PumpDevStream
from .reporter import TelegramNotifier
from .trader import Position, Trader

log = logging.getLogger(__name__)

# Default cadences (seconds).
_HEALTH_CHECK_SEC = 15
_STREAM_IDLE_ALERT_SEC = 60
_SUMMARY_INTERVAL_SEC = 300


def setup_logging(settings: Settings, log_dir: Optional[Path] = None) -> None:
    """Configure root logging: console + rotating file (idempotent)."""
    log_dir = log_dir or ROOT_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "bot.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    if root.handlers:  # already configured (tests, re-import)
        return
    root.setLevel(logging.INFO)

    # HTTP client libs log every request at INFO -> drown out bot events.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    log.info("logging initialised -> %s", log_file)


class TradeJournal:
    """Appends closed trades to CSV and snapshots runtime state to JSON."""

    def __init__(self, settings: Settings, base_dir: Optional[Path] = None) -> None:
        self._base = base_dir or ROOT_DIR
        self._trades_file = self._base / "trade_log.csv"
        self._journal_file = self._base / "journal.json"
        self._csv_headers = [
            "ts", "mint", "symbol", "reason", "entry_price", "exit_price",
            "entry_usdc", "exit_usdc", "pnl_usdc", "roi_pct", "hold_s", "score",
            "entry_liq", "entry_age_s", "max_price", "max_roi_pct", "exit_attempts",
        ]
        self._ensure_csv()

    def _ensure_csv(self) -> None:
        """Create the CSV with headers if it doesn't exist yet."""
        if not self._trades_file.exists():
            with self._trades_file.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(self._csv_headers)

    async def record_trade(self, position: Position) -> None:
        """Append one closed trade to the CSV (best-effort)."""
        try:
            row = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                "mint": position.mint,
                "symbol": position.symbol,
                "reason": position.exit_reason,
                "entry_price": f"{position.entry_price_usd:.8f}",
                "exit_price": f"{position.exit_price_usd:.8f}",
                "entry_usdc": f"{position.entry_amount_usd:.4f}",
                "exit_usdc": f"{position.exit_amount_usd:.4f}",
                "pnl_usdc": f"{position.pnl_usd:.4f}",
                "roi_pct": f"{position.roi_pct:.2f}",
                "hold_s": f"{position.hold_seconds:.1f}",
                "score": f"{position.score:.1f}",
                "entry_liq": f"{position.entry_liquidity_usd:.0f}",
                "entry_age_s": f"{position.entry_age_seconds:.0f}",
                "max_price": f"{position.max_price_usd:.8f}",
                "max_roi_pct": f"{position.max_roi_pct:.2f}",
                "exit_attempts": position.exit_attempts,
            }
            with self._trades_file.open("a", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=self._csv_headers).writerow(row)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not write trade to csv: %s", exc)

    async def save_state(self, trader: Trader) -> None:
        """Snapshot the trader state for crash recovery."""
        snapshot = {
            "ts": time.time(),
            "play_amount": trader.play_amount,
            "saved_amount": trader.saved_amount,
            "stats": {
                "trades": trader.stats.trades,
                "wins": trader.stats.wins,
                "losses": trader.stats.losses,
                "total_pnl_usd": trader.stats.total_pnl_usd,
                "exit_counts": trader.stats.exit_counts,
                "exit_stats": trader.stats.exit_stats,
                "score_buckets": trader.stats.score_buckets,
            },
            "consec_losses": trader._consec_losses,
            "paused_until": trader._paused_until,
            "position": trader.position.to_dict() if trader.position else None,
        }
        try:
            tmp = self._journal_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            tmp.replace(self._journal_file)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save journal: %s", exc)

    def load_state(self) -> Optional[dict]:
        """Load the last journal snapshot (``None`` when absent/invalid)."""
        try:
            if self._journal_file.exists():
                return json.loads(self._journal_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load journal: %s", exc)
        return None


class HealthMonitor:
    """Watchdog that alerts on stream silence and posts periodic summaries."""

    def __init__(
        self,
        reporter: TelegramNotifier,
        stream: PumpDevStream,
        trader: Trader,
        journal: TradeJournal,
        *,
        health_check_sec: float = _HEALTH_CHECK_SEC,
        idle_alert_sec: float = _STREAM_IDLE_ALERT_SEC,
        summary_interval_sec: float = _SUMMARY_INTERVAL_SEC,
    ) -> None:
        self._reporter = reporter
        self._stream = stream
        self._trader = trader
        self._journal = journal
        self._health_check = health_check_sec
        self._idle_alert = idle_alert_sec
        self._summary_interval = summary_interval_sec
        self._last_summary = time.monotonic()
        self._alerted_idle = False

    async def run(self) -> None:
        """Loop forever: check health, persist state, post summaries."""
        log.info("health monitor started")
        while True:
            await asyncio.sleep(self._health_check)

            # 1) Stream heartbeat check.
            idle = self._stream.idle_seconds
            if idle > self._idle_alert:
                if not self._alerted_idle:
                    await self._reporter.send_alert(
                        "Stream silence",
                        f"no PumpDev frames for {idle:.0f}s",
                    )
                    self._alerted_idle = True
                log.warning("stream silent for %.0fs", idle)
            else:
                self._alerted_idle = False

            # 2) Persist runtime state (crash recovery).
            await self._journal.save_state(self._trader)

            # 3) Periodic summary card.
            if time.monotonic() - self._last_summary >= self._summary_interval:
                self._last_summary = time.monotonic()
                await self._reporter.send_status(**self._trader.summary())
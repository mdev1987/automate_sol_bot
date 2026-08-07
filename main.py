"""Entry point: wires every module together into one crash-resilient bot.

Topology::

    PumpDevStream  --events-->  Scanner  --signals-->  Trader
         ^                          ^                    |  prices via DexScreener
         |                          +-- DexScreener      |  + Jupiter /price/v3 fallback
         +-- (self-heals)                                 v
    HealthMonitor <-- reporter --> Telegram      TradeJournal (csv/json)

Crash resistance:
    each pipeline task runs under a supervisor; if any task dies from an
    unexpected error it is logged, reported to Telegram, and recreated from
    its factory. Shutdown (SIGINT/SIGTERM) is graceful: tasks are cancelled,
    clients closed, and a "stopped" card is sent.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Callable

from solbot.config import load_settings
from solbot.data_stream import PumpDevStream
from solbot.dex_screener import DexScreener
from solbot.jupiter import JupiterSwap
from solbot.monitoring import HealthMonitor, TradeJournal, setup_logging
from solbot.prices import JupiterPrice
from solbot.reporter import TelegramNotifier
from solbot.scanner import Scanner
from solbot.trader import Trader

log = logging.getLogger(__name__)


class Bot:
    """Owns all components, tasks, and the supervise/restart loop."""

    def __init__(self) -> None:
        self.settings = load_settings()
        setup_logging(self.settings)

        # -- infrastructure -----------------------------------------------------
        self.dex = DexScreener(self.settings)
        self.jup_price = JupiterPrice(self.settings)
        self.jupiter = JupiterSwap(self.settings)
        self.reporter = TelegramNotifier(self.settings)
        self.journal = TradeJournal(self.settings)

        # -- pipeline ------------------------------------------------------------
        self.stream = PumpDevStream(self.settings)
        self.scanner = Scanner(self.settings, self.dex)
        self.trader = Trader(
            self.settings, self.dex, self.jup_price, self.jupiter,
            self.reporter, on_trade_closed=self.journal,
        )
        self.trader.restore_state(self.journal.load_state())

        self.health = HealthMonitor(self.reporter, self.stream, self.trader, self.journal)
        self.events_q: asyncio.Queue = asyncio.Queue()
        self.signals_q: asyncio.Queue = asyncio.Queue()

        self._tasks: dict[str, asyncio.Task] = {}
        self._shutdown_event = asyncio.Event()

    # ------------------------------------------------------------------ tasks
    def _factories(self) -> dict[str, Callable[[], asyncio.Task]]:
        return {
            "stream": lambda: asyncio.create_task(self.stream.iter_events(self.events_q)),
            "scanner": lambda: asyncio.create_task(self.scanner.run(self.events_q, self.signals_q)),
            "trader": lambda: asyncio.create_task(self.trader.run(self.signals_q)),
            "monitor": lambda: asyncio.create_task(self.health.run()),
        }

    async def _start(self) -> None:
        """Build initial tasks and send the startup notification."""
        for name, factory in self._factories().items():
            self._tasks[name] = factory()
        await self.reporter.test()
        await self.reporter.send_startup(self.settings.display())
        if self.trader.in_position:
            await self.reporter.send_alert(
                "Resumed position",
                f"tracking open {self.trader.position.symbol} "
                f"entry=${self.trader.position.entry_price_usd:.8f}",
            )
        log.info("startup complete: %s", self.settings.display())

    # -------------------------------------------------------------- supervisor
    def _task_name(self, task: asyncio.Task) -> str:
        """Map a task back to its pipeline name."""
        for name, t in self._tasks.items():
            if t is task:
                return name
        return "unknown"

    async def _supervise(self) -> None:
        """Watch tasks; recreate any that die unexpectedly."""
        while not self._shutdown_event.is_set():
            done, _pending = await asyncio.wait(
                list(self._tasks.values()), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                name = self._task_name(task)
                if task is self._tasks.get(name):
                    del self._tasks[name]
                try:
                    task.result()  # re-raise any unexpected exception
                except asyncio.CancelledError:
                    log.info("task %s cancelled (shutdown)", name)
                except Exception as exc:  # noqa: BLE001
                    log.exception("task %s crashed: %s", name, exc)
                    await self.reporter.send_alert("Task crashed", f"{name}: {exc}")
                    # Recreate the crashed task so the bot keeps running.
                    if not self._shutdown_event.is_set():
                        log.info("restarting task %s", name)
                        self._tasks[name] = self._factories()[name]()

    # -------------------------------------------------------------- shutdown
    def _request_shutdown(self) -> None:
        """Signal handler: flag shutdown and cancel the pipeline tasks.

        Cancelling here is essential — the supervisor blocks inside
        ``asyncio.wait()`` and would never observe a lone flag event.
        """
        self._shutdown_event.set()
        for task in self._tasks.values():
            task.cancel()

    async def _shutdown(self) -> None:
        """Cancel all tasks, close clients, send the stopped card, exit."""
        log.info("shutting down…")
        self._shutdown_event.set()
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

        await self.journal.save_state(self.trader)
        log.info("%s", self.scanner.counters_summary())
        log.info("%s", self.jupiter.quote_summary())
        log.info("%s", self.trader.stats.bucket_summary())
        log.info("%s", self.trader.stats.exit_stats_summary())
        await self.reporter.send_stopped(**self.trader.summary())

        for client in (self.dex, self.jup_price, self.jupiter):
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------- run
    async def run(self) -> None:
        """Full bot lifecycle."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                pass  # non-UNIX loop
        try:
            await self._start()
            await self._supervise()
        finally:
            await self._shutdown()
            sys.exit(0)


def main() -> None:
    """Console entry point."""
    asyncio.run(Bot().run())


if __name__ == "__main__":
    main()
"""PumpDev WebSocket feed: real-time detection of new Pump.fun token launches.

The stream is **crash & disconnection resilient**:

- auto-reconnects with exponential backoff (capped at 30s),
- re-subscribes after every reconnect,
- never lets a single malformed frame kill the loop,
- tracks a *heartbeat* so a monitoring watchdog can spot a dead/silent
  connection and act before the bot goes blind.

Events (``create``, ``buy``, ``sell``, ``complete``, ``create_pool``) are
parsed and pushed to an ``asyncio.Queue``; control frames (``connected``,
``subscribed``, ``auth``, ``error``) are logged and swallowed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import orjson
import websockets

from .config import Settings

log = logging.getLogger(__name__)

# Control frames that carry no actionable trade data.
_CONTROL_TYPES = {
    "connected",
    "connectionStatus",
    "subscribed",
    "unsubscribed",
    "auth",
    "notice",
    "error",
}

# Reconnect backoff bounds (seconds).
_BASE_BACKOFF_SEC = 2.0
_MAX_BACKOFF_SEC = 30.0


class PumpDevStream:
    """Long-lived PumpDev WebSocket client.

    Usage inside an asyncio app::

        stream = PumpDevStream(settings)
        task = asyncio.create_task(stream.iter_events(outgoing))

    The task only ends when cancelled or the event loop shuts down; every
    socket error or disconnect is handled by reconnecting with backoff.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Heartbeat markers, updated whenever a valid frame is parsed. Use
        # ``time.monotonic()`` so a monitoring watchdog can compare clocks.
        self.last_seen: float = time.monotonic()
        self.last_message_ts: float = 0.0

    # ------------------------------------------------------------------ health
    def mark_seen(self) -> None:
        """Record that a valid frame just arrived (heartbeat)."""
        now = time.monotonic()
        self.last_seen = now
        self.last_message_ts = now

    @property
    def idle_seconds(self) -> float:
        """Seconds elapsed since the last parsed frame."""
        return time.monotonic() - self.last_seen

    @property
    def wss_url(self) -> str:
        """WebSocket endpoint from settings."""
        return self._settings.pumpdev_wss

    # --------------------------------------------------------------- subscribe
    async def _subscribe(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Subscribe to new-token launches (re-run after every reconnect)."""
        await ws.send(orjson.dumps({"method": "subscribeNewToken"}))
        log.debug("subscribed to subscribeNewToken")

    # ------------------------------------------------------------------- parse
    def _parse_event(self, raw: str) -> Optional[dict]:
        """Decode a single frame, returning an event dict or ``None``.

        Control/status frames are logged and return ``None``. Malformed
        frames produce a warning at most; no exception bubbles up.
        """
        try:
            data = orjson.loads(raw)
        except (orjson.JSONDecodeError, ValueError) as exc:
            log.warning("Ignoring malformed frame: %s", exc)
            return None

        if not isinstance(data, dict):
            log.warning("Ignoring non-object frame: %r", data)
            return None

        # Market events never have a "type" field; control frames always do.
        if data.get("type") in _CONTROL_TYPES:
            log.debug("control frame: %s", data.get("message", data))
            return None

        # Keep only genuine market events (create / buy / sell / complete).
        if not data.get("mint") or not data.get("txType"):
            return None

        self.mark_seen()
        return data

    # ------------------------------------------------------------------- core
    async def iter_events(self, queue: "asyncio.Queue[dict]") -> None:
        """Forward parsed events into ``queue`` with reconnection handling.

        This is the top-level driver. It never returns unless cancelled;
        unexpected socket errors trigger an exponential-backoff reconnect.
        """
        backoff = _BASE_BACKOFF_SEC
        while True:  # reconnect loop
            try:
                async with websockets.connect(self.wss_url) as ws:
                    backoff = _BASE_BACKOFF_SEC  # connected: reset backoff
                    await self._subscribe(ws)
                    log.info("PumpDev stream connected to %s", self.wss_url)
                    async for message in ws:
                        event = self._parse_event(message)
                        if event is not None:
                            await queue.put(event)
                # A clean close falls through to the reconnect path below.
            except asyncio.CancelledError:
                raise  # let the caller cancel the task cleanly
            except Exception as exc:  # noqa: BLE001
                log.warning("PumpDev stream error: %s", exc)

            log.warning("PumpDev stream down; reconnecting in %.1fs", backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, _MAX_BACKOFF_SEC)
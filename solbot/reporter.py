"""Telegram notifier — elegant markdown + emoji trade cards.

This is the only place the bot talks to Telegram. Everything is optional:
if no token/chat are configured, every method becomes a silent no-op so the
bot can still run in console-only mode.

The same class also supports the **Bale** messenger bridge — the base URL is
switched via the ``TELEGRAM_BOT`` flag (true = Telegram, false = Bale).
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from typing import Optional

import telegramify_markdown
from telegram import Bot, MessageEntity as TGMessageEntity

from .config import Settings
from .rugcheck import RUG_EMOJI

log = logging.getLogger(__name__)

# Inline separator used between label/value pairs on a card line.
SEP = "•"

# Card emoji per event type.
ICONS = {
    "start": "🚀",
    "signal": "🟢",
    "buy": "🟢",
    "sell_win": "💰",
    "sell_loss": "🔻",
    "tp": "🎯",
    "sl": "🛑",
    "trailing": "📈",
    "ttl": "⏱️",
    "dead": "💀",
    "status": "📊",
    "alert": "⚠️",
    "stop": "🏁",
}


class TelegramNotifier:
    """Sends formatted, markdown-friendly messages for a single chat."""

    def __init__(self, settings: Settings) -> None:
        token = (settings.bot_token or "").strip()
        self._chat_id = (settings.chat_id or "").strip()
        self._enabled = bool(token and self._chat_id)
        self._bot: Optional[Bot] = None

        # ``base_url`` switches between Telegram and the Bale bridge.
        base_url = (
            "https://api.telegram.org/bot"
            if settings.telegram_enabled
            else "https://tapi.bale.ai/bot"
        )
        if self._enabled:
            self._bot = Bot(token, base_url=base_url)

    # ------------------------------------------------------------------- send
    async def _send(self, text: str) -> None:
        """Send one message with resolved markdown entities (best-effort)."""
        if not self._enabled or self._bot is None:
            log.debug("telegram disabled — dropping: %s", text[:80])
            return
        try:
            rendered, entities = telegramify_markdown.convert(text, latex_escape=False)
            tg_entities = self._to_tg_entities(entities)
            await self._bot.send_message(
                chat_id=self._chat_id, text=rendered, entities=tg_entities
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram send failed: %s", exc)

    @staticmethod
    def _to_tg_entities(items) -> list[TGMessageEntity]:
        """Translate telegramify_markdown ``MessageEntity`` objects to PTB ones."""
        result = []
        for item in items or []:
            kwargs = {"type": item.type, "offset": item.offset, "length": item.length}
            url = getattr(item, "url", None)
            if url:
                kwargs["url"] = url
            result.append(TGMessageEntity(**kwargs))
        return result

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _f(value, dp: int = 2) -> str:
        """Format a number compactly; `—` when None."""
        if value is None:
            return "—"
        value = float(value)
        if value == 0:
            return "0"
        return f"{value:,.{dp}f}"

    @staticmethod
    def _sign(value: float) -> str:
        return "+" if value >= 0 else ""

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    # ------------------------------------------------------------- lifecycle
    async def test(self) -> bool:
        """Verify the bot credentials against the API."""
        if not self._enabled or self._bot is None:
            print("[telegram] disabled")
            return False
        try:
            me = await self._bot.get_me()
            print(f"[telegram] connected as @{me.username}")
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[telegram] connection failed: {exc}", file=sys.stderr)
            return False

    async def send_startup(self, summary: str) -> None:
        """Startup card carrying the active config line."""
        await self._send(
            f"{ICONS['start']} **Bot Started**\n{SEP} `{self._now()}`\n{SEP} {summary}"
        )

    async def send_alert(self, title: str, detail: str = "") -> None:
        """Generic warning/health line."""
        body = f"{ICONS['alert']} **{title}**"
        if detail:
            body += f"\n{SEP} {detail}"
        await self._send(body)

    # ------------------------------------------------------------------- trade
    async def send_buy(
        self,
        mint: str,
        symbol: str,
        score: float,
        price_usd: Optional[float],
        liquidity_usd: float,
        volume_5m: float,
        buy_ratio: float,
        age_s: float,
        amount_usdc: float,
        balance_usdc: float,
        risk: str = "",
    ) -> None:
        """A single buy card, values in USDC."""
        short = mint[:10]
        risk_emoji = RUG_EMOJI.get(risk, "") if risk else ""
        await self._send(
            f"{ICONS['buy']} **BUY** `{symbol}` {risk_emoji}\n"
            f"`{short}…`\n"
            f"{SEP} Score `{score:.1f}` {SEP} Price `${self._f(price_usd)}`\n"
            f"{SEP} Liquidity `${self._f(liquidity_usd)}` {SEP} Vol5m `${self._f(volume_5m)}`\n"
            f"{SEP} BuyRatio `{buy_ratio:.2f}` {SEP} Age `{age_s:.0f}s`\n"
            f"{SEP} Used `${self._f(amount_usdc)}` {SEP} Bal `${self._f(balance_usdc)}` USDC"
        )

    async def send_sell(
        self,
        mint: str,
        symbol: str,
        reason: str,
        pnl_usd: float,
        roi_pct: float,
        entry_usd: float,
        exit_usd: float,
        hold_s: float,
        balance_usdc: float,
        risk: str = "",
        max_roi_pct: Optional[float] = None,
    ) -> None:
        """A sell/exit card with PnL expressed in USDC."""
        icon = ICONS.get(reason, "🔻")
        card = ICONS["sell_win"] if pnl_usd >= 0 else ICONS["sell_loss"]
        s = self._sign(pnl_usd)
        risk_emoji = RUG_EMOJI.get(risk, "") if risk else ""
        peak = (f" {SEP} Peak `{max_roi_pct:.0f}%`"
                if max_roi_pct is not None else "")
        await self._send(
            f"{card} **SELL {reason.upper()}** {icon} {risk_emoji}\n"
            f"`{(mint or '')[:10]}…`\n"
            f"{SEP} PnL `{s}${self._f(pnl_usd)}` {SEP} ROI `{s}{roi_pct:.1f}%`\n"
            f"{SEP} In `${self._f(entry_usd)}` {SEP} Out `${self._f(exit_usd)}`\n{peak}"
            f"{SEP} Held `{hold_s:.0f}s` {SEP} Bal `${self._f(balance_usdc)}` USDC"
        )

    async def send_signal(self, symbol: str, score: float, reason: str = "") -> None:
        """Compact signal card (used in dry-run to show what would trade)."""
        text = f"{ICONS['signal']} **SIGNAL** `{symbol}` score `{score:.1f}`"
        if reason:
            text += f" {SEP} {reason}"
        await self._send(text)

    async def send_status(
        self,
        runtime_s: float,
        trades: int,
        win_rate: float,
        pnl_usdc: float,
        balance_usdc: float,
        exit_counts: dict,
        skips: str = "",
        quotes: str = "",
    ) -> None:
        """Periodic summary card."""
        minutes = runtime_s / 60
        s = self._sign(pnl_usdc)
        exits = "\n".join(f"  • `{k}: {v}`" for k, v in sorted(exit_counts.items()))
        if not exits:
            exits = "  • `none yet`"
        await self._send(
            f"{ICONS['status']} **Summary**\n"
            f"{SEP} Runtime `{minutes:.0f}m` {SEP} Trades `{trades}`\n"
            f"{SEP} WinRate `{win_rate:.1f}%` {SEP} PnL `{s}${self._f(pnl_usdc)}`\n"
            f"{SEP} Balance `${self._f(balance_usdc)}` USDC\n"
            f"{SEP} Quote-gate `{skips}`\n"
            f"`{quotes}`\n"
            f"{exits}"
        )

    async def send_stopped(
        self,
        runtime_s: float,
        trades: int,
        win_rate: float,
        pnl_usdc: float,
        balance_usdc: float,
        exit_counts: dict,
        skips: str = "",
        quotes: str = "",
    ) -> None:
        """Shutdown card; same stats as :meth:`send_status`."""
        minutes = runtime_s / 60
        s = self._sign(pnl_usdc)
        await self._send(
            f"{ICONS['stop']} **Bot Stopped**\n"
            f"{SEP} Runtime `{minutes:.0f}m` {SEP} Trades `{trades}`\n"
            f"{SEP} WinRate `{win_rate:.1f}%` {SEP} PnL `{s}${self._f(pnl_usdc)}`\n"
            f"{SEP} Balance `${self._f(balance_usdc)}` USDC\n"
            f"{SEP} Quote-gate `{skips}`\n"
            f"`{quotes}`"
        )
"""Telegram notifier for pump.fun paper trader — sends formatted trade alerts."""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot
from telegram import MessageEntity as TGMessageEntity
from telegramify_markdown import convert


load_dotenv(Path(__file__).parent / ".env")


def _to_tg_entities(tfm_entities: list) -> list[TGMessageEntity]:
    result = []
    for e in tfm_entities:
        kwargs = {"type": e.type, "offset": e.offset, "length": e.length}
        url = getattr(e, "url", None)
        if url:
            kwargs["url"] = url
        language = getattr(e, "language", None)
        if language:
            kwargs["language"] = language
        result.append(TGMessageEntity(**kwargs))
    return result


def _pf_str(pf: float) -> str:
    if pf == float("inf"):
        return "\u221e"
    if pf == 0.0:
        return "0.00"
    return f"{pf:.2f}"


def _sign(n: float) -> str:
    return "+" if n >= 0 else ""


EMOJI = {
    "buy": "\U0001f7e2",
    "sell_win": "\U0001f7e2",
    "sell_loss": "\U0001f534",
    "summary": "\U0001f4ca",
    "start": "\U0001f680",
    "stop": "\U0001f6c1",
    "sl": "\U0001f6d1",
    "tp": "\U0001f3af",
    "trailing": "\U0001f4c8",
    "ttl": "\u23f3",
    "dead": "\U0001f480",
}

B = "\U0001f539"
R = "\u25b6"


TELEGRAM_BOT = os.environ.get("TELEGRAM_BOT", "true").strip().lower() not in (
    "false",
    "0",
    "no",
)
BASE_URL = (
    "https://api.telegram.org/bot" if TELEGRAM_BOT else "https://tapi.bale.ai/bot"
)


class TelegramNotifier:
    def __init__(self):
        self.token = os.environ.get("BOT_TOKEN", "")
        self.chat_id = os.environ.get("CHAT_ID", "")
        self._bot: Bot | None = None
        self._enabled = bool(self.token and self.chat_id)
        self._sent_count = 0

    @property
    def bot(self) -> Bot:
        if self._bot is None:
            self._bot = Bot(self.token, base_url=BASE_URL)
        return self._bot

    async def _send(self, text: str):
        if not self._enabled:
            return
        try:
            msg, tfm_entities = convert(text, latex_escape=False)
            entities = _to_tg_entities(tfm_entities)
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=msg,
                entities=entities,
            )
            self._sent_count += 1
        except Exception as e:  # noqa: BLE001
            print(f"[telegram] send failed: {e}", file=sys.stderr)

    async def test(self) -> bool:
        if not self._enabled:
            return False
        api = "Bale" if not TELEGRAM_BOT else "Telegram"
        try:
            me = await self.bot.get_me()
            print(f"[telegram] ({api}) connected as @{me.username} (id={me.id})")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[telegram] ({api}) connection failed: {e}", file=sys.stderr)
            return False

    async def send_startup(self, config_info: str):
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        text = f"{EMOJI['start']} **Paper Trader Started**\n{B} `{now}`\n{config_info}"
        await self._send(text)

    async def send_buy(
        self,
        mint: str,
        price: float,
        score: float,
        wallets: int,
        volume: float,
        buy_ratio: float,
        age_ms: int,
        activity: float,
        balance: float,
        rug: RugInfo | None = None,
    ):
        mint_short = mint[:12]
        lines = [
            f"{EMOJI['buy']} **BUY Signal**",
            f"`{mint_short}...`",
            f"{B} Score `{score:.1f}` {R} Price `{price:.2e}`",
            f"{B} Wallets `{wallets}` {R} Vol `{volume:.1f}`",
            f"{B} Buy ratio `{buy_ratio:.2f}` {R} Age `{age_ms // 1000}s`",
            f"{B} Activity `{activity:.3f}`",
            f"{B} Balance `{balance:.4f} SOL`",
        ]
        if rug and not rug.error:
            ve = RUG_EMOJI.get(rug.verdict, "")
            lines.append(f"{B} Rug `{ve} {rug.verdict}` Score `{rug.score}`")
            checks = []
            if rug.mint_revoked:
                checks.append("mint_frozen")
            if rug.freeze_revoked:
                checks.append("freeze_revoked")
            if rug.lp_locked:
                checks.append("lp_locked")
            if not rug.top10_ok:
                checks.append(f"top10_{rug.top10_pct:.0f}%")
            if checks:
                lines.append(f"  {' '.join(f'`{c}`' for c in checks)}")
        await self._send("\n".join(lines))

    async def send_sell(
        self,
        mint: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        hold_sec: float,
        exit_reason: str,
        score: float,
        balance: float,
        rug: RugInfo | None = None,
    ):
        is_win = pnl > 0
        emoji = EMOJI["sell_win"] if is_win else EMOJI["sell_loss"]
        reason_emoji = EMOJI.get(exit_reason, "\u2753")
        mint_short = mint[:12]
        s = _sign(pnl)
        lines = [
            f"{emoji} **SELL {exit_reason.upper()}** {reason_emoji}",
            f"`{mint_short}...`",
            f"{B} Entry `{entry_price:.2e}` {R} Exit `{exit_price:.2e}`",
            f"{B} PnL `{s}{pnl:.4f} SOL` {R} ROI `{s}{pnl_pct * 100:.2f}%`",
            f"{B} Hold `{hold_sec:.0f}s` {R} Score `{score:.1f}`",
            f"{B} Balance `{balance:.4f} SOL`",
        ]
        if rug and not rug.error:
            ve = RUG_EMOJI.get(rug.verdict, "")
            lines.append(f"{B} Rug `{ve} {rug.verdict}` Score `{rug.score}`")
        await self._send("\n".join(lines))

    async def send_summary(
        self,
        runtime_s: float,
        trades: int,
        win_rate: float,
        pnl: float,
        pf: float,
        balance: float,
        exit_counts: dict[str, int],
    ):
        runtime_m = runtime_s / 60
        s = _sign(pnl)
        pfs = _pf_str(pf)
        exit_lines = (
            "\n".join(
                f"  {EMOJI.get(k, B)} `{k}: {v}`"
                for k, v in sorted(exit_counts.items())
            )
            if exit_counts
            else f"  {B} `no exits yet`"
        )
        text = (
            f"{EMOJI['summary']} **Paper Trader Summary**\n"
            f"{B} Runtime `{runtime_m:.0f}m`\n"
            f"{B} Trades `{trades}` {R} Win Rate `{win_rate:.1f}%`\n"
            f"{B} PnL `{s}{pnl:.4f} SOL` {R} PF `{pfs}`\n"
            f"{B} Balance `{balance:.4f} SOL`\n"
            f"{exit_lines}"
        )
        await self._send(text)

    async def send_stopped(
        self, runtime_s: float, trades: int, win_rate: float, pnl: float, msgs: int = 0
    ):
        runtime_m = runtime_s / 60
        s = _sign(pnl)
        text = (
            f"{EMOJI['stop']} **Paper Trader Stopped**\n"
            f"{B} Run `{runtime_m:.0f}m` {R} Trades `{trades}`\n"
            f"{B} WR `{win_rate:.1f}%` {R} PnL `{s}{pnl:.4f} SOL`\n"
            f"{B} Sent `{msgs}` msgs"
        )
        await self._send(text)

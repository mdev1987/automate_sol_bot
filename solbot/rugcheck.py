"""In-house rug-pull heuristics: fast, dependency-free screening.

As opposed to an external rug-checking API (which adds latency and a hard
dependency), we score each candidate with simple on-chain / metadata rules
that are nearly free to compute:

- **Scam name**  — banned keyword scan over ``name``/``symbol``.
- **Wash trading** — 5-minute volume that vastly exceeds liquidity.
- **Honeypot** — heavy buying but almost no sells (can't exit).
- **Dev dump** — the creator loaded the bag at launch (huge initial buy).

A ``RugInfo`` is produced with a ``verdict`` (Safe/Warning/Rug) and a
0-100 score. The verdict/emoji mapping mirrors what ``reporter.py`` expects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .dex_screener import Pair

log = logging.getLogger(__name__)

# Emoji per verdict, used by the Telegram reporter.
RUG_EMOJI = {
    "Safe": "🟢",
    "Warning": "🟡",
    "Rug": "🔴",
}

# Banned name/symbol keywords — normalized to lowercase for matching.
SCAM_KEYWORDS = ("guranteed", "100x", "airdrop", "giveaway", "presale", "safemoon")

# Any single one of these penalty buckets. The total score is 100 - penalties.
_PENALTY_SCAM_NAME = 40
_PENALTY_WASH_TRADING = 30
_PENALTY_HONEYPOT = 50
_PENALTY_DEV_DUMP = 25

# A token is considered a honeypot trap above these thresholds.
HONEYPOT_MIN_BUYS = 50
HONEYPOT_MAX_SELLS = 2

# Dev-dump threshold: an initial creator buy above this becomes a red flag.
MAX_INITIAL_BUY_SOL = 20.0


@dataclass
class RugInfo:
    """Result of an in-house rug check for a single token."""

    error: bool = False
    verdict: str = "Safe"
    score: float = 100.0
    flags: list[str] = field(default_factory=list)

    # Compatibility fields (used by the reporter formatting layer).
    mint_revoked: bool = False
    freeze_revoked: bool = False
    lp_locked: Optional[bool] = None
    top10_ok: bool = True
    top10_pct: float = 0.0

    @property
    def flagged(self) -> bool:
        """True when at least one risk was detected."""
        return bool(self.flags)


def _scam_name_flags(name: str, symbol: str) -> list[str]:
    """Return a flag for every banned keyword found in the token's name."""
    text = f"{name} {symbol}".lower()
    return [f"scam_name:{kw}" for kw in SCAM_KEYWORDS if kw in text]


def _wash_trading_flags(pair: Optional[Pair]) -> list[str]:
    """Flag fake activity when 5m volume dwarfs real liquidity."""
    if pair and pair.liquidity_usd > 0 and pair.volume_m5 > 50 * pair.liquidity_usd:
        return ["wash_trading"]
    return []


def _honeypot_flags(pair: Optional[Pair]) -> list[str]:
    """Flag a can't-sell trap: heavy buying, essentially no sells."""
    if pair and pair.buys_m5 >= HONEYPOT_MIN_BUYS and pair.sells_m5 < HONEYPOT_MAX_SELLS:
        return ["honeypot"]
    return []


def _dev_dump_flags(create_event: dict) -> list[str]:
    """Flag a dev that loaded an outsized bag at launch."""
    initial = create_event.get("initialQuoteAmount") or create_event.get("solAmount") or 0.0
    try:
        initial = float(initial)
    except (TypeError, ValueError):
        initial = 0.0
    if initial > MAX_INITIAL_BUY_SOL:
        return ["dev_dump"]
    return []


_PENALTY = {
    "scam_name": _PENALTY_SCAM_NAME,
    "wash_trading": _PENALTY_WASH_TRADING,
    "honeypot": _PENALTY_HONEYPOT,
    "dev_dump": _PENALTY_DEV_DUMP,
}


def check_token(create_event: dict, pair: Optional[Pair]) -> RugInfo:
    """Run every heuristic and return a single :class:`RugInfo`.

    - ``create_event``: the PumpDev ``create`` frame (has ``name``,
      ``symbol``, and the creator's initial buy amount).
    - ``pair``: the best DexScreener pair (optional — wash trading and
      honeypot checks need it and are skipped when it's ``None``).
    """
    name = str(create_event.get("name") or "")
    symbol = str(create_event.get("symbol") or "")

    flags: list[str] = []
    flags += _scam_name_flags(name, symbol)
    flags += _wash_trading_flags(pair)
    flags += _honeypot_flags(pair)
    flags += _dev_dump_flags(create_event)

    # Deduplicate (e.g. two keywords could produce two scam_name flags).
    flags = list(dict.fromkeys(flags))

    score = 100.0
    for flag in flags:
        key = flag.split(":", 1)[0] if ":" in flag else flag
        score -= _PENALTY.get(key, 0)
    score = max(0.0, min(100.0, score))

    if any(f.startswith("honeypot") for f in flags):
        verdict = "Rug"
    elif score < 40:
        verdict = "Rug"
    elif score < 70:
        verdict = "Warning"
    else:
        verdict = "Safe"

    log.debug("rug check %s: verdict=%s score=%.0f flags=%s", symbol, verdict, score, flags)
    return RugInfo(
        verdict=verdict,
        score=round(score, 1),
        flags=flags,
    )
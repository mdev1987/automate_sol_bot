"""Buy-signal scoring: turn a qualified candidate into a 0-100 score.

Higher = stronger buy signal. The signal is meant to reward *being early*
on genuinely active, non-rigged launches and punish worn-out or suspicious
activity. Components (all derived from the DexScreener pair + launch event):

- **Freshness** (0-10)     — the younger the token, the more upside left.
- **Volume**   (0-20)      — logarithmic; $100k of 5m volume = max.
- **Buy pressure** (0-25)  — buy/sell ratio, saturated at 5x.
- **Fair launch** (0-15)   — the less SOL the creator bought, the higher.
- **Liquidity** (0-15)     — best inside the healthy $5k-$30k band.

The scanner uses this score to rank/surface signals; the trader only enters
when the score clears a configured cutoff.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from math import log10

log = logging.getLogger(__name__)

# Freshness boundaries (seconds).
FRESH_BEST_SEC = 2 * 60  # < 2 minutes
FRESH_GOOD_SEC = 5 * 60  # < 5 minutes

# Volume that earns the full volume bucket (log-scaled to it).
VOLUME_FULL_USD = 100_000

# Liquidity band considered healthy for a new launch.
LIQ_MIN_USD = 5_000
LIQ_MAX_USD = 30_000


@dataclass(frozen=True)
class TokenScore:
    """Breakdown of a token's score plus the total."""

    total: float
    freshness: float
    volume: float
    buy_pressure: float
    fair_launch: float
    liquidity: float


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _freshness(age_seconds: float) -> float:
    """0-10 points: early is better."""
    if age_seconds < FRESH_BEST_SEC:
        return 10.0
    if age_seconds < FRESH_GOOD_SEC:
        return 5.0
    return 0.0


def _volume_score(volume_5m: float) -> float:
    """0-20 points, log-scaled, saturated at $100K."""
    if volume_5m <= 0:
        return 0.0
    return _clamp(20.0 * (log10(volume_5m) / log10(VOLUME_FULL_USD)), 0.0, 20.0)


def _buy_pressure(ratio: float) -> float:
    """0-25 points: buy/sell ratio, saturated at 5x."""
    return _clamp(25.0 * ratio / 5.0, 0.0, 25.0)


def _fair_launch(dev_sol: float) -> float:
    """0-15 points: less creator buying = fairer = better."""
    if dev_sol <= 0:
        return 15.0
    if dev_sol < 5:
        return 9.0
    if dev_sol < 20:
        return 4.0
    return 0.0


def _liquidity_score(liquidity_usd: float) -> float:
    """0-15 points: full marks inside the healthy liquidity band."""
    if LIQ_MIN_USD <= liquidity_usd <= LIQ_MAX_USD:
        return 15.0
    return 0.0


def score_token(
    *,
    age_seconds: float,
    volume_5m: float,
    buy_ratio: float,
    dev_sol: float,
    liquidity_usd: float,
) -> TokenScore:
    """Compute the composite score for a candidate.

    All parameters are derived earlier by the scanner; this function is pure
    and unit-testable.
    """
    freshness = _freshness(age_seconds)
    volume = _volume_score(volume_5m)
    buy_pressure = _buy_pressure(buy_ratio)
    fair_launch = _fair_launch(dev_sol)
    liquidity = _liquidity_score(liquidity_usd)

    total = _clamp(freshness + volume + buy_pressure + fair_launch + liquidity, 0.0, 100.0)
    return TokenScore(
        total=round(total, 1),
        freshness=freshness,
        volume=round(volume, 1),
        buy_pressure=round(buy_pressure, 1),
        fair_launch=fair_launch,
        liquidity=liquidity,
    )
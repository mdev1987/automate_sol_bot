"""Typed configuration loaded from ``.env``.

All runtime switches, strategy thresholds and credentials live here so the
rest of the package stays free of magic numbers and ``os.environ`` lookups.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from solders.keypair import Keypair

log = logging.getLogger(__name__)

# Project root = the parent of the ``solbot`` package directory.
ROOT_DIR = Path(__file__).resolve().parent.parent

# On-chain constants.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
LAMPORTS_PER_SOL = 1_000_000_000


@dataclass(frozen=True)
class ScannerThresholds:
    """Filter thresholds used to decide whether a token qualifies for a buy.

    These gate entry (if any fails, the token is dropped). The score does
    NOT gate — it only ranks qualified tokens. All are overridable via env.
    """

    max_age_seconds: int = 10 * 60        # older than 10 min => gains are gone
    min_liquidity_usd: float = 1_500      # reject dust / near-zero pools
    max_liquidity_usd: float = 500_000    # reject already-huge pools (late entry)
    min_txns_5m: int = 6                  # at least 6 trades in last 5 minutes
    min_buys_5m: int = 4                  # at least 4 buys (not just sells)
    min_buy_sell_ratio: float = 1.1       # more buyers than sellers -> momentum
    min_volume_5m: float = 250            # $250 of volume in the last 5 minutes
    max_market_cap: float = 10_000_000    # deep caps are usually late entries
    # NOTE: no min_score here — the score does NOT gate entry. It is only
    # used to rank qualified tokens and pick the best when several qualify.


@dataclass(frozen=True)
class ExitConfig:
    """Take-profit / stop-loss policy for an open position."""

    take_profit_mult: float = 2.0        # sells at 2x entry
    stop_loss_mult: float = 0.82         # sells at -18% from entry
    dead_pool_liquidity_usd: float = 25.0  # below this liquidity the pool is dead
    poll_interval_sec: float = 8.0       # seconds between price polls
    max_hold_seconds: float = 600.0      # TTL fallback exit (hard safety)


@dataclass(frozen=True)
class RiskConfig:
    """Bankroll-protection rules (the "stay alive" layer)."""

    play_floor_usd: float = 1.00         # reset play amount if it drops below this
    max_consec_losses: int = 2           # pause after this many losses in a row
    pause_seconds: int = 300             # cooldown length after the loss streak
    dead_pool_check_seconds: int = 600   # how long to back off from a dead pool


@dataclass(frozen=True)
class CompoundingConfig:
    """Split a winning trade's proceeds between reinvest and savings."""

    reinvest_ratio: float = 0.6          # 60% goes back in, 40% is saved


@dataclass(frozen=True)
class Settings:
    """Full application settings derived from ``.env`` and package defaults."""

    # -- credentials / endpoints -------------------------------------------
    wallet_key: str = ""
    jupiter_api_key: str = ""
    pumpdev_wss: str = "wss://pumpdev.io/ws"
    rpc_url: str = ""
    dex_screener_api: str = "https://api.dexscreener.com"
    jupiter_api: str = "https://api.jup.ag"

    # -- trading budget & execution -----------------------------------------
    starting_amount_usdc: Decimal = Decimal("2.00")
    slippage_bps: int = 150
    dry_run: bool = True

    # -- telegram -------------------------------------------------------------
    bot_token: str = ""
    chat_id: str = ""
    telegram_enabled: bool = True

    # -- strategy swallows -------------------------------------------------------
    scanner: ScannerThresholds = field(default_factory=ScannerThresholds)
    exit: ExitConfig = field(default_factory=ExitConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    compounding: CompoundingConfig = field(default_factory=CompoundingConfig)

    # -- wallet ---------------------------------------------------------------
    @property
    def keypair(self) -> Optional[Keypair]:
        """Decode the base58 wallet private key into a :class:`Keypair`."""
        key = (self.wallet_key or "").strip()
        if not key:
            return None
        try:
            return Keypair.from_base58_string(key)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not parse WALLET_PRIVATE_KEY: %s", exc)
            return None

    @property
    def pubkey(self) -> str:
        """Wallet public key (empty if no key configured)."""
        return str(self.keypair.pubkey()) if self.keypair else ""

    def display(self) -> str:
        """Human-readable one-line summary used in the startup report."""
        return (
            f"mode={'dry-run' if self.dry_run else 'live'} | "
            f"budget ${self.starting_amount_usdc} USDC | "
            f"wallet={self.pubkey or '<not set>'}"
        )


def _load_env() -> None:
    """Load the repository-root ``.env`` file if present (never crashes)."""
    env_path = ROOT_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    else:
        log.warning("No .env found at %s", env_path)


def _env(key: str, default: str = "") -> str:
    """Read a single env var, returning ``default`` when unset/blank."""
    value = os.environ.get(key, "").strip()
    return value if value else default


def _env_bool(key: str, default: bool) -> bool:
    """Parse a boolean env var tolerating common truthy/falsy spellings."""
    value = _env(key, "true" if default else "false").lower()
    return value not in {"0", "false", "no", "off"}


def _env_float(key: str, default: float) -> float:
    """Parse a float env var, falling back to ``default`` when unset/bad."""
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    """Parse an int env var, falling back to ``default`` when unset/bad."""
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def load_settings() -> Settings:
    """Build the full :class:`Settings` from the environment."""
    _load_env()
    getenv = _env
    return Settings(
        wallet_key=getenv("WALLET_PRIVATE_KEY"),
        jupiter_api_key=getenv("JUPITER_API_KEY"),
        rpc_url=getenv("SOLANA_RPC_URL"),
        pumpdev_wss=getenv("PUMPDEV_WSS", "wss://pumpdev.io/ws"),
        starting_amount_usdc=Decimal(getenv("STARTING_AMOUNT_USDC", "2.00")),
        slippage_bps=int(getenv("SLIPPAGE_BPS", "150")),
        scanner=ScannerThresholds(
            max_age_seconds=_env_int("MAX_AGE_SECONDS", ScannerThresholds.max_age_seconds),
            min_liquidity_usd=_env_float("MIN_LIQUIDITY_USD", ScannerThresholds.min_liquidity_usd),
            max_liquidity_usd=_env_float("MAX_LIQUIDITY_USD", ScannerThresholds.max_liquidity_usd),
            min_txns_5m=_env_int("MIN_TXNS_5M", ScannerThresholds.min_txns_5m),
            min_buys_5m=_env_int("MIN_BUYS_5M", ScannerThresholds.min_buys_5m),
            min_buy_sell_ratio=_env_float("MIN_BUY_SELL_RATIO", ScannerThresholds.min_buy_sell_ratio),
            min_volume_5m=_env_float("MIN_VOLUME_5M", ScannerThresholds.min_volume_5m),
            max_market_cap=_env_float("MAX_MARKET_CAP", ScannerThresholds.max_market_cap),
        ),
        exit=ExitConfig(
            take_profit_mult=_env_float("TAKE_PROFIT_MULT", ExitConfig.take_profit_mult),
            stop_loss_mult=_env_float("STOP_LOSS_MULT", ExitConfig.stop_loss_mult),
            dead_pool_liquidity_usd=_env_float("DEAD_POOL_LIQUIDITY_USD", ExitConfig.dead_pool_liquidity_usd),
            poll_interval_sec=_env_float("POLL_INTERVAL_SEC", ExitConfig.poll_interval_sec),
            max_hold_seconds=_env_float("MAX_HOLD_SECONDS", ExitConfig.max_hold_seconds),
        ),
        risk=RiskConfig(
            play_floor_usd=_env_float("PLAY_FLOOR_USD", RiskConfig.play_floor_usd),
            max_consec_losses=_env_int("MAX_CONSEC_LOSSES", RiskConfig.max_consec_losses),
            pause_seconds=_env_int("LOSS_PAUSE_SEC", RiskConfig.pause_seconds),
            dead_pool_check_seconds=_env_int("DEAD_POOL_CHECK_SEC", RiskConfig.dead_pool_check_seconds),
        ),
        compounding=CompoundingConfig(
            reinvest_ratio=_env_float("REINVEST_RATIO", CompoundingConfig.reinvest_ratio),
        ),
        dry_run=_env_bool("DRY_RUN", True),
        bot_token=getenv("BOT_TOKEN"),
        chat_id=getenv("CHAT_ID"),
        telegram_enabled=_env_bool("TELEGRAM_BOT", True),
    )
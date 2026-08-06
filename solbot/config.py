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
    """Filter thresholds used to decide whether a token qualifies for a buy."""

    max_age_seconds: int = 5 * 60        # older than 5 minutes => gains are gone
    min_liquidity_usd: float = 2_000     # reject dust / near-zero pools
    max_liquidity_usd: float = 500_000   # reject already-huge pools (late entry)
    min_txns_5m: int = 12                # at least 12 trades in last 5 minutes
    min_buys_5m: int = 8                 # at least 8 buys (not just sells)
    min_buy_sell_ratio: float = 1.3      # more buyers than sellers -> momentum
    min_volume_5m: float = 500           # $500 of volume in the last 5 minutes
    max_market_cap: float = 10_000_000   # deep caps are usually late entries
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
        """Decode the hex-private-key into a Solana :class:`Keypair`."""
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
        exit=ExitConfig(
            poll_interval_sec=float(getenv("POLL_INTERVAL_SEC", "8.0")),
        ),
        dry_run=_env_bool("DRY_RUN", True),
        bot_token=getenv("BOT_TOKEN"),
        chat_id=getenv("CHAT_ID"),
        telegram_enabled=_env_bool("TELEGRAM_BOT", True),
    )
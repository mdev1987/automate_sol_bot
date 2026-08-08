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

    # -- throughput / backpressure ------------------------------------------
    max_pending_evaluations: int = 500     # bounded pending queue (drop oldest)
    max_evaluation_workers: int = 25       # concurrent evaluations
    max_scan_window_sec: int = 30          # best-effort DexScreener wait
    min_signal_score: float = 50.0         # signals below this score are dropped


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
    min_play_amount_usd: float = 1.0     # floor for the reinvested stake
    max_play_amount_usd: float = 25.0    # cap so a hot streak can't blow up


@dataclass(frozen=True)
class QuoteConfig:
    """Jupiter quote-gate: tradability checks, retries, and throttling.

    The bot only executes a swap after a verified quote passes these rules —
    a new launch with no route is skipped, not blindly bought.
    """

    max_price_impact_pct: float = 10.0       # reject quotes above this impact
    retries: int = 5                          # quote retries for "no route"
    retry_delay_sec: float = 0.5              # delay between retries
    rate_per_sec: float = 3.0                  # global quote rate limit (free tier ~1/s)
    cache_ttl_sec: float = 1.5                # dedupe bursts for the same (mint, amount)
    max_quote_age_ms: float = 3000.0          # stale-quote guard before executing

    # Liquidity-based slippage tiers: (liquidity_floor_usd, slippage_bps).
    # Table-driven so tuning is a one-line change; last tier uses inf.
    slippage_tiers: tuple = (
        (20_000, 2000),
        (100_000, 1000),
        (float("inf"), 300),
    )

    def slippage_for(self, liquidity_usd: float) -> int:
        """Pick the slippage tier for a given liquidity level."""
        for floor, bps in self.slippage_tiers:
            if liquidity_usd < floor:
                return bps
        return self.slippage_tiers[-1][1]


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
    quote: QuoteConfig = field(default_factory=QuoteConfig)

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


def _env_tiers(key: str, default: tuple) -> tuple:
    """Parse ``floor:bps,floor:bps,...`` into a slippage tier tuple.

    ``floor`` may be ``inf`` for the last (catch-all) tier.
    """
    raw = _env(key, "")
    if not raw:
        return default
    tiers: list = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        floor_s, _, bps_s = part.partition(":")
        try:
            floor = float("inf") if floor_s.strip() == "inf" else float(floor_s)
            bps = int(bps_s)
        except ValueError:
            continue
        tiers.append((floor, bps))
    if not tiers:
        return default
    return tuple(sorted(tiers, key=lambda t: (t[0] != float("inf"), t[0])))


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
            max_pending_evaluations=_env_int("MAX_PENDING_EVALUATIONS", ScannerThresholds.max_pending_evaluations),
            max_evaluation_workers=_env_int("MAX_EVALUATION_WORKERS", ScannerThresholds.max_evaluation_workers),
            max_scan_window_sec=_env_int("MAX_SCAN_WINDOW_SEC", ScannerThresholds.max_scan_window_sec),
            min_signal_score=_env_float("MIN_SIGNAL_SCORE", ScannerThresholds.min_signal_score),
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
            min_play_amount_usd=_env_float("MIN_PLAY_USD", CompoundingConfig.min_play_amount_usd),
            max_play_amount_usd=_env_float("MAX_PLAY_USD", CompoundingConfig.max_play_amount_usd),
        ),
        quote=QuoteConfig(
            max_price_impact_pct=_env_float("MAX_PRICE_IMPACT_PCT", QuoteConfig.max_price_impact_pct),
            retries=_env_int("QUOTE_RETRIES", QuoteConfig.retries),
            retry_delay_sec=_env_float("QUOTE_RETRY_DELAY_SEC", QuoteConfig.retry_delay_sec),
            rate_per_sec=_env_float("QUOTE_RATE_PER_SEC", QuoteConfig.rate_per_sec),
            cache_ttl_sec=_env_float("QUOTE_CACHE_TTL_SEC", QuoteConfig.cache_ttl_sec),
            max_quote_age_ms=_env_float("MAX_QUOTE_AGE_MS", QuoteConfig.max_quote_age_ms),
            slippage_tiers=_env_tiers("SLIPPAGE_TIERS", QuoteConfig.slippage_tiers),
        ),
        dry_run=_env_bool("DRY_RUN", True),
        bot_token=getenv("BOT_TOKEN"),
        chat_id=getenv("CHAT_ID"),
        telegram_enabled=_env_bool("TELEGRAM_BOT", True),
    )
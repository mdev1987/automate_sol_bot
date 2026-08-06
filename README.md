# automate_sol_bot

A crash-resistant Pump.fun sniper for Solana: watches new launches, filters
for momentum, ranks candidates, and trades USDC via Jupiter — with Telegram
notifications.

## How it works

```
PumpDevStream ─events→ Scanner ─signals→ Trader
     ↑             │                    │   prices: DexScreener + Jupiter /price/v3
     │             └─ DexScreener       ▼
HealthMonitor ←─ reporter ─→ Telegram      TradeJournal (csv + json)
```

- **Scanner**: detects launches, waits for the pair to be indexed on
  DexScreener, then applies market filters → rug check → momentum score.
  The score *ranks* candidates only (it never rejects) — the trader picks
  the highest-scored one when several qualify.
- **Trader**: one position at a time, with take-profit / stop-loss /
  dead-pool / TTL exits, loss-pause, a play-floor reset, and 60/40
  compounding.
- **Execution**: Jupiter managed swaps (`/swap/v2/order` → `/execute`),
  USDC in/out, slippage- escalating sells.
- **Ops**: task supervision + auto-restart, crash-recoverable journal
  (bankroll, stats, open position, cooldown), health watchdog, Telegram cards.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # or: uv sync
pip install -e .                                     # if not using uv
cp .env.example .env   # then fill in values
uv run python main.py
```

`.env` requires at minimum: `BOT_TOKEN`, `CHAT_ID` (Telegram),
`WALLET_PRIVATE_KEY` (base58) and `JUPITER_API_KEY`. Start in **dry-run**
(`DRY_RUN=true`) before going live.

## Key env knobs

| Var | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | paper-trades unless `false` |
| `STARTING_AMOUNT_USDC` | `2.00` | per-trade stake |
| `SLIPPAGE_BPS` | `150` | buy slippage |
| `MAX_AGE_SECONDS` / `MIN_LIQUIDITY_USD` / `MIN_TXNS_5M` / `MIN_BUYS_5M` / `MIN_BUY_SELL_RATIO` / `MIN_VOLUME_5M` / `MAX_MARKET_CAP` | … | entry filters |
| `TAKE_PROFIT_MULT` / `STOP_LOSS_MULT` / `MAX_HOLD_SECONDS` / `POLL_INTERVAL_SEC` | `2.0`/`0.82`/`600`/`8` | exits |
| `PLAY_FLOOR_USD` / `MAX_CONSEC_LOSSES` / `LOSS_PAUSE_SEC` / `REINVEST_RATIO` | … | risk & compounding |

## Layout

```
main.py                  orchestration, supervision, shutdown
solbot/config.py         settings + .env loading
solbot/data_stream.py    PumpDev websocket feed
solbot/dex_screener.py   pair data + price polling
solbot/prices.py         Jupiter /price/v3 fallback
solbot/rugcheck.py       in-house rug heuristics
solbot/scoring.py        0-100 quality score
solbot/scanner.py        filter → rug-check → score → signal
solbot/trader.py         bankroll, entries, exits, risk
solbot/jupiter.py        Swap V2 order+execute
solbot/reporter.py       Telegram cards
solbot/monitoring.py     logging, journal, health watchdog
```
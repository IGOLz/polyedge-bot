# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PolyEdge Bot is a strategy-based trading bot for Polymarket crypto prediction markets (5m/15m windows). It evaluates momentum signals from live market tick data and places FOK (Fill-or-Kill) orders via the Polymarket CLOB API. Uses `signature_type=2` (Gnosis Safe proxy mode) so orders appear in the Polymarket UI and funds stay in the proxy wallet.

This bot is the **trading component only** — it reads market data from a shared PostgreSQL database (`market_outcomes`, `market_ticks` tables) populated by a separate collector service not in this repo.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Generate API credentials (one-time)
python setup.py

# Run diagnostics (verify config, balance, connectivity)
python debug.py

# Start bot (live trading)
python main.py

# Start bot (dry run — evaluates strategies, no real orders)
python main.py --dry-run

# Run momentum strategy backtest
python strategy_momentum.py

# Docker
docker compose up --build
```

There is no test suite or linter configured. The `test_*.py` files are manual integration scripts that place real orders, not automated tests.

## Architecture

### Main Loop (`main.py`)
Entry point. Runs an async event loop that:
1. Initializes PostgreSQL pool and CLOB client
2. Starts background tasks (heartbeat, outcome tracker, stop-loss monitor, hourly summary)
3. Polls `market_outcomes` for active markets every `LOOP_INTERVAL` seconds
4. For each active market, calls `evaluate_strategies()` → `execute_trade()` for any signals

### Strategy Layer (`strategies.py`)
Defines the `Signal` dataclass and `evaluate_strategies()` dispatcher. Currently only the momentum strategy is implemented. Strategies read price ticks from the DB (`market_ticks` table) and return `Signal` objects with direction, entry price, and sizing data.

### Execution Layer (`executor.py`)
Takes a `Signal` and places a FOK order on the Polymarket CLOB. Handles:
- Daily net-loss tracking (resets at midnight UTC)
- Bankroll guard (pauses if balance < 2x bet size)
- Token ID resolution from CLOB API (cached in-memory)
- FOK retry logic (up to 4 attempts with backoff)
- Post-fill price validation against configured range
- Stop-loss GTC orders placed after confirmed fills

### Database Layer (`db.py`)
All PostgreSQL queries via asyncpg. Key tables:
- `bot_trades` — trade records with status lifecycle: `filled` → `win`/`loss`/`stop_loss`
- `bot_logs` — structured event log (typed JSON)
- `bot_config` — live key-value config (source of truth, seeded from .env on first run)
- `market_outcomes`, `market_ticks` — **read-only**, populated by external collector

### Config (`config.py`)
Loads `.env` via python-dotenv. Provides HTTP client factories (`get_http_client`, `get_sync_http_client`) that respect `PROXY_URL` for SOCKS5 proxy support. Also patches `py-clob-client`'s internal HTTP client for proxy routing.

### Redemption (`redeemer.py`)
Auto-redeems resolved winning positions on-chain via Gnosis Safe `execTransaction`. Currently disabled in `main.py`. Requires `POLYGON_RPC_URL` env var and POL for gas.

## Key Patterns

- **Live config from DB**: Bot reads all config from `bot_config` table each loop iteration, not from `.env`. The `.env` values only seed the DB on first run. Config changes are made in the database.
- **Proxy mode**: All Polymarket API calls use `signature_type=2` (Gnosis Safe). The EOA signs, the proxy wallet holds funds.
- **Async with sync CLOB client**: The `py-clob-client` library is synchronous. The bot wraps sync calls in `loop.run_in_executor()` with `asyncio.wait_for()` timeouts.
- **Token ID resolution**: Market IDs in the DB are Polymarket condition IDs. Token IDs (needed for orders) are fetched lazily from the CLOB API and cached in `_token_cache` dict.
- **Logging**: Uses `utils.log` (Python `logging.Logger` named "polyedge"). Logs to stdout + `bot.log` (file handler skipped in Docker). Structured events also written to `bot_logs` table via `db.log_event()`.

## Environment Variables

See `.env.example` for all required and optional variables. Key ones:
- Auth: `PRIVATE_KEY`, `POLYMARKET_API_*`, `PROXY_WALLET`, `EOA_ADDRESS`
- DB: `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
- Trading: `BET_SIZE_USD`, `DAILY_LOSS_LIMIT`, `LOOP_INTERVAL`, `STRATEGY_MOMENTUM_ENABLED`
- Optional: `PROXY_URL` (SOCKS5), `POLYGON_RPC_URL` (for redemption)

"""PostgreSQL database layer — asyncpg connection pool and all queries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import asyncpg

import config
from utils import log

# ── Connection pool (module-level, initialised once) ────────────────────
_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    """Create the asyncpg connection pool and ensure bot_trades table exists."""
    global _pool
    _pool = await asyncpg.create_pool(
        host=config.POSTGRES_HOST,
        port=config.POSTGRES_PORT,
        user=config.POSTGRES_USER,
        password=config.POSTGRES_PASSWORD,
        database=config.POSTGRES_DB,
        min_size=2,
        max_size=10,
    )
    await _create_tables()
    log.info("PostgreSQL pool ready (%s@%s:%s/%s)",
             config.POSTGRES_USER, config.POSTGRES_HOST,
             config.POSTGRES_PORT, config.POSTGRES_DB)
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised — call init_pool() first")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ── Schema ──────────────────────────────────────────────────────────────

_CREATE_BOT_LOGS = """
CREATE TABLE IF NOT EXISTS bot_logs (
    id          SERIAL PRIMARY KEY,
    logged_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    log_type    TEXT NOT NULL,
    message     TEXT NOT NULL,
    data        JSONB
);
"""

_CREATE_BOT_TRADES = """
CREATE TABLE IF NOT EXISTS bot_trades (
    id              SERIAL PRIMARY KEY,
    market_id       TEXT NOT NULL,
    market_type     TEXT NOT NULL,
    strategy_name   TEXT NOT NULL,
    direction       TEXT NOT NULL,
    entry_price     NUMERIC(6,4) NOT NULL,
    bet_size_usd    NUMERIC(10,2) NOT NULL,
    shares          NUMERIC(10,4),
    token_id        TEXT,
    condition_id    TEXT,
    status          TEXT NOT NULL,
    order_id        TEXT,
    placed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    final_outcome   TEXT,
    pnl             NUMERIC(10,2),
    notes           TEXT,
    redeemed        BOOLEAN NOT NULL DEFAULT FALSE,
    confidence_multiplier NUMERIC(4,2) DEFAULT 1.0
);
"""


_CREATE_BOT_CONFIG = """
CREATE TABLE IF NOT EXISTS bot_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def _create_tables() -> None:
    async with pool().acquire() as conn:
        await conn.execute(_CREATE_BOT_TRADES)
        await conn.execute(_CREATE_BOT_LOGS)
        await conn.execute(_CREATE_BOT_CONFIG)
        # Add columns to existing tables (idempotent)
        await conn.execute("""
            ALTER TABLE bot_trades ADD COLUMN IF NOT EXISTS redeemed BOOLEAN NOT NULL DEFAULT FALSE
        """)
        await conn.execute("""
            ALTER TABLE bot_trades ADD COLUMN IF NOT EXISTS confidence_multiplier NUMERIC(4,2) DEFAULT 1.0
        """)
        await conn.execute("""
            ALTER TABLE bot_trades ADD COLUMN IF NOT EXISTS stop_loss_order_id TEXT
        """)
        await conn.execute("""
            ALTER TABLE bot_trades ADD COLUMN IF NOT EXISTS stop_loss_price NUMERIC(6,4)
        """)
        await conn.execute("""
            ALTER TABLE bot_trades ADD COLUMN IF NOT EXISTS stop_loss_triggered BOOLEAN DEFAULT FALSE
        """)


# ── Live config ────────────────────────────────────────────────────────

async def seed_config_if_empty() -> None:
    """Seed bot_config with .env defaults for any keys not already in the database."""
    defaults = {
        'STRATEGY_MOMENTUM_ENABLED': str(config.STRATEGY_MOMENTUM_ENABLED).lower(),
        'BET_SIZE_USD': str(config.BET_SIZE_USD),
        'DAILY_LOSS_LIMIT': str(config.DAILY_LOSS_LIMIT),
    }
    async with pool().acquire() as conn:
        for key, value in defaults.items():
            await conn.execute("""
                INSERT INTO bot_config (key, value)
                VALUES ($1, $2)
                ON CONFLICT (key) DO NOTHING
            """, key.lower(), value)
    log.info("[CONFIG] Bot config loaded from database — database is source of truth")


async def get_live_config() -> dict[str, str]:
    """Read all key-value pairs from bot_config."""
    async with pool().acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM bot_config")
        return {row['key'].lower(): row['value'] for row in rows}


# ── Data classes ────────────────────────────────────────────────────────

@dataclass
class MarketInfo:
    market_id: str          # This IS the Polymarket condition ID
    market_type: str
    started_at: datetime
    ended_at: datetime
    # Token IDs are fetched lazily via CLOB API — cached in memory
    up_token_id: str | None = None
    down_token_id: str | None = None


@dataclass
class UnresolvedTrade:
    id: int
    market_id: str
    market_type: str
    strategy_name: str
    direction: str
    entry_price: float
    bet_size_usd: float
    token_id: str | None
    condition_id: str | None


# ── Queries ─────────────────────────────────────────────────────────────

async def get_active_markets() -> list[MarketInfo]:
    """Return markets that haven't ended yet (collector writes these)."""
    async with pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT market_id, market_type, started_at, ended_at
            FROM market_outcomes
            WHERE ended_at > NOW()
              AND resolved = FALSE
            ORDER BY started_at ASC
        """)
    return [
        MarketInfo(
            market_id=r["market_id"],
            market_type=r["market_type"],
            started_at=r["started_at"],
            ended_at=r["ended_at"],
        )
        for r in rows
    ]


async def get_latest_price(market_id: str) -> float | None:
    """Get most recent up_price for a market from market_ticks."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow("""
            SELECT up_price FROM market_ticks
            WHERE market_id = $1
            ORDER BY time DESC
            LIMIT 1
        """, market_id)
    if row is None:
        return None
    return float(row["up_price"])


async def get_price_at_second(market_id: str, started_at: datetime, seconds: int) -> float | None:
    """Get up_price closest to `seconds` after market start (±10s window)."""
    from datetime import timedelta
    target = started_at + timedelta(seconds=seconds)
    window_start = target - timedelta(seconds=10)
    window_end = target + timedelta(seconds=10)

    async with pool().acquire() as conn:
        row = await conn.fetchrow("""
            SELECT up_price
            FROM market_ticks
            WHERE market_id = $1
              AND time BETWEEN $2 AND $3
            ORDER BY ABS(EXTRACT(EPOCH FROM (time - $4)))
            LIMIT 1
        """, market_id, window_start, window_end, target)
    if row is None:
        return None
    return float(row["up_price"])



async def already_traded_this_market(market_id: str, strategy_name: str | None = None) -> bool:
    """Check if we already placed a trade on this market (optionally per-strategy)."""
    async with pool().acquire() as conn:
        if strategy_name:
            row = await conn.fetchrow("""
                SELECT 1 FROM bot_trades
                WHERE market_id = $1 AND strategy_name = $2
                LIMIT 1
            """, market_id, strategy_name)
        else:
            row = await conn.fetchrow("""
                SELECT 1 FROM bot_trades
                WHERE market_id = $1
                LIMIT 1
            """, market_id)
    return row is not None


async def insert_bot_trade(
    *,
    market_id: str,
    market_type: str,
    strategy_name: str,
    direction: str,
    entry_price: float,
    bet_size_usd: float,
    shares: float | None = None,
    token_id: str | None = None,
    condition_id: str | None = None,
    status: str,
    order_id: str | None = None,
    notes: str | None = None,
) -> int:
    """Insert a trade record and return its id."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO bot_trades
                (market_id, market_type, strategy_name, direction,
                 entry_price, bet_size_usd, shares, token_id,
                 condition_id, status, order_id, notes)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            RETURNING id
        """,
            market_id, market_type, strategy_name, direction,
            Decimal(str(entry_price)), Decimal(str(bet_size_usd)),
            Decimal(str(shares)) if shares is not None else None,
            token_id, condition_id, status, order_id, notes,
        )
    return row["id"]


async def get_unresolved_trades() -> list[UnresolvedTrade]:
    """Return filled trades that haven't been resolved yet."""
    async with pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, market_id, market_type, strategy_name, direction,
                   entry_price, bet_size_usd, token_id, condition_id
            FROM bot_trades
            WHERE status = 'filled' AND final_outcome IS NULL
        """)
    return [
        UnresolvedTrade(
            id=r["id"],
            market_id=r["market_id"],
            market_type=r["market_type"],
            strategy_name=r["strategy_name"],
            direction=r["direction"],
            entry_price=float(r["entry_price"]),
            bet_size_usd=float(r["bet_size_usd"]),
            token_id=r["token_id"],
            condition_id=r["condition_id"],
        )
        for r in rows
    ]


async def update_bot_trade_outcome(trade_id: int, outcome: str, pnl: float) -> None:
    """Set final_outcome, resolved_at, and pnl for a trade."""
    async with pool().acquire() as conn:
        await conn.execute("""
            UPDATE bot_trades
            SET final_outcome = $1,
                resolved_at = NOW(),
                pnl = $2
            WHERE id = $3
        """, outcome, Decimal(str(round(pnl, 2))), trade_id)


async def update_pending_outcomes(clob=None) -> None:
    """Bulk-resolve filled bot_trades by joining against market_outcomes."""
    async with pool().acquire() as conn:
        await conn.execute("""
            UPDATE bot_trades bt
            SET
                final_outcome = CASE
                    WHEN mo.final_outcome = bt.direction THEN 'win'
                    WHEN mo.final_outcome IS NOT NULL AND mo.final_outcome != bt.direction THEN 'loss'
                    ELSE NULL
                END,
                resolved_at = NOW(),
                pnl = CASE
                    WHEN mo.final_outcome = bt.direction
                        THEN (1.0 - bt.entry_price) * bt.bet_size_usd - (0.02 * bt.bet_size_usd)
                    WHEN mo.final_outcome IS NOT NULL AND mo.final_outcome != bt.direction
                        THEN -bt.entry_price * bt.bet_size_usd - (0.02 * bt.bet_size_usd)
                    ELSE NULL
                END
            FROM market_outcomes mo
            WHERE bt.market_id = mo.market_id
            AND bt.status = 'filled'
            AND bt.final_outcome IS NULL
            AND mo.resolved = TRUE
            AND mo.final_outcome IS NOT NULL
        """)

    # Cancel stop-loss orders for just-resolved trades
    if clob:
        from executor import cancel_stop_loss_order
        async with pool().acquire() as conn:
            resolved_with_sl = await conn.fetch("""
                SELECT id, stop_loss_order_id
                FROM bot_trades
                WHERE stop_loss_order_id IS NOT NULL
                AND stop_loss_triggered = FALSE
                AND final_outcome IS NOT NULL
                AND status = 'filled'
            """)
        for trade in resolved_with_sl:
            await cancel_stop_loss_order(clob, pool(), trade['id'], trade['stop_loss_order_id'])


# ── Stop-loss helpers ──────────────────────────────────────────────────

async def update_stop_loss_order(p, trade_id: int, order_id: str, stop_loss_price: float) -> None:
    async with p.acquire() as conn:
        await conn.execute("""
            UPDATE bot_trades
            SET stop_loss_order_id = $1, stop_loss_price = $2
            WHERE id = $3
        """, order_id, Decimal(str(round(stop_loss_price, 4))), trade_id)


async def mark_stop_loss_triggered(p, trade_id: int) -> None:
    async with p.acquire() as conn:
        await conn.execute("""
            UPDATE bot_trades
            SET stop_loss_triggered = TRUE, final_outcome = 'stop_loss'
            WHERE id = $1
        """, trade_id)


async def mark_stop_loss_cancelled(p, trade_id: int) -> None:
    async with p.acquire() as conn:
        await conn.execute("""
            UPDATE bot_trades SET stop_loss_order_id = NULL
            WHERE id = $1
        """, trade_id)


async def get_open_stop_loss_orders(p) -> list:
    async with p.acquire() as conn:
        return await conn.fetch("""
            SELECT id, market_id, stop_loss_order_id, token_id, direction, entry_price
            FROM bot_trades
            WHERE stop_loss_order_id IS NOT NULL
            AND stop_loss_triggered = FALSE
            AND final_outcome IS NULL
            AND status = 'filled'
        """)


async def get_unredeemed_fills() -> list[dict[str, Any]]:
    """Return filled winning trades that haven't been redeemed yet."""
    async with pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT bt.market_id, bt.condition_id, bt.token_id, bt.bet_size_usd
            FROM bot_trades bt
            JOIN market_outcomes mo ON bt.market_id = mo.market_id
            WHERE bt.status = 'filled'
              AND bt.final_outcome = 'win'
              AND bt.redeemed = FALSE
              AND bt.condition_id IS NOT NULL
              AND bt.condition_id != ''
              AND mo.resolved = TRUE
        """)
    return [dict(r) for r in rows]


async def mark_redeemed(condition_id: str) -> None:
    """Mark all trades with this condition_id as redeemed."""
    async with pool().acquire() as conn:
        await conn.execute("""
            UPDATE bot_trades SET redeemed = TRUE
            WHERE condition_id = $1
        """, condition_id)


# ── Logging ─────────────────────────────────────────────────────────────

async def log_event(log_type: str, message: str, data: dict | None = None) -> None:
    """Append a structured log entry to bot_logs. Never crashes the bot."""
    log.info(message)
    try:
        async with pool().acquire() as conn:
            await conn.execute("""
                INSERT INTO bot_logs (log_type, message, data)
                VALUES ($1, $2, $3)
            """, log_type, message, json.dumps(data) if data else None)
    except Exception as e:
        log.warning("[LOG ERROR] Failed to write bot_log: %s", e)


@dataclass
class BotStats:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    fok_no_fills: int = 0
    total_pnl: float = 0.0
    roi: float = 0.0
    daily_net_loss_today: float = 0.0
    pending_redemption: float = 0.0
    strategies_active: list[str] | None = None


async def get_bot_stats() -> BotStats:
    """Aggregate last-24h stats from bot_trades."""
    stats = BotStats()
    try:
        async with pool().acquire() as conn:
            # Last 24h trades
            row = await conn.fetchrow("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'filled') AS total_trades,
                    COUNT(*) FILTER (WHERE final_outcome = 'win') AS wins,
                    COUNT(*) FILTER (WHERE final_outcome = 'loss') AS losses,
                    COUNT(*) FILTER (WHERE status = 'fok_no_fill') AS fok_no_fills,
                    COALESCE(SUM(pnl) FILTER (WHERE final_outcome IS NOT NULL), 0) AS total_pnl,
                    COALESCE(SUM(bet_size_usd) FILTER (WHERE status = 'filled'), 0) AS total_wagered
                FROM bot_trades
                WHERE placed_at > NOW() - INTERVAL '24 hours'
            """)
            if row:
                stats.total_trades = row["total_trades"]
                stats.wins = row["wins"]
                stats.losses = row["losses"]
                stats.fok_no_fills = row["fok_no_fills"]
                stats.total_pnl = float(row["total_pnl"])
                wagered = float(row["total_wagered"])
                stats.roi = (stats.total_pnl / wagered * 100) if wagered > 0 else 0.0

            # Today's net loss (losses increase, wins decrease, floor at 0)
            row2 = await conn.fetchrow("""
                SELECT
                    COALESCE(SUM(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE 0 END), 0)
                    - COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0)
                    AS daily_net_loss
                FROM bot_trades
                WHERE status = 'filled'
                  AND final_outcome IS NOT NULL
                  AND placed_at::date = CURRENT_DATE
            """)
            if row2:
                stats.daily_net_loss_today = max(0.0, float(row2["daily_net_loss"]))

            # Pending redemption
            row3 = await conn.fetchrow("""
                SELECT COALESCE(SUM(bet_size_usd), 0) AS pending
                FROM bot_trades
                WHERE status = 'filled' AND final_outcome IS NULL
            """)
            if row3:
                stats.pending_redemption = float(row3["pending"])

            # Active strategies (distinct from last 24h)
            rows = await conn.fetch("""
                SELECT DISTINCT strategy_name FROM bot_trades
                WHERE placed_at > NOW() - INTERVAL '24 hours'
                  AND status IN ('filled', 'dry_run')
            """)
            stats.strategies_active = [r["strategy_name"] for r in rows]

    except Exception:
        log.exception("Failed to compute bot stats")

    return stats

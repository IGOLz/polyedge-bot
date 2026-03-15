"""Momentum strategy backtest — 5m markets only, early momentum detection."""

import asyncio
import json
import os
from decimal import Decimal
from itertools import product

import asyncpg
from dotenv import load_dotenv

load_dotenv()

# ── Parameter grid ─────────────────────────────────────────────────────
MOMENTUM_THRESHOLDS = [0.03, 0.05, 0.07, 0.10]
MAX_ENTRY_SECONDS = [60, 90, 120]
MIN_TIME_REMAINING_PCT = 0.50  # skip if < 50% of window remains

BET_SIZE = 1.0
FEE_RATE = 0.02


async def get_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "polymarket"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        database=os.getenv("POSTGRES_DB", "polymarket_tracker"),
        min_size=2,
        max_size=10,
    )


async def ensure_backtest_table(pool: asyncpg.Pool) -> int:
    """Create backtest_results table if needed, return next run_id."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_results (
                id                    SERIAL PRIMARY KEY,
                run_id                INTEGER NOT NULL,
                strategy              TEXT NOT NULL,
                config                JSONB NOT NULL,
                total_trades          INTEGER,
                wins                  INTEGER,
                losses                INTEGER,
                total_pnl             NUMERIC(10,4),
                win_rate              NUMERIC(6,4),
                avg_pnl_per_trade     NUMERIC(10,4),
                avg_entry_minute      NUMERIC(8,2),
                avg_entry_second      NUMERIC(8,2),
                avg_time_remaining_pct NUMERIC(6,2),
                created_at            TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        row = await conn.fetchrow(
            "SELECT COALESCE(MAX(run_id), 0) + 1 AS next_id FROM backtest_results"
        )
        return row["next_id"]


async def fetch_resolved_5m_markets(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT market_id, market_type, started_at, ended_at, final_outcome
            FROM market_outcomes
            WHERE resolved = TRUE
              AND final_outcome IS NOT NULL
              AND market_type LIKE '%5m%'
              AND market_type NOT LIKE 'btc_%'
            ORDER BY started_at
        """)
    return [dict(r) for r in rows]


async def get_price_at_second(pool: asyncpg.Pool, market_id: str, started_at, seconds: int) -> float | None:
    """Get up_price closest to `seconds` after market start (±10s window)."""
    from datetime import timedelta
    target = started_at + timedelta(seconds=seconds)
    window_start = target - timedelta(seconds=10)
    window_end = target + timedelta(seconds=10)

    async with pool.acquire() as conn:
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


async def run_backtest() -> None:
    pool = await get_pool()
    run_id = await ensure_backtest_table(pool)

    markets = await fetch_resolved_5m_markets(pool)
    print(f"Momentum backtest — {len(markets)} resolved 5m markets, run_id={run_id}")

    # Pre-fetch price_30s and price_60s for all markets
    market_prices: dict[str, dict] = {}
    for m in markets:
        mid = m["market_id"]
        p30 = await get_price_at_second(pool, mid, m["started_at"], 30)
        p60 = await get_price_at_second(pool, mid, m["started_at"], 60)
        if p30 is not None and p60 is not None:
            window_duration = (m["ended_at"] - m["started_at"]).total_seconds()
            market_prices[mid] = {
                "price_30s": p30,
                "price_60s": p60,
                "momentum": p60 - p30,
                "outcome": m["final_outcome"],
                "window_duration": window_duration,
            }

    print(f"  {len(market_prices)} markets with valid 30s/60s prices")

    results = []

    for threshold, max_entry_sec in product(MOMENTUM_THRESHOLDS, MAX_ENTRY_SECONDS):
        trades: list[float] = []
        entry_seconds_list: list[float] = []
        time_remaining_pcts: list[float] = []

        for mid, data in market_prices.items():
            momentum = data["momentum"]
            window_dur = data["window_duration"]

            # Detection happens at ~60s mark
            detection_second = 60.0

            # Filter: detection must be within max_entry_seconds
            if detection_second > max_entry_sec:
                continue

            # Filter: at least 50% of window must remain
            time_remaining_pct = (window_dur - detection_second) / window_dur
            if time_remaining_pct < MIN_TIME_REMAINING_PCT:
                continue

            direction = None
            entry_price = None

            if momentum >= threshold:
                direction = "Up"
                entry_price = data["price_60s"]
            elif momentum <= -threshold:
                direction = "Down"
                entry_price = 1 - data["price_60s"]

            if direction is not None:
                outcome = data["outcome"]
                if outcome == direction:
                    pnl = (1 - entry_price) * BET_SIZE - FEE_RATE * BET_SIZE
                else:
                    pnl = -entry_price * BET_SIZE - FEE_RATE * BET_SIZE

                trades.append(pnl)
                entry_seconds_list.append(detection_second)
                time_remaining_pcts.append(time_remaining_pct * 100)

        total_trades = len(trades)
        wins = sum(1 for p in trades if p > 0)
        losses = total_trades - wins
        total_pnl = sum(trades)
        win_rate = wins / total_trades if total_trades else 0
        avg_pnl = total_pnl / total_trades if total_trades else 0
        avg_entry_sec = (
            sum(entry_seconds_list) / len(entry_seconds_list)
            if entry_seconds_list
            else 0
        )
        avg_time_rem = (
            sum(time_remaining_pcts) / len(time_remaining_pcts)
            if time_remaining_pcts
            else 0
        )

        config_dict = {
            "momentum_threshold": threshold,
            "max_entry_seconds": max_entry_sec,
            "min_time_remaining_pct": MIN_TIME_REMAINING_PCT,
        }

        results.append({
            "config": config_dict,
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "total_pnl": round(total_pnl, 4),
            "win_rate": round(win_rate, 4),
            "avg_pnl": round(avg_pnl, 4),
            "avg_entry_second": round(avg_entry_sec, 2),
            "avg_time_remaining_pct": round(avg_time_rem, 2),
        })

        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO backtest_results
                    (run_id, strategy, config, total_trades, wins, losses,
                     total_pnl, win_rate, avg_pnl_per_trade,
                     avg_entry_second, avg_time_remaining_pct)
                VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
                run_id, "momentum", json.dumps(config_dict),
                total_trades, wins, losses,
                Decimal(str(round(total_pnl, 4))),
                Decimal(str(round(win_rate, 4))),
                Decimal(str(round(avg_pnl, 4))),
                Decimal(str(round(avg_entry_sec, 2))),
                Decimal(str(round(avg_time_rem, 2))),
            )

    await pool.close()

    # ── Report ─────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"MOMENTUM BACKTEST RESULTS  (run_id={run_id}, {len(results)} configurations)")
    print(f"{'='*80}")

    header = (
        f"{'Thresh':>7} {'MaxSec':>7} {'Trades':>7} {'Wins':>5} "
        f"{'WR%':>7} {'PnL':>9} {'Avg PnL':>8} {'AvgSec':>7} {'TimeRem%':>9}"
    )
    print(f"\n{header}")
    print("-" * len(header))
    for r in sorted(results, key=lambda x: x["total_pnl"], reverse=True):
        c = r["config"]
        print(
            f"{c['momentum_threshold']:>7.2f} {c['max_entry_seconds']:>7} "
            f"{r['total_trades']:>7} {r['wins']:>5} "
            f"{r['win_rate']*100:>6.1f}% {r['total_pnl']:>+9.4f} "
            f"{r['avg_pnl']:>+8.4f} {r['avg_entry_second']:>7.1f} "
            f"{r['avg_time_remaining_pct']:>8.1f}%"
        )

    # Top 5 by PnL
    by_pnl = sorted(results, key=lambda x: x["total_pnl"], reverse=True)[:5]
    print(f"\n--- Top 5 by PnL ---")
    for i, r in enumerate(by_pnl, 1):
        c = r["config"]
        print(
            f"  {i}. threshold={c['momentum_threshold']:.2f} max_entry={c['max_entry_seconds']}s "
            f"| trades={r['total_trades']} WR={r['win_rate']*100:.1f}% "
            f"PnL={r['total_pnl']:+.4f} avg_entry={r['avg_entry_second']:.1f}s "
            f"time_rem={r['avg_time_remaining_pct']:.1f}%"
        )

    # Top 5 by win rate (min 10 trades)
    qualified = [r for r in results if r["total_trades"] >= 10]
    by_wr = sorted(qualified, key=lambda x: x["win_rate"], reverse=True)[:5]
    print(f"\n--- Top 5 by Win Rate (min 10 trades) ---")
    for i, r in enumerate(by_wr, 1):
        c = r["config"]
        print(
            f"  {i}. threshold={c['momentum_threshold']:.2f} max_entry={c['max_entry_seconds']}s "
            f"| trades={r['total_trades']} WR={r['win_rate']*100:.1f}% "
            f"PnL={r['total_pnl']:+.4f} avg_entry={r['avg_entry_second']:.1f}s "
            f"time_rem={r['avg_time_remaining_pct']:.1f}%"
        )

    if not by_wr:
        print("  (no configurations with >= 10 trades)")


if __name__ == "__main__":
    asyncio.run(run_backtest())

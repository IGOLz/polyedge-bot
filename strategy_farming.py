"""Farming strategy backtest — 15m markets only, fading early extremes."""

import asyncio
import json
import os
from decimal import Decimal
from itertools import product

import asyncpg
from dotenv import load_dotenv

load_dotenv()

# ── Parameter grid ─────────────────────────────────────────────────────
TRIGGER_POINTS = [0.70, 0.75, 0.80, 0.85, 0.90]
MAX_ENTRY_MINUTES = [1, 2, 3, 4, 5]

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


async def fetch_resolved_15m_markets(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT market_id, market_type, started_at, ended_at, final_outcome
            FROM market_outcomes
            WHERE resolved = TRUE
              AND final_outcome IS NOT NULL
              AND market_type LIKE '%15m%'
              AND market_type NOT LIKE 'btc_%'
            ORDER BY started_at
        """)
    return [dict(r) for r in rows]


async def fetch_early_ticks(pool: asyncpg.Pool, market_id: str, started_at, max_seconds: float) -> list[dict]:
    """Get ticks within the first max_seconds of the market window."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT up_price,
                   EXTRACT(EPOCH FROM (time - $2)) AS seconds_elapsed
            FROM market_ticks
            WHERE market_id = $1
              AND time BETWEEN $2 AND $2 + make_interval(secs => $3)
            ORDER BY time
        """, market_id, started_at, float(max_seconds))
    return [dict(r) for r in rows]


async def run_backtest() -> None:
    pool = await get_pool()
    run_id = await ensure_backtest_table(pool)

    markets = await fetch_resolved_15m_markets(pool)
    print(f"Farming backtest — {len(markets)} resolved 15m markets, run_id={run_id}")

    # Pre-fetch ticks up to the max window (5 minutes)
    max_window = max(MAX_ENTRY_MINUTES) * 60
    market_ticks: dict[str, list[dict]] = {}
    for m in markets:
        ticks = await fetch_early_ticks(pool, m["market_id"], m["started_at"], max_window)
        if ticks:
            market_ticks[m["market_id"]] = ticks

    results = []

    for trigger_point, max_entry_min in product(TRIGGER_POINTS, MAX_ENTRY_MINUTES):
        max_entry_sec = max_entry_min * 60
        trades: list[float] = []
        entry_minutes_list: list[float] = []
        time_remaining_pcts: list[float] = []

        for m in markets:
            ticks = market_ticks.get(m["market_id"])
            if not ticks:
                continue

            outcome = m["final_outcome"]
            window_duration = (m["ended_at"] - m["started_at"]).total_seconds()

            for tick in ticks:
                sec = float(tick["seconds_elapsed"])
                if sec > max_entry_sec:
                    break

                price = float(tick["up_price"])
                direction = None
                entry_price = None

                if price >= trigger_point:
                    direction = "Up"
                    entry_price = price
                elif price <= (1 - trigger_point):
                    direction = "Down"
                    entry_price = 1 - price

                if direction is not None:
                    if outcome == direction:
                        pnl = (1 - entry_price) * BET_SIZE - FEE_RATE * BET_SIZE
                    else:
                        pnl = -entry_price * BET_SIZE - FEE_RATE * BET_SIZE

                    trades.append(pnl)
                    entry_minutes_list.append(sec / 60.0)
                    time_remaining_pcts.append(
                        (window_duration - sec) / window_duration * 100
                    )
                    break  # one trade per market

        total_trades = len(trades)
        wins = sum(1 for p in trades if p > 0)
        losses = total_trades - wins
        total_pnl = sum(trades)
        win_rate = wins / total_trades if total_trades else 0
        avg_pnl = total_pnl / total_trades if total_trades else 0
        avg_entry_min = (
            sum(entry_minutes_list) / len(entry_minutes_list)
            if entry_minutes_list
            else 0
        )
        avg_time_rem = (
            sum(time_remaining_pcts) / len(time_remaining_pcts)
            if time_remaining_pcts
            else 0
        )

        config_dict = {
            "trigger_point": trigger_point,
            "max_entry_minutes": max_entry_min,
        }

        results.append({
            "config": config_dict,
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "total_pnl": round(total_pnl, 4),
            "win_rate": round(win_rate, 4),
            "avg_pnl": round(avg_pnl, 4),
            "avg_entry_minute": round(avg_entry_min, 2),
            "avg_time_remaining_pct": round(avg_time_rem, 2),
        })

        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO backtest_results
                    (run_id, strategy, config, total_trades, wins, losses,
                     total_pnl, win_rate, avg_pnl_per_trade,
                     avg_entry_minute, avg_time_remaining_pct)
                VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
                run_id, "farming", json.dumps(config_dict),
                total_trades, wins, losses,
                Decimal(str(round(total_pnl, 4))),
                Decimal(str(round(win_rate, 4))),
                Decimal(str(round(avg_pnl, 4))),
                Decimal(str(round(avg_entry_min, 2))),
                Decimal(str(round(avg_time_rem, 2))),
            )

    await pool.close()

    # ── Report ─────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"FARMING BACKTEST RESULTS  (run_id={run_id}, {len(results)} configurations)")
    print(f"{'='*80}")

    header = (
        f"{'Trigger':>8} {'MaxMin':>6} {'Trades':>7} {'Wins':>5} "
        f"{'WR%':>7} {'PnL':>9} {'Avg PnL':>8} {'AvgMin':>7} {'TimeRem%':>9}"
    )
    print(f"\n{header}")
    print("-" * len(header))
    for r in sorted(results, key=lambda x: x["total_pnl"], reverse=True):
        c = r["config"]
        print(
            f"{c['trigger_point']:>8.2f} {c['max_entry_minutes']:>6} "
            f"{r['total_trades']:>7} {r['wins']:>5} "
            f"{r['win_rate']*100:>6.1f}% {r['total_pnl']:>+9.4f} "
            f"{r['avg_pnl']:>+8.4f} {r['avg_entry_minute']:>7.2f} "
            f"{r['avg_time_remaining_pct']:>8.1f}%"
        )

    # Top 5 by PnL
    by_pnl = sorted(results, key=lambda x: x["total_pnl"], reverse=True)[:5]
    print(f"\n--- Top 5 by PnL ---")
    for i, r in enumerate(by_pnl, 1):
        c = r["config"]
        print(
            f"  {i}. trigger={c['trigger_point']:.2f} max_entry={c['max_entry_minutes']}min "
            f"| trades={r['total_trades']} WR={r['win_rate']*100:.1f}% "
            f"PnL={r['total_pnl']:+.4f} avg_entry={r['avg_entry_minute']:.2f}min "
            f"time_rem={r['avg_time_remaining_pct']:.1f}%"
        )

    # Top 5 by win rate (min 10 trades)
    qualified = [r for r in results if r["total_trades"] >= 10]
    by_wr = sorted(qualified, key=lambda x: x["win_rate"], reverse=True)[:5]
    print(f"\n--- Top 5 by Win Rate (min 10 trades) ---")
    for i, r in enumerate(by_wr, 1):
        c = r["config"]
        print(
            f"  {i}. trigger={c['trigger_point']:.2f} max_entry={c['max_entry_minutes']}min "
            f"| trades={r['total_trades']} WR={r['win_rate']*100:.1f}% "
            f"PnL={r['total_pnl']:+.4f} avg_entry={r['avg_entry_minute']:.2f}min "
            f"time_rem={r['avg_time_remaining_pct']:.1f}%"
        )

    if not by_wr:
        print("  (no configurations with >= 10 trades)")


if __name__ == "__main__":
    asyncio.run(run_backtest())

"""PolyEdge Bot — strategy-based trading bot for Polymarket crypto markets."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from colorama import Fore, Style, init as colorama_init
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

import config
import db
from balance import get_usdc_balance
from executor import execute_trade
from redeemer import redemption_loop
from strategies import evaluate_strategies
from utils import log



def build_clob_client() -> ClobClient:
    creds = ApiCreds(
        api_key=config.API_KEY,
        api_secret=config.API_SECRET,
        api_passphrase=config.API_PASSPHRASE,
    )
    return ClobClient(
        config.CLOB_BASE_URL,
        key=config.PRIVATE_KEY,
        chain_id=config.CHAIN_ID,
        creds=creds,
        signature_type=2,
        funder=config.PROXY_WALLET,
    )


async def verify_proxy() -> None:
    """If PROXY_URL is set, verify the proxy works before proceeding."""
    if not config.PROXY_URL:
        log.warning("No PROXY_URL set — traffic routes directly")
        return
    try:
        async with config.get_http_client() as client:
            resp = await client.get("https://api64.ipify.org?format=json")
            ip = resp.json()["ip"]
            log.info("Proxy active — outbound IP: %s", ip)
    except Exception as e:
        log.critical("Proxy connection failed: %s — fix PROXY_URL or remove it", e)
        sys.exit(1)


async def outcome_tracker_loop() -> None:
    """Background loop: bulk-resolve filled trades via market_outcomes join."""
    log.info("Outcome tracker started (every 5 min)")
    while True:
        try:
            await db.update_pending_outcomes()
        except Exception:
            log.exception("Error in outcome tracker")

        await asyncio.sleep(300)  # 5 minutes


async def hourly_summary_loop() -> None:
    """Background loop: log an hourly performance snapshot."""
    log.info("Hourly summary loop started")
    while True:
        await asyncio.sleep(3600)  # 60 minutes
        try:
            stats = await db.get_bot_stats()
            balance = await get_usdc_balance()

            await db.log_event("hourly_summary",
                f"Hourly summary — ROI: {stats.roi:.1f}% | Balance: ${balance:.2f}", {
                    "period": "last_24h",
                    "total_trades": stats.total_trades,
                    "wins": stats.wins,
                    "losses": stats.losses,
                    "fok_no_fills": stats.fok_no_fills,
                    "total_pnl": round(stats.total_pnl, 2),
                    "roi": round(stats.roi, 2),
                    "current_balance": balance if balance >= 0 else None,
                    "daily_net_loss_today": round(stats.daily_net_loss_today, 2),
                    "daily_loss_limit": config.DAILY_LOSS_LIMIT,
                    "pending_redemption": round(stats.pending_redemption, 2),
                    "strategies_active": stats.strategies_active,
                })
        except Exception:
            log.exception("Error in hourly summary")


def _enabled_strategies_list() -> list[str]:
    enabled = []
    if config.STRATEGY_MOMENTUM_ENABLED:
        enabled.append("momentum")
    if config.STRATEGY_STREAK_ENABLED:
        enabled.append("streak")
    if config.STRATEGY_CALIBRATION_ENABLED:
        enabled.append("calibration")
    if config.STRATEGY_FARMING_ENABLED:
        enabled.append("farming")
    return enabled


async def run() -> None:
    colorama_init()
    await verify_proxy()
    config.patch_clob_client_proxy(config.PROXY_URL)

    # Init PostgreSQL
    await db.init_pool()

    # Build CLOB client
    clob = build_clob_client()

    # Startup balance check
    try:
        bal = clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        balance = int(bal.get("balance", "0")) / 1_000_000
    except Exception:
        log.critical("Could not fetch balance — check network connectivity")
        raise SystemExit(1)

    log.info("USDC balance: $%.2f", balance)

    # Resolve any pending outcomes from before bot started
    try:
        await db.update_pending_outcomes()
        log.info("Startup outcome resolution complete")
    except Exception:
        log.exception("Error resolving outcomes on startup")

    # Start redemption loop immediately — even with low balance we want to redeem winnings
    asyncio.create_task(redemption_loop())

    if balance < config.BET_SIZE_USD:
        log.warning("Balance low: $%.2f — trading paused, redemption still running", balance)
        # Don't return — continue startup so redemption loop can run

    enabled = _enabled_strategies_list()

    if config.DRY_RUN:
        log.info("[DRY RUN] Mode active — no real orders will be placed")

    # Log bot_start event
    await db.log_event("bot_start", "Bot started", {
        "strategies_enabled": enabled,
        "bet_size": config.BET_SIZE_USD,
        "daily_loss_limit": config.DAILY_LOSS_LIMIT,
        "balance": balance,
        "dry_run": config.DRY_RUN,
    })

    # Start remaining background tasks
    asyncio.create_task(outcome_tracker_loop())
    asyncio.create_task(hourly_summary_loop())

    log.info(
        "Bot started at %s UTC — mode=%s | $%.2f/trade | daily loss limit $%.2f | loop every %ds | strategies=%s",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "DRY RUN" if config.DRY_RUN else "LIVE",
        config.BET_SIZE_USD,
        config.DAILY_LOSS_LIMIT,
        config.LOOP_INTERVAL,
        ", ".join(enabled),
    )

    # ── Main strategy evaluation loop ───────────────────────────────────
    backoff = 0
    while True:
        try:
            active_markets = await db.get_active_markets()

            for market in active_markets:
                signal = await evaluate_strategies(market)
                if signal:
                    await execute_trade(clob, market, signal)

            backoff = 0  # reset on success

        except Exception as exc:
            log.exception("Strategy loop error")
            await db.log_event("bot_error", f"Strategy loop error — {exc}", {
                "error": str(exc),
            })
            backoff = min(backoff + 1, 6)
            wait = config.LOOP_INTERVAL * (2 ** backoff)
            log.info("Backing off %ds after error", wait)
            await asyncio.sleep(wait)
            continue

        await asyncio.sleep(config.LOOP_INTERVAL)


def main() -> None:
    parser = argparse.ArgumentParser(description="PolyEdge strategy trading bot")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate strategies but do not place real orders")
    args = parser.parse_args()

    if args.dry_run:
        config.DRY_RUN = True

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Shutting down (KeyboardInterrupt)")


if __name__ == "__main__":
    main()

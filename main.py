"""PolyEdge Bot — strategy-based trading bot for Polymarket crypto markets."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

import config
import db
from balance import get_usdc_balance
from executor import execute_trade, get_execution_metrics, get_variance_metrics
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


async def heartbeat_loop() -> None:
    """Background loop: log heartbeat every 10 seconds."""
    while True:
        log.info("[HEARTBEAT] Bot alive — %s", datetime.now(timezone.utc).strftime('%H:%M:%S'))
        await asyncio.sleep(10)


def _fmt_market(mt: str) -> str:
    parts = mt.split("_")
    if len(parts) == 2:
        return f"{parts[0].upper()} {parts[1]}"
    return mt


async def outcome_tracker_loop(clob) -> None:
    """Background loop: bulk-resolve filled trades via market_outcomes join."""
    log.info("Outcome tracker started (every 5 min)")
    while True:

        try:
            resolved = await db.update_pending_outcomes(clob)
            for t in resolved:
                tag = "M3" if "M3" in t["strategy_name"] else "M4" if "M4" in t["strategy_name"] else t["strategy_name"]
                market_label = _fmt_market(t["market_type"])
                pnl = t["pnl"]
                result = t["result"].upper()

                if t["result"] == "win":
                    log.info(
                        "[%s] %s | %s bet %s @%.4f (%d sh, $%.2f) → ✅ WIN | PnL: +$%.2f",
                        tag, market_label, t["market_id"][:12],
                        t["direction"], t["entry_price"],
                        int(t["shares"]), t["bet_size_usd"],
                        abs(pnl),
                    )
                else:
                    log.warning(
                        "[%s] %s | %s bet %s @%.4f (%d sh, $%.2f) → ❌ LOSS | PnL: -$%.2f",
                        tag, market_label, t["market_id"][:12],
                        t["direction"], t["entry_price"],
                        int(t["shares"]), t["bet_size_usd"],
                        abs(pnl),
                    )

                await db.log_event(
                    f"trade_{t['result']}",
                    f"[{tag}] {market_label} {t['direction']} → {result} | PnL: {pnl:+.2f}",
                    {
                        "trade_id": t["trade_id"],
                        "market_id": t["market_id"],
                        "market_type": t["market_type"],
                        "strategy_name": t["strategy_name"],
                        "direction": t["direction"],
                        "entry_price": t["entry_price"],
                        "shares": int(t["shares"]),
                        "bet_size_usd": t["bet_size_usd"],
                        "market_outcome": t["market_outcome"],
                        "pnl": pnl,
                    },
                )

            if resolved:
                wins = sum(1 for t in resolved if t["result"] == "win")
                losses = len(resolved) - wins
                total_pnl = sum(t["pnl"] for t in resolved)
                balance = await get_usdc_balance()
                log.info(
                    "Outcome batch: %d resolved (%d WIN, %d LOSS) | Batch PnL: %+.2f | Balance: $%.2f",
                    len(resolved), wins, losses, total_pnl,
                    balance if balance >= 0 else 0,
                )

        except Exception:
            log.exception("Error in outcome tracker")

        await asyncio.sleep(300)  # 5 minutes


async def stop_loss_monitor_loop(clob) -> None:
    """Background loop: check if any GTC stop-loss orders have been filled."""
    log.info("Stop-loss monitor started (every 30s)")
    while True:

        try:
            open_stop_losses = await db.get_open_stop_loss_orders(db.pool())

            for trade in open_stop_losses:
                order_id = trade['stop_loss_order_id']
                try:
                    loop = asyncio.get_event_loop()
                    order = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda oid=order_id: clob.get_order(oid)),
                        timeout=10.0,
                    )
                    status = order.get('status', '') if isinstance(order, dict) else ''

                    if status in ('FILLED', 'MATCHED'):
                        log.info("[STOP-LOSS] Stop-loss triggered for trade %d", trade['id'])
                        await db.mark_stop_loss_triggered(db.pool(), trade['id'])
                        await db.log_event('trade_stop_loss',
                            "Stop-loss triggered — position closed",
                            {'trade_id': trade['id'], 'stop_loss_order_id': order_id},
                        )
                except asyncio.TimeoutError:
                    log.warning("[STOP-LOSS] Timeout checking order %s", order_id[:16])
                except Exception as e:
                    log.warning("[STOP-LOSS] Could not check order %s: %s", order_id[:16], e)

        except Exception as e:
            log.error("[STOP-LOSS] Monitor loop error: %s", e)

        await asyncio.sleep(30)


async def hourly_summary_loop() -> None:
    """Background loop: log an hourly performance snapshot."""
    log.info("Hourly summary loop started")
    while True:
        await asyncio.sleep(3600)  # 60 minutes
        try:
            stats = await db.get_bot_stats()
            balance = await get_usdc_balance()

            # Log execution metrics
            metrics = get_execution_metrics()
            if metrics.total > 0:
                log.info("[EXEC METRICS] %s", metrics.summary())

            # Log variance metrics (locked vs actual)
            variance = get_variance_metrics()
            if variance.total_trades > 0:
                log.info("[VARIANCE METRICS] %s", variance.summary())

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
                    "exec_metrics": {
                        "total": metrics.total,
                        "filled": metrics.filled,
                        "stage_1": metrics.stage_1_fills,
                        "stage_2": metrics.stage_2_fills,
                        "stage_3": metrics.stage_3_fills,
                        "failed": metrics.failed,
                    } if metrics.total > 0 else None,
                })
        except Exception:
            log.exception("Error in hourly summary")


async def run() -> None:
    asyncio.create_task(heartbeat_loop())

    await verify_proxy()
    config.patch_clob_client_proxy(config.PROXY_URL)

    # Init PostgreSQL
    await db.init_pool()
    await db.seed_config_if_empty()

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
        await db.update_pending_outcomes(clob)
        log.info("Startup outcome resolution complete")
    except Exception:
        log.exception("Error resolving outcomes on startup")

    # Redemption disabled for now — will re-enable later
    # asyncio.create_task(redemption_loop())

    if balance < config.BET_SIZE_USD:
        log.warning("Balance low: $%.2f — trading paused, redemption still running", balance)
        # Don't return — continue startup so redemption loop can run

    if config.DRY_RUN:
        log.info("[DRY RUN] Mode active — no real orders will be placed")

    # Log bot_start event
    await db.log_event("bot_start", "Bot started", {
        "bet_size": config.BET_SIZE_USD,
        "daily_loss_limit": config.DAILY_LOSS_LIMIT,
        "balance": balance,
        "dry_run": config.DRY_RUN,
    })

    # Start remaining background tasks
    asyncio.create_task(outcome_tracker_loop(clob))
    asyncio.create_task(stop_loss_monitor_loop(clob))
    asyncio.create_task(hourly_summary_loop())

    log.info(
        "Bot started at %s UTC — mode=%s | $%.2f/trade | daily loss limit $%.2f",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "DRY RUN" if config.DRY_RUN else "LIVE",
        config.BET_SIZE_USD,
        config.DAILY_LOSS_LIMIT,
    )

    # ── Main strategy evaluation loop ───────────────────────────────────
    backoff = 0
    previous_config: dict[str, str] = {}
    first_iteration = True
    while True:

        try:
            live_config = await db.get_live_config()

            # Log active strategies on first iteration
            if first_iteration:
                from strategies import M3_CONFIG, M4_CONFIG, BET_SIZING
                active = []
                if M3_CONFIG['enabled']:
                    active.append('M3_spike_reversion')
                if M4_CONFIG['enabled']:
                    active.append('M4_volatility')
                log.info("[CONFIG] Active strategies: %s", ', '.join(active) or 'none')
                log.info("[CONFIG] M3 params — spike_threshold: %.2f | reversion: %.0f%% | window: %ds | min_reversion_ticks: %d",
                         M3_CONFIG['spike_threshold_up'], M3_CONFIG['reversion_reversal_pct'] * 100,
                         M3_CONFIG['spike_detection_window_seconds'], M3_CONFIG['min_reversion_ticks'])
                log.info("[CONFIG] M4 params — eval_second: %d | vol_threshold: %.2f | spread: [%.2f, %.2f]",
                         M4_CONFIG['eval_second'], M4_CONFIG['volatility_threshold'],
                         M4_CONFIG['min_spread'], M4_CONFIG['max_spread'])
                log.info("[CONFIG] Bet sizing: %.0f%% of bankroll | Daily loss limit: $%s",
                         BET_SIZING['bet_percentage'] * 100,
                         live_config.get('daily_loss_limit', '?'))
                first_iteration = False

            # Log any config changes
            if previous_config and live_config != previous_config:
                for key in set(live_config) | set(previous_config):
                    old_val = previous_config.get(key)
                    new_val = live_config.get(key)
                    if old_val != new_val:
                        log.info("[CONFIG] %s changed: %s → %s", key, old_val, new_val)
            previous_config = live_config.copy()

            active_markets = await db.get_active_markets()


            for market in active_markets:
                ticks = await db.get_market_ticks(market.market_id, market.started_at)
                signals = await evaluate_strategies(market, ticks)
                for signal in signals:
                    await execute_trade(clob, market, signal, live_config)

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

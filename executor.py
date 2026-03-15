"""Order execution — places trades on Polymarket based on strategy signals."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

from colorama import Fore, Style
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

import config
import db
from balance import get_usdc_balance
from strategies import Signal
from utils import log

# ── Daily net-loss tracking (resets at midnight UTC) ─────────────────────
_daily_net_loss: float = 0.0
_daily_date: str = ""

MIN_DOLLAR_SIZE = 1.0  # Polymarket minimum order value

# ── Token ID cache (fetched from CLOB API, cached for session) ──────────
_token_cache: dict[str, tuple[str, str]] = {}  # market_id (condition ID) -> (up_token_id, down_token_id)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _reset_daily_if_needed() -> None:
    global _daily_net_loss, _daily_date
    today = _today_utc()
    if _daily_date != today:
        if _daily_date:
            log.info("New UTC day — daily net loss reset (was $%.2f)", _daily_net_loss)
        _daily_date = today
        _daily_net_loss = 0.0


def record_trade_outcome(pnl: float) -> None:
    """Call this when a trade resolves. pnl is positive for win, negative for loss."""
    global _daily_net_loss
    _reset_daily_if_needed()
    if pnl < 0:
        _daily_net_loss += abs(pnl)
    else:
        _daily_net_loss = max(0.0, _daily_net_loss - pnl)


def is_daily_limit_reached(daily_limit: float | None = None) -> bool:
    _reset_daily_if_needed()
    limit = daily_limit if daily_limit is not None else config.DAILY_LOSS_LIMIT
    return _daily_net_loss >= limit


def _get_best_price(clob: ClobClient, token_id: str, side: str) -> float | None:
    """Fetch orderbook and return best executable price, or None if no liquidity."""
    try:
        book = clob.get_order_book(token_id)
    except Exception:
        log.warning("Failed to fetch orderbook for %s", token_id)
        return None

    if side == "BUY":
        asks = book.asks if hasattr(book, "asks") else []
        if not asks:
            return None
        return float(min(asks, key=lambda x: float(x.price)).price)
    else:
        bids = book.bids if hasattr(book, "bids") else []
        if not bids:
            return None
        return float(max(bids, key=lambda x: float(x.price)).price)


def _fetch_token_ids(clob: ClobClient, condition_id: str) -> tuple[str, str] | None:
    """Fetch token IDs for a market from the CLOB API. Returns (up_token_id, down_token_id)."""
    if condition_id in _token_cache:
        return _token_cache[condition_id]

    try:
        with config.get_sync_http_client(timeout=5.0) as client:
            resp = client.get(f"{config.CLOB_BASE_URL}/markets/{condition_id}")
            resp.raise_for_status()
            data = resp.json()


        # CLOB API returns a list of two token objects for binary markets
        if isinstance(data, list) and len(data) >= 2:
            tokens = {}
            for t in data:
                outcome = t.get("outcome", "").lower()
                token_id = t.get("token_id", "")
                if outcome in ("yes", "up"):
                    tokens["up"] = token_id
                elif outcome in ("no", "down"):
                    tokens["down"] = token_id
            if "up" in tokens and "down" in tokens:
                result = (tokens["up"], tokens["down"])
                _token_cache[condition_id] = result
                return result

        # Single market object with tokens array
        if isinstance(data, dict):
            tokens_list = data.get("tokens", [])
            if len(tokens_list) >= 2:
                up_id = tokens_list[0].get("token_id", "")
                down_id = tokens_list[1].get("token_id", "")
                if up_id and down_id:
                    result = (up_id, down_id)
                    _token_cache[condition_id] = result
                    return result
            else:
                log.warning("[TOKENS] No tokens in market detail for %s", condition_id[:16])

    except Exception as e:
        log.warning("[TOKENS] Error fetching market %s: %s", condition_id[:16], e)

    return None


async def place_stop_loss_order(clob, p, trade_id: int, token_id: str, shares: float, stop_loss_price: float) -> None:
    """Place a GTC sell order as a stop-loss for a filled trade."""
    await asyncio.sleep(5)  # wait for token settlement before placing stop-loss

    # Verify token balance before placing sell order
    loop = asyncio.get_event_loop()
    balance = 0
    for attempt in range(5):
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            balance_resp = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: clob.get_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
                )),
                timeout=5.0,
            )
            log.info("[STOP-LOSS] Full balance response: %s", balance_resp)
            balance = int(balance_resp.get('balance', '0')) if isinstance(balance_resp, dict) else 0
            log.info("[STOP-LOSS] Token balance check attempt %d: %d", attempt + 1, balance)
            if balance > 0:
                break
            await asyncio.sleep(3)
        except Exception as e:
            log.warning("[STOP-LOSS] Balance check failed attempt %d: %s", attempt + 1, e)
            await asyncio.sleep(3)

    if balance == 0:
        log.warning("[STOP-LOSS] Token balance is 0 after 5 attempts — skipping stop-loss for trade %d", trade_id)
        return

    # Convert raw balance to shares (CTF tokens use 6 decimal places)
    actual_shares = balance / 1_000_000
    sellable_shares = math.floor(actual_shares)

    if sellable_shares <= 0:
        log.warning("[STOP-LOSS] Sellable shares is 0 after balance conversion — skipping")
        return

    log.info("[STOP-LOSS] Actual balance: %.4f shares | selling: %d shares", actual_shares, sellable_shares)

    from py_clob_client.order_builder.constants import SELL
    log.info("[STOP-LOSS] Attempting GTC sell — token: %s | shares: %d | price: %s | trade_id: %d",
             token_id[:16], sellable_shares, stop_loss_price, trade_id)
    try:
        def _place():
            log.info("[STOP-LOSS-DEBUG] token_id type: %s | value: %s | len: %d", type(token_id), token_id, len(str(token_id)))
            sell_args = OrderArgs(token_id=token_id, price=round(stop_loss_price, 2), size=float(sellable_shares), side=SELL)
            log.info("[STOP-LOSS-DEBUG] OrderArgs token_id: %s | len: %d", sell_args.token_id, len(str(sell_args.token_id)))
            signed = clob.create_order(sell_args)
            return clob.post_order(signed, OrderType.GTC)

        resp = await asyncio.wait_for(
            loop.run_in_executor(None, _place),
            timeout=10.0,
        )

        order_id = resp.get('orderID') or resp.get('id') if isinstance(resp, dict) else None
        if order_id:
            await db.update_stop_loss_order(p, trade_id, order_id, stop_loss_price)
            log.info("[STOP-LOSS] GTC order placed for trade %d @ %.2f | order: %s", trade_id, stop_loss_price, order_id[:16])
        else:
            log.warning("[STOP-LOSS] No order ID returned for trade %d — no stop-loss active", trade_id)

    except asyncio.TimeoutError:
        log.error("[STOP-LOSS] Timeout placing stop-loss for trade %d — continuing without stop-loss", trade_id)
    except Exception as e:
        log.error("[STOP-LOSS] Full error for trade %d: %s: %s", trade_id, type(e).__name__, e)
        log.error("[STOP-LOSS] Failed order args — token: %s | size: %s | price: %s | side: SELL",
                  token_id[:16], shares, stop_loss_price)


async def cancel_stop_loss_order(clob, p, trade_id: int, stop_loss_order_id: str) -> None:
    """Cancel an existing GTC stop-loss order."""
    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: clob.cancel(stop_loss_order_id)),
            timeout=10.0,
        )
        await db.mark_stop_loss_cancelled(p, trade_id)
        log.info("[STOP-LOSS] Cancelled GTC order %s for trade %d", stop_loss_order_id[:16], trade_id)
    except asyncio.TimeoutError:
        log.error("[STOP-LOSS] Timeout cancelling stop-loss %s", stop_loss_order_id[:16])
    except Exception as e:
        log.warning("[STOP-LOSS] Could not cancel stop-loss %s: %s", stop_loss_order_id[:16], e)


async def execute_trade(
    clob: ClobClient,
    market: db.MarketInfo,
    signal: Signal,
    live_config: dict | None = None,
) -> None:
    """Place a trade based on a strategy signal. Records result to bot_trades."""
    _reset_daily_if_needed()
    if live_config is None:
        live_config = {}

    market_label = f"{market.market_type}:{market.market_id[:12]}"

    # ── Calculate dynamic bet size ─────────────────────────────────────
    base_bet = float(live_config.get('bet_size_usd', str(config.BET_SIZE_USD)))
    bet_size = round(base_bet * signal.confidence_multiplier, 2)
    bet_size = max(bet_size, 1.00)  # Polymarket minimum order

    if signal.strategy_name == 'momentum':
        log.info(
            "[MOMENTUM] Bet sizing — momentum: %.3f | multiplier: %.2fx | bet: $%.2f",
            signal.signal_data.get('momentum_value', 0) if signal.signal_data else 0,
            signal.confidence_multiplier, bet_size,
        )
    else:
        log.info(
            "[CONFIDENCE] %s on %s — multiplier: %.1fx → bet: $%.2f",
            signal.strategy_name, market.market_type,
            signal.confidence_multiplier, bet_size,
        )

    # ── Dry-run mode ────────────────────────────────────────────────────
    if config.DRY_RUN:
        log.info(
            "[DRY RUN] Would place BUY %s on %s at %.4f — strategy: %s (%.1fx → $%.2f)",
            signal.direction, market_label, signal.entry_price,
            signal.strategy_name, signal.confidence_multiplier, bet_size,
        )
        print(f"{Fore.YELLOW}[DRY RUN] Would place BUY {signal.direction} on {market_label} at {signal.entry_price:.4f} — strategy: {signal.strategy_name} ({signal.confidence_multiplier:.1f}x → ${bet_size:.2f}){Style.RESET_ALL}")
        await db.insert_bot_trade(
            market_id=market.market_id, market_type=market.market_type,
            strategy_name=signal.strategy_name, direction=signal.direction,
            entry_price=signal.entry_price, bet_size_usd=bet_size,
            confidence_multiplier=signal.confidence_multiplier,
            status="dry_run", condition_id=market.market_id,
        )
        await db.log_event("trade_dry_run",
            f"[DRY RUN] Would place {signal.direction} on {market.market_type} — strategy: {signal.strategy_name}", {
                "market_id": market.market_id,
                "market_type": market.market_type,
                "strategy_name": signal.strategy_name,
                "direction": signal.direction,
                "entry_price": signal.entry_price,
                "signal_data": signal.signal_data,
            })
        return

    # ── Guard: daily loss limit ────────────────────────────────────────
    daily_limit = float(live_config.get('daily_loss_limit', str(config.DAILY_LOSS_LIMIT)))
    if is_daily_limit_reached(daily_limit):
        log.warning("Daily loss limit reached — net loss today: $%.2f / $%.2f", _daily_net_loss, daily_limit)
        await db.insert_bot_trade(
            market_id=market.market_id, market_type=market.market_type,
            strategy_name=signal.strategy_name, direction=signal.direction,
            entry_price=signal.entry_price, bet_size_usd=bet_size,
            confidence_multiplier=signal.confidence_multiplier,
            status="skipped_daily_limit", condition_id=market.market_id,
        )
        await db.log_event("trade_skipped",
            f"Daily loss limit reached — net loss today: ${_daily_net_loss:.2f} / ${daily_limit:.2f}", {
                "market_id": market.market_id,
                "market_type": market.market_type,
                "strategy_name": signal.strategy_name,
                "direction": signal.direction,
                "reason": "daily_limit",
                "daily_net_loss": _daily_net_loss,
                "daily_loss_limit": daily_limit,
            })
        return

    # ── Guard: bankroll ─────────────────────────────────────────────────
    balance = await get_usdc_balance()
    if balance >= 0:
        min_runway = base_bet * 2
        if balance < min_runway:
            log.critical("Bankroll critically low ($%.2f < $%.2f) — bot paused", balance, min_runway)
            print(f"{Fore.RED}*** BANKROLL CRITICALLY LOW: ${balance:.2f} — bot paused ***{Style.RESET_ALL}")
            await db.insert_bot_trade(
                market_id=market.market_id, market_type=market.market_type,
                strategy_name=signal.strategy_name, direction=signal.direction,
                entry_price=signal.entry_price, bet_size_usd=bet_size,
                confidence_multiplier=signal.confidence_multiplier,
                status="skipped_bankroll", condition_id=market.market_id,
            )
            await db.log_event("trade_skipped",
                f"Signal skipped — bankroll (${balance:.2f} < ${min_runway:.2f})", {
                    "market_id": market.market_id,
                    "market_type": market.market_type,
                    "strategy_name": signal.strategy_name,
                    "direction": signal.direction,
                    "reason": "bankroll",
                    "balance": balance,
                    "min_runway": min_runway,
                })
            return

    # ── Resolve token IDs ───────────────────────────────────────────────
    up_token_id = market.up_token_id
    down_token_id = market.down_token_id
    stop_loss_enabled = True

    if not up_token_id or not down_token_id:
        ids = _fetch_token_ids(clob, market.market_id)
        if ids is None:
            log.warning("[TOKENS] Could not resolve token IDs for %s — placing trade WITHOUT stop-loss", market_label)
            stop_loss_enabled = False
            # Cannot place trade without token IDs — still need them for the order
            await db.insert_bot_trade(
                market_id=market.market_id, market_type=market.market_type,
                strategy_name=signal.strategy_name, direction=signal.direction,
                entry_price=signal.entry_price, bet_size_usd=bet_size,
                confidence_multiplier=signal.confidence_multiplier,
                status="error", condition_id=market.market_id,
                notes="Failed to resolve token IDs",
            )
            await db.log_event("bot_error",
                f"Failed to resolve token IDs for {market_label}", {
                    "market_id": market.market_id,
                })
            return
        up_token_id, down_token_id = ids
        market.up_token_id = up_token_id
        market.down_token_id = down_token_id

    token_id = up_token_id if signal.direction == "Up" else down_token_id

    # ── Get best price from orderbook ───────────────────────────────────
    best_price = _get_best_price(clob, token_id, "BUY")
    if best_price is None:
        log.warning("No liquidity for %s %s — skipping", signal.direction, market_label)
        await db.insert_bot_trade(
            market_id=market.market_id, market_type=market.market_type,
            strategy_name=signal.strategy_name, direction=signal.direction,
            entry_price=signal.entry_price, bet_size_usd=bet_size,
            confidence_multiplier=signal.confidence_multiplier,
            token_id=token_id, condition_id=market.market_id,
            status="error", notes="No liquidity",
        )
        await db.log_event("trade_skipped",
            f"Signal skipped — no liquidity for {signal.direction} on {market_label}", {
                "market_id": market.market_id,
                "market_type": market.market_type,
                "strategy_name": signal.strategy_name,
                "direction": signal.direction,
                "reason": "no_liquidity",
            })
        return

    # ── Place order ─────────────────────────────────────────────────────
    status = "error"
    order_id = None
    my_shares: float | None = None

    try:
        rounded_price = round(best_price, 2)
        if rounded_price <= 0 or rounded_price >= 1:
            log.warning("Rounded price %.2f out of range — skipping %s", rounded_price, market_label)
            await db.insert_bot_trade(
                market_id=market.market_id, market_type=market.market_type,
                strategy_name=signal.strategy_name, direction=signal.direction,
                entry_price=best_price, bet_size_usd=bet_size,
                confidence_multiplier=signal.confidence_multiplier,
                token_id=token_id, condition_id=market.market_id,
                status="error", notes=f"Price out of range: {rounded_price}",
            )
            return

        my_shares = math.floor(bet_size / rounded_price)
        min_shares = math.ceil(MIN_DOLLAR_SIZE / rounded_price)
        if my_shares < min_shares:
            my_shares = min_shares

        # Apply momentum_min_shares if configured
        cfg_min_shares = int(live_config.get('momentum_min_shares', '0'))
        if cfg_min_shares > 0 and my_shares < cfg_min_shares:
            log.info("[BET-SIZE] Shares (%d) below min_shares (%d) — increasing to %d", my_shares, cfg_min_shares, cfg_min_shares)
            my_shares = cfg_min_shares

        if my_shares < 1:
            log.warning("Cannot meet $1 minimum at price %.2f — skipping", rounded_price)
            await db.insert_bot_trade(
                market_id=market.market_id, market_type=market.market_type,
                strategy_name=signal.strategy_name, direction=signal.direction,
                entry_price=best_price, bet_size_usd=bet_size,
                confidence_multiplier=signal.confidence_multiplier,
                token_id=token_id, condition_id=market.market_id,
                status="error", notes="Order too small",
            )
            return

        order_args = OrderArgs(
            token_id=token_id,
            price=rounded_price,
            size=my_shares,
            side="BUY",
        )
        signed = clob.create_order(order_args)
        resp = clob.post_order(signed, OrderType.FOK)

        order_id = resp.get("orderID") or resp.get("order_id") if isinstance(resp, dict) else None
        order_status = (resp.get("status") or "").upper() if isinstance(resp, dict) else ""

        if order_status in ("CANCELLED", "EXPIRED", ""):
            status = "fok_no_fill"
            log.info("FOK no fill — %s %s on %s at %.2f", signal.strategy_name, signal.direction, market_label, rounded_price)
            await db.log_event("trade_fok_no_fill",
                f"FOK no fill — {signal.strategy_name} {signal.direction} on {market.market_type} at {rounded_price:.2f}", {
                    "market_id": market.market_id,
                    "market_type": market.market_type,
                    "strategy_name": signal.strategy_name,
                    "direction": signal.direction,
                    "entry_price": rounded_price,
                    "signal_data": signal.signal_data,
                })
        else:
            status = "filled"
            log.info(
                "TRADE PLACED — %s %s on %s | $%.2f (%.1fx) (%d shares) @ %.2f | order=%s",
                signal.strategy_name, signal.direction, market_label,
                bet_size, signal.confidence_multiplier, my_shares, rounded_price, order_id,
            )
            print(f"{Fore.GREEN}*** TRADE: {signal.strategy_name} {signal.direction} on {market_label} — ${bet_size:.2f} ({signal.confidence_multiplier:.1f}x) @ {rounded_price:.2f} ***{Style.RESET_ALL}")

            new_balance = await get_usdc_balance()

            await db.log_event("trade_placed",
                f"Placed {signal.direction} on {market.market_type} — strategy: {signal.strategy_name}", {
                    "market_id": market.market_id,
                    "market_type": market.market_type,
                    "strategy_name": signal.strategy_name,
                    "direction": signal.direction,
                    "entry_price": rounded_price,
                    "bet_size_usd": bet_size,
                    "confidence_multiplier": signal.confidence_multiplier,
                    "shares": my_shares,
                    "token_id": token_id,
                    "order_id": order_id,
                    "balance_after": new_balance if new_balance >= 0 else None,
                    "signal_data": signal.signal_data,
                })

    except Exception as exc:
        exc_msg = str(exc).lower()
        if "couldn't be fully filled" in exc_msg or "fully filled or killed" in exc_msg:
            status = "fok_no_fill"
            log.info("FOK no fill — %s %s — not enough liquidity", signal.strategy_name, market_label)
            await db.log_event("trade_fok_no_fill",
                f"FOK no fill — {signal.strategy_name} {signal.direction} on {market.market_type}", {
                    "market_id": market.market_id,
                    "strategy_name": signal.strategy_name,
                    "direction": signal.direction,
                    "reason": "not_enough_liquidity",
                })
        elif "min size" in exc_msg or "invalid amount" in exc_msg:
            status = "error"
            log.warning("Order too small for %s — %s", market_label, exc)
        elif "insufficient" in exc_msg or "balance" in exc_msg:
            status = "error"
            log.error("Insufficient funds for %s — skipping", market_label)
        elif "closed" in exc_msg or "resolved" in exc_msg:
            status = "error"
            log.warning("Market closed/resolved for %s — skipping", market_label)
        else:
            status = "error"
            log.exception("Order failed — %s", exc)
            await db.log_event("bot_error",
                f"Order failed for {market_label} — {exc}", {
                    "market_id": market.market_id,
                    "strategy_name": signal.strategy_name,
                    "error": str(exc),
                })

    trade_id = await db.insert_bot_trade(
        market_id=market.market_id,
        market_type=market.market_type,
        strategy_name=signal.strategy_name,
        direction=signal.direction,
        entry_price=best_price or signal.entry_price,
        bet_size_usd=bet_size,
        confidence_multiplier=signal.confidence_multiplier,
        shares=my_shares,
        token_id=token_id,
        condition_id=market.market_id,
        status=status,
        order_id=order_id,
    )

    # Place stop-loss GTC order after confirmed fill
    if status == "filled" and my_shares and trade_id and stop_loss_enabled:
        stop_loss_key = f"{signal.strategy_name}_use_stop_loss"
        exit_point_key = f"{signal.strategy_name}_stop_loss_exit_point"
        use_stop_loss = live_config.get(stop_loss_key, 'false') == 'true'
        if use_stop_loss:
            sl_exit = float(live_config.get(exit_point_key, '0.40'))
            log.info("[STOP-LOSS] Passing token_id to stop-loss: %s | direction: %s | this should be the %s token",
                     token_id, signal.direction, signal.direction)
            await place_stop_loss_order(
                clob=clob, p=db.pool(), trade_id=trade_id,
                token_id=token_id, shares=my_shares, stop_loss_price=sl_exit,
            )

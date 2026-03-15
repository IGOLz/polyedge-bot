"""Order execution — places trades on Polymarket based on strategy signals."""

from __future__ import annotations

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


def is_daily_limit_reached() -> bool:
    _reset_daily_if_needed()
    return _daily_net_loss >= config.DAILY_LOSS_LIMIT


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
        with config.get_sync_http_client(timeout=10.0) as client:
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

    except Exception:
        log.warning("Failed to fetch token IDs for condition %s", condition_id)

    return None


async def execute_trade(
    clob: ClobClient,
    market: db.MarketInfo,
    signal: Signal,
) -> None:
    """Place a trade based on a strategy signal. Records result to bot_trades."""
    _reset_daily_if_needed()

    market_label = f"{market.market_type}:{market.market_id[:12]}"

    # ── Calculate dynamic bet size ─────────────────────────────────────
    bet_size = round(config.BET_SIZE_USD * signal.confidence_multiplier, 2)
    bet_size = max(bet_size, 1.00)  # Polymarket minimum order

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
    if is_daily_limit_reached():
        log.warning("Daily loss limit reached — net loss today: $%.2f / $%.2f", _daily_net_loss, config.DAILY_LOSS_LIMIT)
        await db.insert_bot_trade(
            market_id=market.market_id, market_type=market.market_type,
            strategy_name=signal.strategy_name, direction=signal.direction,
            entry_price=signal.entry_price, bet_size_usd=bet_size,
            confidence_multiplier=signal.confidence_multiplier,
            status="skipped_daily_limit", condition_id=market.market_id,
        )
        await db.log_event("trade_skipped",
            f"Daily loss limit reached — net loss today: ${_daily_net_loss:.2f} / ${config.DAILY_LOSS_LIMIT:.2f}", {
                "market_id": market.market_id,
                "market_type": market.market_type,
                "strategy_name": signal.strategy_name,
                "direction": signal.direction,
                "reason": "daily_limit",
                "daily_net_loss": _daily_net_loss,
                "daily_loss_limit": config.DAILY_LOSS_LIMIT,
            })
        return

    # ── Guard: bankroll ─────────────────────────────────────────────────
    balance = await get_usdc_balance()
    if balance >= 0:
        min_runway = config.BET_SIZE_USD * 2
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

    if not up_token_id or not down_token_id:
        ids = _fetch_token_ids(clob, market.market_id)
        if ids is None:
            log.warning("Cannot resolve token IDs for %s — skipping", market_label)
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

    await db.insert_bot_trade(
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

"""Order execution — places trades on Polymarket based on strategy signals."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone

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


# ── Hybrid execution tracking ────────────────────────────────────────

class ExecutionStage:
    IDEAL_LIMIT = "stage_1_ideal"
    RELAXED_LIMIT = "stage_2_relaxed"
    MARKET_FOK = "stage_3_market"
    FAILED = "failed"

_STAGE_LABELS = {
    ExecutionStage.IDEAL_LIMIT: "Stage 1 (Ideal Limit)",
    ExecutionStage.RELAXED_LIMIT: "Stage 2 (Relaxed +1¢)",
    ExecutionStage.MARKET_FOK: "Stage 3 (Market FOK)",
}


class ExecutionMetrics:
    """Track hybrid execution fill rates and slippage across the session."""

    def __init__(self):
        self.total = 0
        self.filled = 0
        self.stage_1_fills = 0
        self.stage_2_fills = 0
        self.stage_3_fills = 0
        self.failed = 0
        self.total_slippage = 0.0
        self.total_time = 0.0

    def record(self, stage: str, filled: bool, slippage: float = 0.0, elapsed: float = 0.0):
        self.total += 1
        self.total_time += elapsed
        if not filled:
            self.failed += 1
            return
        self.filled += 1
        self.total_slippage += slippage
        if stage == ExecutionStage.IDEAL_LIMIT:
            self.stage_1_fills += 1
        elif stage == ExecutionStage.RELAXED_LIMIT:
            self.stage_2_fills += 1
        elif stage == ExecutionStage.MARKET_FOK:
            self.stage_3_fills += 1

    def summary(self) -> str:
        rate = (self.filled / self.total * 100) if self.total > 0 else 0
        avg_slip = (self.total_slippage / self.filled) if self.filled > 0 else 0
        avg_time = (self.total_time / self.total) if self.total > 0 else 0
        return (
            f"Orders: {self.total} | Filled: {self.filled} ({rate:.1f}%) | "
            f"S1: {self.stage_1_fills} S2: {self.stage_2_fills} S3: {self.stage_3_fills} | "
            f"Failed: {self.failed} | Avg Slippage: ${avg_slip:.3f} | Avg Time: {avg_time:.2f}s"
        )

    def reset(self):
        self.__init__()


_exec_metrics = ExecutionMetrics()


class VarianceMetrics:
    """Track execution variance from locked signal parameters."""

    def __init__(self):
        self.total_trades = 0
        self.perfect_executions = 0
        self.acceptable_variance = 0
        self.unacceptable_variance = 0
        self.total_price_variance = 0.0
        self.total_shares_variance = 0
        self.total_signal_age = 0.0

    def add_execution(self, price_variance_pct: float, shares_variance: int, signal_age_seconds: float):
        self.total_trades += 1
        if price_variance_pct == 0 and shares_variance == 0:
            self.perfect_executions += 1
        elif abs(price_variance_pct) < 1.0:
            self.acceptable_variance += 1
        else:
            self.unacceptable_variance += 1
        self.total_price_variance += price_variance_pct
        self.total_shares_variance += shares_variance
        self.total_signal_age += signal_age_seconds

    def summary(self) -> str:
        if self.total_trades == 0:
            return "No variance data yet"
        avg_pv = self.total_price_variance / self.total_trades
        avg_sv = self.total_shares_variance / self.total_trades
        avg_age = self.total_signal_age / self.total_trades
        perfect_pct = self.perfect_executions / self.total_trades * 100
        acceptable_pct = self.acceptable_variance / self.total_trades * 100
        bad_pct = self.unacceptable_variance / self.total_trades * 100
        return (
            f"Trades: {self.total_trades} | Perfect: {perfect_pct:.1f}% | "
            f"Acceptable (<1%): {acceptable_pct:.1f}% | Bad (>1%): {bad_pct:.1f}% | "
            f"Avg price variance: {avg_pv:+.2f}% | Avg shares variance: {avg_sv:+.1f} | "
            f"Avg signal age: {avg_age:.2f}s"
        )

    def reset(self):
        self.__init__()


_variance_metrics = VarianceMetrics()


def get_execution_metrics() -> ExecutionMetrics:
    return _exec_metrics


def get_variance_metrics() -> VarianceMetrics:
    return _variance_metrics


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


async def _wait_for_fill(clob: ClobClient, order_id: str, timeout: float) -> tuple[bool, dict | None]:
    """Poll order status until filled or timeout. Returns (filled, order_details)."""
    loop = asyncio.get_event_loop()
    start = time.time()
    while time.time() - start < timeout:
        try:
            order = await asyncio.wait_for(
                loop.run_in_executor(None, lambda oid=order_id: clob.get_order(oid)),
                timeout=3.0,
            )
            if isinstance(order, dict):
                status = (order.get("status") or "").upper()
                if status in ("MATCHED", "FILLED"):
                    return True, order
                if status in ("CANCELLED", "EXPIRED"):
                    return False, order
        except Exception:
            pass
        await asyncio.sleep(0.15)
    return False, None


async def _cancel_open_order(clob: ClobClient, order_id: str) -> bool:
    """Cancel an open GTC order. Returns True on success."""
    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda oid=order_id: clob.cancel(oid)),
            timeout=5.0,
        )
        return True
    except Exception as e:
        log.warning("[EXEC] Failed to cancel order %s: %s", order_id[:16] if order_id else "?", e)
        return False


def _parse_fill_from_resp(resp: dict | None, fallback_shares: int, fallback_price: float) -> tuple[int, float]:
    """Extract fill shares and price from an order response dict."""
    fill_shares = fallback_shares
    fill_price = fallback_price
    if isinstance(resp, dict):
        raw_shares = resp.get("size_matched") or resp.get("matched_size") or resp.get("filled")
        raw_price = resp.get("average_price") or resp.get("price")
        if raw_shares is not None:
            fill_shares = int(float(raw_shares))
        if raw_price is not None:
            fill_price = float(raw_price)
    return fill_shares, fill_price


async def _execute_hybrid(
    clob: ClobClient,
    token_id: str,
    ideal_price: float,
    shares: int,
) -> dict:
    """Execute order using 3-stage hybrid strategy.

    Stage 1: GTC limit at ideal price   (1.0s poll timeout)
    Stage 2: GTC limit at ideal + $0.01 (1.0s poll timeout)
    Stage 3: FOK at ideal + $0.02       (immediate fill-or-kill)

    Returns dict with: filled, order_id, fill_price, fill_shares, stage,
                       slippage, elapsed, error_status, error_notes
    """
    start_time = time.time()
    loop = asyncio.get_event_loop()

    result: dict = {
        "filled": False,
        "order_id": None,
        "fill_price": None,
        "fill_shares": None,
        "stage": ExecutionStage.FAILED,
        "slippage": 0.0,
        "elapsed": 0.0,
        "error_status": "hybrid_no_fill",
        "error_notes": None,
    }

    # (stage_name, price_offset, poll_timeout, order_type)
    stages = [
        (ExecutionStage.IDEAL_LIMIT,   0.00, 1.0, OrderType.GTC),
        (ExecutionStage.RELAXED_LIMIT, 0.01, 1.0, OrderType.GTC),
        (ExecutionStage.MARKET_FOK,    0.02, None, OrderType.FOK),
    ]

    for stage_name, offset, poll_timeout, order_type in stages:
        price = round(ideal_price + offset, 2)
        if price <= 0 or price >= 1:
            continue
        label = _STAGE_LABELS.get(stage_name, stage_name)
        slippage_cents = round(offset * 100)

        log.info(
            "[EXEC] %s | BUY %d shares @ $%.2f (+%d¢) | %s | timeout: %s",
            label, shares, price, slippage_cents,
            "GTC" if order_type == OrderType.GTC else "FOK",
            f"{poll_timeout}s" if poll_timeout else "immediate",
        )

        try:
            def _place(p=price, s=shares, ot=order_type):
                order_args = OrderArgs(token_id=token_id, price=p, size=s, side="BUY")
                signed = clob.create_order(order_args)
                return clob.post_order(signed, ot)

            resp = await asyncio.wait_for(
                loop.run_in_executor(None, _place),
                timeout=5.0,
            )

            order_id = (resp.get("orderID") or resp.get("order_id")) if isinstance(resp, dict) else None
            order_status = (resp.get("status") or "").upper() if isinstance(resp, dict) else ""

            if order_type == OrderType.FOK:
                # FOK: check immediate fill from response
                if order_status in ("CANCELLED", "EXPIRED", ""):
                    log.info("[EXEC] %s — no fill (FOK)", label)
                    continue

                fill_shares, fill_price = _parse_fill_from_resp(resp, shares, price)
                elapsed = time.time() - start_time
                slippage = (fill_price - ideal_price) * fill_shares

                _exec_metrics.record(stage_name, True, slippage, elapsed)
                log.info(
                    "[EXEC] ✅ %s filled | %d shares @ $%.4f | slippage: $%.3f | time: %.2fs",
                    label, fill_shares, fill_price, slippage, elapsed,
                )
                result.update({
                    "filled": True, "order_id": order_id,
                    "fill_price": fill_price, "fill_shares": fill_shares,
                    "stage": stage_name, "slippage": slippage, "elapsed": elapsed,
                })
                return result

            else:
                # GTC: check if immediately matched, otherwise poll
                if order_status in ("MATCHED", "FILLED"):
                    fill_shares, fill_price = _parse_fill_from_resp(resp, shares, price)
                    elapsed = time.time() - start_time
                    slippage = (fill_price - ideal_price) * fill_shares

                    _exec_metrics.record(stage_name, True, slippage, elapsed)
                    log.info(
                        "[EXEC] ✅ %s filled (immediate) | %d shares @ $%.4f | slippage: $%.3f | time: %.2fs",
                        label, fill_shares, fill_price, slippage, elapsed,
                    )
                    result.update({
                        "filled": True, "order_id": order_id,
                        "fill_price": fill_price, "fill_shares": fill_shares,
                        "stage": stage_name, "slippage": slippage, "elapsed": elapsed,
                    })
                    return result

                if not order_id:
                    log.warning("[EXEC] %s — no order ID returned, skipping stage", label)
                    continue

                # Poll for fill
                filled, order_detail = await _wait_for_fill(clob, order_id, poll_timeout)
                if filled:
                    fill_shares, fill_price = _parse_fill_from_resp(order_detail, shares, price)
                    elapsed = time.time() - start_time
                    slippage = (fill_price - ideal_price) * fill_shares

                    _exec_metrics.record(stage_name, True, slippage, elapsed)
                    log.info(
                        "[EXEC] ✅ %s filled | %d shares @ $%.4f | slippage: $%.3f | time: %.2fs",
                        label, fill_shares, fill_price, slippage, elapsed,
                    )
                    result.update({
                        "filled": True, "order_id": order_id,
                        "fill_price": fill_price, "fill_shares": fill_shares,
                        "stage": stage_name, "slippage": slippage, "elapsed": elapsed,
                    })
                    return result

                # Not filled — cancel and move to next stage
                log.info("[EXEC] ⏱️ %s timeout after %.1fs — cancelling", label, poll_timeout)
                await _cancel_open_order(clob, order_id)

        except asyncio.TimeoutError:
            log.warning("[EXEC] %s — API timeout, continuing to next stage", label)
            continue

        except Exception as exc:
            exc_msg = str(exc).lower()
            # Fatal errors — stop all stages
            if "min size" in exc_msg or "invalid amount" in exc_msg:
                result["error_status"] = "error"
                result["error_notes"] = str(exc)[:500]
                log.warning("[EXEC] %s — order too small: %s", label, exc)
                break
            elif "insufficient" in exc_msg or "balance" in exc_msg:
                result["error_status"] = "error"
                result["error_notes"] = str(exc)[:500]
                log.error("[EXEC] %s — insufficient funds: %s", label, exc)
                break
            elif "closed" in exc_msg or "resolved" in exc_msg:
                result["error_status"] = "error"
                result["error_notes"] = str(exc)[:500]
                log.warning("[EXEC] %s — market closed/resolved", label)
                break
            # Non-fatal — continue to next stage
            elif "couldn't be fully filled" in exc_msg or "fully filled or killed" in exc_msg:
                log.info("[EXEC] %s — no fill (FOK exception)", label)
                continue
            else:
                log.warning("[EXEC] %s — error: %s, continuing", label, exc)
                continue

    # All stages exhausted or fatal error
    elapsed = time.time() - start_time
    result["elapsed"] = elapsed
    if result["error_status"] == "hybrid_no_fill":
        _exec_metrics.record(ExecutionStage.FAILED, False, 0.0, elapsed)
        log.error("[EXEC] ❌ All stages exhausted after %.2fs — order not filled", elapsed)
    return result


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

    # ── Use locked parameters from signal (never recalculate) ──────────
    ideal_price = round(signal.entry_price, 2)
    locked_shares = signal.locked_shares if signal.locked_shares > 0 else max(
        1, math.floor(float(signal.signal_data.get('bet_cost', config.BET_SIZE_USD)) / signal.entry_price)
    ) if signal.entry_price > 0 else 1
    bet_size = signal.locked_cost if signal.locked_cost > 0 else round(
        float(signal.signal_data.get('bet_cost', config.BET_SIZE_USD)), 2
    )
    bet_size = max(bet_size, 1.00)
    tag = 'M3' if 'M3' in signal.strategy_name else 'M4'
    log.info(
        "[BET] %s on %s — $%.2f (%d shares @ $%.4f) [LOCKED]",
        signal.strategy_name, market.market_type,
        bet_size, locked_shares, ideal_price,
    )

    # ── Dry-run mode ────────────────────────────────────────────────────
    if config.DRY_RUN:
        log.info(
            "[DRY RUN] Would place BUY %s on %s at %.4f — strategy: %s ($%.2f)",
            signal.direction, market_label, signal.entry_price,
            signal.strategy_name, bet_size,
        )
        await db.insert_bot_trade(
            market_id=market.market_id, market_type=market.market_type,
            strategy_name=signal.strategy_name, direction=signal.direction,
            entry_price=signal.entry_price, bet_size_usd=bet_size,
            status="dry_run", condition_id=market.market_id,
            signal_data=signal.signal_data,
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
            status="skipped_daily_limit", condition_id=market.market_id,
            signal_data=signal.signal_data,
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
        min_runway = bet_size * 2
        if balance < min_runway:
            log.critical("Bankroll critically low ($%.2f < $%.2f) — bot paused", balance, min_runway)
            await db.insert_bot_trade(
                market_id=market.market_id, market_type=market.market_type,
                strategy_name=signal.strategy_name, direction=signal.direction,
                entry_price=signal.entry_price, bet_size_usd=bet_size,
                status="skipped_bankroll", condition_id=market.market_id,
                signal_data=signal.signal_data,
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
                status="error", condition_id=market.market_id,
                notes="Failed to resolve token IDs",
                signal_data=signal.signal_data,
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

    # ── Validate locked price ──────────────────────────────────────────
    status = "error"
    order_id = None
    my_shares: float | None = locked_shares
    actual_price: float | None = None
    error_notes: str | None = None
    price_variance: float | None = None
    price_variance_pct: float | None = None
    shares_variance: int | None = None
    signal_age_s: float | None = None

    if ideal_price <= 0 or ideal_price >= 1:
        log.warning("Locked price %.2f out of range — skipping %s", ideal_price, market_label)
        await db.insert_bot_trade(
            market_id=market.market_id, market_type=market.market_type,
            strategy_name=signal.strategy_name, direction=signal.direction,
            entry_price=signal.entry_price, bet_size_usd=bet_size,
            token_id=token_id, condition_id=market.market_id,
            status="error", notes=f"Locked price out of range: {ideal_price}",
            signal_data=signal.signal_data,
        )
        return

    # ── Pre-order price validation against strategy range ──────────────
    pre_price_min = float(signal.signal_data.get('price_min', live_config.get('price_min', '0.01')))
    pre_price_max = float(signal.signal_data.get('price_max', live_config.get('price_max', '0.99')))
    if ideal_price < pre_price_min or ideal_price > pre_price_max:
        log.warning(
            "Locked price %.4f outside strategy range [%.2f, %.2f] — skipping %s",
            ideal_price, pre_price_min, pre_price_max, market_label,
        )
        await db.insert_bot_trade(
            market_id=market.market_id, market_type=market.market_type,
            strategy_name=signal.strategy_name, direction=signal.direction,
            entry_price=signal.entry_price, bet_size_usd=bet_size,
            token_id=token_id, condition_id=market.market_id,
            status="skipped_price_range",
            notes=f"Locked price {ideal_price:.4f} outside [{pre_price_min:.2f}, {pre_price_max:.2f}]",
            signal_data=signal.signal_data,
        )
        return

    # ── Minimum shares check ──────────────────────────────────────────
    min_shares = math.ceil(MIN_DOLLAR_SIZE / ideal_price) if ideal_price > 0 else 1
    if locked_shares < min_shares:
        locked_shares = min_shares
        my_shares = locked_shares

    if locked_shares < 1:
        log.warning("Cannot meet $1 minimum at price %.2f — skipping", ideal_price)
        await db.insert_bot_trade(
            market_id=market.market_id, market_type=market.market_type,
            strategy_name=signal.strategy_name, direction=signal.direction,
            entry_price=signal.entry_price, bet_size_usd=bet_size,
            token_id=token_id, condition_id=market.market_id,
            status="error", notes="Order too small",
            signal_data=signal.signal_data,
        )
        return

    # Log time elapsed since signal was generated
    signal_age_ms = (datetime.now(timezone.utc) - signal.created_at).total_seconds() * 1000
    log.info("[TIMING] Signal age at order submission: %.0fms — %s %s on %s",
             signal_age_ms, signal.strategy_name, signal.direction, market_label)

    # ── Execute with hybrid strategy using LOCKED parameters ─────────
    hybrid = await _execute_hybrid(clob, token_id, ideal_price, locked_shares)

    order_id = hybrid["order_id"]
    error_notes = hybrid.get("error_notes")
    execution_stage = hybrid["stage"]

    if hybrid["filled"]:
        actual_shares = hybrid["fill_shares"]
        actual_price = hybrid["fill_price"]
        actual_cost = actual_shares * actual_price

        # ── Variance tracking (locked vs actual) ──────────────────────
        price_variance = round(actual_price - ideal_price, 6)
        price_variance_pct = round((price_variance / ideal_price) * 100, 4) if ideal_price > 0 else 0.0
        shares_variance = actual_shares - locked_shares
        signal_age_s = round((datetime.now(timezone.utc) - signal.created_at).total_seconds(), 2)
        _variance_metrics.add_execution(price_variance_pct, shares_variance, signal_age_s)

        log.info(
            "[%s] LOCKED: $%.4f x %dsh = $%.2f | ACTUAL: $%.4f x %dsh = $%.2f | "
            "Variance: %+.2f%% (%+dsh) | Age: %.2fs",
            tag, ideal_price, locked_shares, bet_size,
            actual_price, actual_shares, actual_cost,
            price_variance_pct, shares_variance, signal_age_s,
        )
        if abs(price_variance_pct) > 1.0:
            log.warning("[%s] High price variance: %+.2f%% (locked: $%.4f, actual: $%.4f)",
                        tag, price_variance_pct, ideal_price, actual_price)
        if shares_variance != 0:
            log.warning("[%s] Share variance: %+d shares (locked: %d, actual: %d)",
                        tag, shares_variance, locked_shares, actual_shares)

        # Post-fill price validation against strategy range
        fill_price_min = float(signal.signal_data.get('price_min', live_config.get('price_min', '0.01')))
        fill_price_max = float(signal.signal_data.get('price_max', live_config.get('price_max', '0.99')))
        if actual_price < fill_price_min or actual_price > fill_price_max:
            status = "filled_price_rejected"
            my_shares = actual_shares
            bet_size = round(actual_cost, 2)
            log.warning(
                "FILL PRICE REJECTED — %s %s on %s | filled @ %.4f outside [%.2f, %.2f] | locked was %.4f | stage: %s",
                signal.strategy_name, signal.direction, market_label,
                actual_price, fill_price_min, fill_price_max,
                ideal_price, execution_stage,
            )
            await db.log_event("trade_fill_price_rejected",
                f"Fill price {actual_price:.4f} outside [{fill_price_min:.2f}, {fill_price_max:.2f}] on {market.market_type}", {
                    "market_id": market.market_id,
                    "market_type": market.market_type,
                    "strategy_name": signal.strategy_name,
                    "direction": signal.direction,
                    "actual_price": actual_price,
                    "locked_price": ideal_price,
                    "price_variance": price_variance,
                    "price_variance_pct": price_variance_pct,
                    "price_min": fill_price_min,
                    "price_max": fill_price_max,
                    "shares": actual_shares,
                    "order_id": order_id,
                    "execution_stage": execution_stage,
                    "signal_data": signal.signal_data,
                })
        else:
            status = "filled"
            my_shares = actual_shares
            bet_size = round(actual_cost, 2)

            log.info(
                "TRADE FILLED — %s %s on %s | %d shares @ %.4f ($%.2f) | stage: %s | slippage: $%.3f | time: %.2fs | order=%s",
                signal.strategy_name, signal.direction, market_label,
                actual_shares, actual_price, actual_cost,
                execution_stage, hybrid["slippage"], hybrid["elapsed"],
                order_id,
            )
            new_balance = await get_usdc_balance()

            await db.log_event("trade_placed",
                f"Placed {signal.direction} on {market.market_type} — strategy: {signal.strategy_name}", {
                    "market_id": market.market_id,
                    "market_type": market.market_type,
                    "strategy_name": signal.strategy_name,
                    "direction": signal.direction,
                    "entry_price": actual_price,
                    "locked_entry_price": ideal_price,
                    "bet_size_usd": actual_cost,
                    "shares": actual_shares,
                    "locked_shares": locked_shares,
                    "price_variance": price_variance,
                    "price_variance_pct": price_variance_pct,
                    "shares_variance": shares_variance,
                    "signal_age_seconds": signal_age_s,
                    "execution_stage": execution_stage,
                    "slippage": round(hybrid["slippage"], 4),
                    "execution_time": round(hybrid["elapsed"], 2),
                    "token_id": token_id,
                    "order_id": order_id,
                    "balance_after": new_balance if new_balance >= 0 else None,
                    "signal_data": signal.signal_data,
                })
    else:
        status = hybrid.get("error_status", "hybrid_no_fill")
        actual_price = None

        if status == "hybrid_no_fill":
            log.info("Hybrid no fill — %s %s on %s at %.4f [LOCKED] (all stages exhausted in %.2fs)",
                     signal.strategy_name, signal.direction, market_label,
                     ideal_price, hybrid["elapsed"])
            await db.log_event("trade_hybrid_no_fill",
                f"Hybrid no fill — {signal.strategy_name} {signal.direction} on {market.market_type} at {ideal_price:.4f} [LOCKED]", {
                    "market_id": market.market_id,
                    "market_type": market.market_type,
                    "strategy_name": signal.strategy_name,
                    "direction": signal.direction,
                    "locked_entry_price": ideal_price,
                    "locked_shares": locked_shares,
                    "execution_time": round(hybrid["elapsed"], 2),
                    "signal_data": signal.signal_data,
                })

    # ── Record trade in database ─────────────────────────────────────
    trade_id = await db.insert_bot_trade(
        market_id=market.market_id,
        market_type=market.market_type,
        strategy_name=signal.strategy_name,
        direction=signal.direction,
        entry_price=actual_price if status == "filled" and actual_price is not None else signal.entry_price,
        bet_size_usd=bet_size,
        shares=my_shares,
        token_id=token_id,
        condition_id=market.market_id,
        status=status,
        order_id=order_id,
        notes=error_notes if status == "error" else None,
        signal_data=signal.signal_data,
        execution_stage=execution_stage if hybrid["filled"] else None,
        locked_entry_price=ideal_price,
        locked_shares_count=locked_shares,
        locked_cost=signal.locked_cost,
        locked_balance=signal.locked_balance,
        price_variance_val=price_variance,
        price_variance_pct_val=price_variance_pct,
        shares_variance_count=shares_variance,
        signal_generated_at=signal.created_at,
        signal_age_seconds=signal_age_s,
    )

    # Place stop-loss GTC order after confirmed fill
    if status == "filled" and my_shares and trade_id and stop_loss_enabled:
        sl_price = signal.signal_data.get('stop_loss_price')
        if sl_price is not None:
            sl_exit = float(sl_price)
            log.info("[STOP-LOSS] Placing stop-loss @ %.2f | token: %s | direction: %s",
                     sl_exit, token_id[:16], signal.direction)
            await place_stop_loss_order(
                clob=clob, p=db.pool(), trade_id=trade_id,
                token_id=token_id, shares=my_shares, stop_loss_price=sl_exit,
            )

"""Strategy evaluation logic for Polymarket 5m/15m crypto markets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import db
from balance import get_usdc_balance
from utils import log


@dataclass
class Signal:
    direction: str        # 'Up' or 'Down'
    strategy_name: str
    entry_price: float    # price of the token we'd buy
    signal_data: dict[str, Any] = field(default_factory=dict)
    confidence_multiplier: float = 1.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def calculate_shares(balance: float, entry_price: float, live_config: dict) -> int:
    """
    Calculate number of shares to buy based on balance percentage.
    Returns integer shares, minimum 1.
    bet_pct is read from live_config as a decimal e.g. 0.02 = 2%
    actual cost = shares * entry_price
    """
    bet_pct = float(live_config.get('momentum_bet_pct', '0.02'))
    dollar_amount = balance * bet_pct
    shares = int(dollar_amount / entry_price)
    return max(shares, 1)


async def evaluate_momentum(market: db.MarketInfo, live_config: dict) -> Signal | None:
    """Single momentum strategy built from backtesting analysis."""

    # Guard 1 — Market type
    if '15m' in market.market_type:
        return None

    # Guard 2 — Market filter
    market_filter = live_config.get('momentum_markets', 'xrp_sol_only')
    if market_filter == 'xrp_sol_only':
        if 'xrp' not in market.market_type and 'sol' not in market.market_type:
            return None
    elif market_filter == 'no_btc':
        if 'btc' in market.market_type:
            return None

    # Guard 3 — Hour filter
    hours_start = int(live_config.get('momentum_hours_start', '0'))
    hours_end = int(live_config.get('momentum_hours_end', '24'))
    current_hour = datetime.now(timezone.utc).hour
    if not (hours_start <= current_hour < hours_end):
        return None

    # Guard 4 — Timing window
    seconds_elapsed = (datetime.now(timezone.utc) - market.started_at).total_seconds()
    entry_after = int(live_config.get('momentum_entry_after_seconds', '65'))
    entry_until = int(live_config.get('momentum_entry_until_seconds', '90'))
    if seconds_elapsed < entry_after or seconds_elapsed > entry_until:
        return None

    # Context filter (opening price awareness)
    price_open_seconds = int(live_config.get('momentum_price_open_seconds', '0'))
    context_max_delta = live_config.get('momentum_context_max_delta', '0.1')
    context_enabled = context_max_delta not in ('off', 'none', '', 'null')
    context_delta = float(context_max_delta) if context_enabled else None

    price_open = await db.get_price_at_second(market.market_id, market.started_at, price_open_seconds)

    # Signal samples
    price_a_sec = int(live_config.get('momentum_price_a_seconds', '45'))
    price_b_sec = int(live_config.get('momentum_price_b_seconds', '60'))

    price_a = await db.get_price_at_second(market.market_id, market.started_at, price_a_sec)
    price_b = await db.get_price_at_second(market.market_id, market.started_at, price_b_sec)

    if price_a is None or price_b is None:
        return None

    momentum = price_b - price_a

    # Threshold check
    threshold = float(live_config.get('momentum_threshold', '0.10'))
    if abs(momentum) < threshold:
        return None

    # Direction
    if momentum > 0:
        direction = 'Up'
        entry_price = price_b
    else:
        direction = 'Down'
        entry_price = 1 - price_b

    # Direction filter (default: up_only — Down signals show no edge on current dataset)
    direction_filter = live_config.get('momentum_direction', 'up_only')
    if direction_filter == 'up_only' and direction == 'Down':
        return None
    if direction_filter == 'down_only' and direction == 'Up':
        return None
    # 'both' passes through with no filter

    # Context filter check
    if context_enabled and context_delta is not None and price_open is not None:
        open_delta = price_b - price_open
        if direction == 'Down' and open_delta > context_delta:
            return None
        if direction == 'Up' and open_delta < -context_delta:
            return None

    # Entry price range filter
    price_min = float(live_config.get('momentum_price_min', '0.50'))
    price_max = float(live_config.get('momentum_price_max', '0.75'))
    if entry_price < price_min or entry_price > price_max:
        return None

    # Position sizing
    balance = await get_usdc_balance()
    if balance <= 0:
        fallback = float(live_config.get('momentum_fallback_shares', '2'))
        log.warning("Could not fetch balance (got %.2f), using fallback shares: %d", balance, int(fallback))
        shares = int(fallback)
    else:
        shares = calculate_shares(balance, entry_price, live_config)
    bet_cost = shares * entry_price

    # Stop-loss
    sl_enabled = live_config.get('momentum_stop_loss_enabled', 'true') == 'true'
    sl_price = float(live_config.get('momentum_stop_loss_price', '0.35'))
    sl_active = sl_enabled and bet_cost >= 5.0

    # Return signal
    return Signal(
        direction=direction,
        strategy_name='momentum',
        entry_price=entry_price,
        signal_data={
            'price_a_seconds': price_a_sec,
            'price_b_seconds': price_b_sec,
            'price_a': price_a,
            'price_b': price_b,
            'price_open': price_open,
            'momentum_value': round(momentum, 6),
            'entry_price': entry_price,
            'seconds_elapsed': round(seconds_elapsed, 1),
            'shares': shares,
            'bet_cost': round(bet_cost, 4),
            'stop_loss_price': sl_price if sl_active else None,
            'balance_at_signal': balance,
        }
    )


async def evaluate_strategies(market: db.MarketInfo, live_config: dict) -> list[Signal]:
    if live_config.get('strategy_momentum_enabled', 'false') != 'true':
        return []
    if await db.already_traded_this_market(market.market_id, 'momentum'):
        return []
    signal = await evaluate_momentum(market, live_config)
    if signal:
        log.info(
            "Signal: momentum %s on %s (price=%.4f, shares=%d, cost=$%.2f, sl=%s)",
            signal.direction,
            market.market_id[:16],
            signal.entry_price,
            signal.signal_data.get('shares', 0),
            signal.signal_data.get('bet_cost', 0),
            signal.signal_data.get('stop_loss_price') or 'off'
        )
        return [signal]
    return []

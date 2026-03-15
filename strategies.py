"""Strategy evaluation logic for Polymarket 5m/15m crypto markets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import config
import db
from utils import log


@dataclass
class Signal:
    direction: str        # 'Up' or 'Down'
    strategy_name: str
    entry_price: float    # price of the token we'd buy
    signal_data: dict[str, Any] = field(default_factory=dict)


async def evaluate_farming(market: db.MarketInfo) -> Signal | None:
    """Farming: bet on strong directional moves after a brief warm-up."""
    if market.market_type in ('btc_5m', 'btc_15m'):
        return None
    if '5m' in market.market_type:
        return None
    now = datetime.now(timezone.utc)
    seconds_elapsed = (now - market.started_at).total_seconds()

    if seconds_elapsed < config.FARMING_TRIGGER_MINUTES * 60:
        return None

    current_price = await db.get_latest_price(market.market_id)
    if current_price is None:
        return None

    if current_price >= config.FARMING_TRIGGER_POINT:
        return Signal("Up", "farming", current_price, {
            "current_price": current_price,
            "trigger_point": config.FARMING_TRIGGER_POINT,
            "seconds_elapsed": round(seconds_elapsed, 1),
        })
    if current_price <= (1 - config.FARMING_TRIGGER_POINT):
        return Signal("Down", "farming", 1 - current_price, {
            "current_price": current_price,
            "trigger_point": config.FARMING_TRIGGER_POINT,
            "seconds_elapsed": round(seconds_elapsed, 1),
        })

    return None


async def evaluate_momentum(market: db.MarketInfo) -> Signal | None:
    """Momentum: bet in the direction of early price movement."""
    now = datetime.now(timezone.utc)
    seconds_elapsed = (now - market.started_at).total_seconds()

    # Need at least 65s of data to have reliable 60s price
    if seconds_elapsed < 65:
        return None

    price_30s = await db.get_price_at_second(market.market_id, market.started_at, 30)
    price_60s = await db.get_price_at_second(market.market_id, market.started_at, 60)

    if price_30s is None or price_60s is None:
        return None

    momentum = price_60s - price_30s

    sig_data = {
        "price_30s": price_30s,
        "price_60s": price_60s,
        "momentum_value": round(momentum, 6),
    }

    if momentum >= config.MOMENTUM_MIN_THRESHOLD:
        return Signal("Up", "momentum", price_60s, sig_data)
    if momentum <= -config.MOMENTUM_MIN_THRESHOLD:
        return Signal("Down", "momentum", 1 - price_60s, sig_data)

    return None


async def evaluate_streak(market: db.MarketInfo) -> Signal | None:
    """Streak: fade consecutive same-direction outcomes (mean reversion)."""
    recent = await db.get_recent_outcomes(market.market_type, config.STREAK_LENGTH)
    if len(recent) < config.STREAK_LENGTH:
        return None

    current_price = await db.get_latest_price(market.market_id)
    if current_price is None:
        return None

    sig_data = {
        "streak_length": config.STREAK_LENGTH,
        "streak_direction": config.STREAK_DIRECTION,
        "recent_outcomes": recent,
    }

    # Check if all recent outcomes are the same — then fade the streak
    if all(o == "Up" for o in recent) and config.STREAK_DIRECTION in ("Up", "both"):
        return Signal("Down", "streak", 1 - current_price, sig_data)

    if all(o == "Down" for o in recent) and config.STREAK_DIRECTION in ("Down", "both"):
        return Signal("Up", "streak", current_price, sig_data)

    return None


async def evaluate_calibration(market: db.MarketInfo) -> Signal | None:
    """Calibration: exploit early mispricing based on historical deviation data."""
    # Only valid for 5m markets
    if "15m" in market.market_type:
        return None

    now = datetime.now(timezone.utc)
    seconds_elapsed = (now - market.started_at).total_seconds()

    if seconds_elapsed > config.CALIBRATION_MAX_ENTRY_SECONDS:
        return None

    current_price = await db.get_latest_price(market.market_id)
    if current_price is None:
        return None

    if not (config.CALIBRATION_ENTRY_LOW <= current_price <= config.CALIBRATION_ENTRY_HIGH):
        return None

    # Round to nearest 0.05 bucket
    bucket = round(round(current_price / 0.05) * 0.05, 2)
    deviation = await db.get_calibration_deviation(market.market_type, bucket)

    if deviation is None or abs(deviation) < config.CALIBRATION_MIN_DEVIATION:
        return None

    sig_data = {
        "current_price": current_price,
        "bucket": bucket,
        "deviation": round(deviation, 6),
    }

    if deviation < -config.CALIBRATION_MIN_DEVIATION:
        return Signal("Down", "calibration", 1 - current_price, sig_data)
    if deviation > config.CALIBRATION_MIN_DEVIATION:
        return Signal("Up", "calibration", current_price, sig_data)

    return None


async def evaluate_strategies(market: db.MarketInfo) -> Signal | None:
    """Try each enabled strategy in priority order. Return first signal found."""

    if config.STRATEGY_MOMENTUM_ENABLED:
        # Only evaluate if not already traded this market with this strategy
        if not await db.already_traded_this_market(market.market_id, "momentum"):
            signal = await evaluate_momentum(market)
            if signal:
                log.info("Signal: %s %s on %s (price=%.4f)",
                         signal.strategy_name, signal.direction,
                         market.market_id[:16], signal.entry_price)
                return signal

    if config.STRATEGY_STREAK_ENABLED:
        if not await db.already_traded_this_market(market.market_id, "streak"):
            signal = await evaluate_streak(market)
            if signal:
                log.info("Signal: %s %s on %s (price=%.4f)",
                         signal.strategy_name, signal.direction,
                         market.market_id[:16], signal.entry_price)
                return signal

    if config.STRATEGY_CALIBRATION_ENABLED:
        if not await db.already_traded_this_market(market.market_id, "calibration"):
            signal = await evaluate_calibration(market)
            if signal:
                log.info("Signal: %s %s on %s (price=%.4f)",
                         signal.strategy_name, signal.direction,
                         market.market_id[:16], signal.entry_price)
                return signal

    if config.STRATEGY_FARMING_ENABLED:
        if not await db.already_traded_this_market(market.market_id, "farming"):
            signal = await evaluate_farming(market)
            if signal:
                log.info("Signal: %s %s on %s (price=%.4f)",
                         signal.strategy_name, signal.direction,
                         market.market_id[:16], signal.entry_price)
                return signal

    return None

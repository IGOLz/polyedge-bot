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
    confidence_multiplier: float = 1.0


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


async def evaluate_late_dip_recovery(market: db.MarketInfo) -> Signal | None:
    """Late dip recovery: buy Up when a strong uptrend dips late in a 15m window."""
    # Only 15m markets
    if '5m' in market.market_type:
        return None

    now = datetime.now(timezone.utc)
    seconds_elapsed = (now - market.started_at).total_seconds()

    # Only activate between minute 10 and minute 14
    if seconds_elapsed < 600:
        return None
    if seconds_elapsed > 840:
        return None

    current_price = await db.get_latest_price(market.market_id)
    if current_price is None:
        return None

    # Get the average price between minute 5 and minute 10
    avg_price_5_to_10 = await db.get_average_price_between(
        market.market_id, market.started_at, 300, 600
    )
    if avg_price_5_to_10 is None:
        return None

    # Market must have been clearly Up during minutes 5-10
    if avg_price_5_to_10 < 0.65:
        return None

    # Current price must have dropped significantly
    drop = avg_price_5_to_10 - current_price
    if drop < 0.20:
        return None

    # Must actually be in a dip
    if current_price > 0.55:
        return None

    log.info("[CONFIDENCE] late_dip_recovery on %s — avg_5_10: %.2f, current: %.2f, drop: %.2f",
             market.market_type, avg_price_5_to_10, current_price, drop)

    return Signal('Up', 'late_dip_recovery', current_price, signal_data={
        'avg_price_5_to_10': avg_price_5_to_10,
        'current_price': current_price,
        'drop': round(drop, 4),
        'seconds_elapsed': seconds_elapsed,
    })


def calculate_confidence(signal_type: str, signal_data: dict) -> float:
    """
    Returns a multiplier between BET_SIZE_MIN_MULTIPLIER and BET_SIZE_MAX_MULTIPLIER.
    Higher = more confident = bigger bet.
    """
    if signal_type == 'momentum':
        momentum_value = abs(signal_data.get('momentum_value', 0.02))
        entry_price = signal_data.get('entry_price', 0.5)

        # Momentum strength score (0.0 to 1.0)
        momentum_score = min(momentum_value / 0.08, 1.0)

        # Price centrality score (0.0 to 1.0) — prices near 0.5 = better edge
        price_centrality = 1.0 - abs(entry_price - 0.5) * 2
        price_score = max(price_centrality, 0.0)

        # Combined: 70% momentum strength, 30% price centrality
        confidence = (momentum_score * 0.7) + (price_score * 0.3)

    elif signal_type == 'farming':
        entry_price = signal_data.get('entry_price', 0.65)
        seconds_elapsed = signal_data.get('seconds_elapsed', 60)

        # Price extremity score (0.0 to 1.0) — more extreme = stronger signal
        price_extremity = (abs(entry_price - 0.5) - 0.15) / 0.30
        price_score = max(min(price_extremity, 1.0), 0.0)

        # Time score (0.0 to 1.0) — earlier entry = better
        time_score = max(1.0 - (seconds_elapsed - 60) / 120, 0.0)

        # Combined: 60% price extremity, 40% time
        confidence = (price_score * 0.6) + (time_score * 0.4)

    elif signal_type == 'streak':
        streak_length = signal_data.get('streak_length', 3)
        streak_score = min((streak_length - 3) / 2, 1.0)
        confidence = 0.5 + (streak_score * 0.5)

    elif signal_type == 'calibration':
        deviation = abs(signal_data.get('deviation', 0.05))
        deviation_score = min(deviation / 0.15, 1.0)
        confidence = deviation_score

    elif signal_type == 'late_dip_recovery':
        drop = signal_data.get('drop', 0.20)
        drop_score = min(drop / 0.40, 1.0)
        confidence = 0.5 + (drop_score * 0.5)

    else:
        confidence = 0.5

    # Map confidence (0.0-1.0) to multiplier range
    min_mult = config.BET_SIZE_MIN_MULTIPLIER
    max_mult = config.BET_SIZE_MAX_MULTIPLIER
    multiplier = min_mult + (confidence * (max_mult - min_mult))

    # Round to nearest 0.5x to avoid tiny differences
    multiplier = round(multiplier * 2) / 2

    return multiplier


async def evaluate_strategies(market: db.MarketInfo) -> Signal | None:
    """Try each enabled strategy in priority order. Return first signal found."""

    strategies = []
    if config.STRATEGY_MOMENTUM_ENABLED:
        strategies.append(("momentum", evaluate_momentum))
    if config.STRATEGY_STREAK_ENABLED:
        strategies.append(("streak", evaluate_streak))
    if config.STRATEGY_CALIBRATION_ENABLED:
        strategies.append(("calibration", evaluate_calibration))
    if config.STRATEGY_FARMING_ENABLED:
        strategies.append(("farming", evaluate_farming))
    if config.STRATEGY_LATE_DIP_RECOVERY_ENABLED:
        strategies.append(("late_dip_recovery", evaluate_late_dip_recovery))

    for name, evaluate_fn in strategies:
        if not await db.already_traded_this_market(market.market_id, name):
            signal = await evaluate_fn(market)
            if signal:
                signal.confidence_multiplier = calculate_confidence(
                    signal.strategy_name, signal.signal_data or {}
                )
                log.info("Signal: %s %s on %s (price=%.4f, confidence=%.1fx)",
                         signal.strategy_name, signal.direction,
                         market.market_id[:16], signal.entry_price,
                         signal.confidence_multiplier)
                return signal

    return None

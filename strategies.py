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


async def evaluate_farming(market: db.MarketInfo, live_config: dict) -> Signal | None:
    """Farming: bet on strong directional moves after a brief warm-up."""
    if market.market_type in ('btc_5m', 'btc_15m'):
        return None
    if '5m' in market.market_type:
        return None
    now = datetime.now(timezone.utc)
    seconds_elapsed = (now - market.started_at).total_seconds()

    max_seconds = float(live_config.get('farming_max_entry_minutes', '3')) * 60

    # Only enter in the first N minutes, wait at least 1 minute
    if seconds_elapsed < 60:
        return None
    if seconds_elapsed > max_seconds:
        return None

    current_price = await db.get_latest_price(market.market_id)
    if current_price is None:
        return None

    trigger_point = float(live_config.get('farming_trigger_point', str(config.FARMING_TRIGGER_POINT)))
    use_stop_loss = live_config.get('farming_use_stop_loss', 'true') == 'true'
    exit_point = float(live_config.get('farming_exit_point', str(config.FARMING_EXIT_POINT)))

    if current_price >= trigger_point:
        if use_stop_loss and current_price < exit_point:
            return None
        return Signal("Up", "farming", current_price, {
            "current_price": current_price,
            "trigger_point": trigger_point,
            "seconds_elapsed": round(seconds_elapsed, 1),
        })
    if current_price <= (1 - trigger_point):
        if use_stop_loss and (1 - current_price) > (1 - exit_point):
            return None
        return Signal("Down", "farming", 1 - current_price, {
            "current_price": current_price,
            "trigger_point": trigger_point,
            "seconds_elapsed": round(seconds_elapsed, 1),
        })

    return None


async def evaluate_momentum_tier(market: db.MarketInfo, live_config: dict, tier: str) -> Signal | None:
    """Momentum tier: evaluate a single momentum tier (broad/filtered/aggressive)."""
    if '15m' in market.market_type:
        return None

    prefix = f'momentum_{tier}_'

    # --- Read all tier parameters from live_config ---
    price_a_seconds = int(live_config.get(f'{prefix}price_a_seconds', '45'))
    price_b_seconds = int(live_config.get(f'{prefix}price_b_seconds', '90'))
    entry_after_seconds = int(live_config.get(f'{prefix}entry_after_seconds', '95'))
    entry_until_seconds = int(live_config.get(f'{prefix}entry_until_seconds', '120'))
    threshold = float(live_config.get(f'{prefix}threshold', '0.03'))
    price_min = float(live_config.get(f'{prefix}price_min', '0.40'))
    price_max = float(live_config.get(f'{prefix}price_max', '0.75'))
    direction_filter = live_config.get(f'{prefix}direction', 'both')
    markets_filter = live_config.get(f'{prefix}markets', 'all')
    hours_start = int(live_config.get(f'{prefix}hours_start', '8'))
    hours_end = int(live_config.get(f'{prefix}hours_end', '24'))
    bet_size = float(live_config.get(f'{prefix}bet_size', '1.0'))
    stop_loss_price = float(live_config.get(f'{prefix}stop_loss_price', '0.35'))
    stop_loss_enabled = live_config.get(f'{prefix}stop_loss_enabled', 'false') == 'true'

    # 1. Market type filter
    if markets_filter == 'no_btc' and 'btc' in market.market_type:
        return None
    if markets_filter == 'xrp_sol_only':
        if 'xrp' not in market.market_type and 'sol' not in market.market_type:
            return None

    # 2. Hour filter
    current_hour = datetime.now(timezone.utc).hour
    if current_hour < hours_start or current_hour >= hours_end:
        return None

    # 3. Timing window
    now = datetime.now(timezone.utc)
    seconds_elapsed = (now - market.started_at).total_seconds()
    if seconds_elapsed < entry_after_seconds or seconds_elapsed > entry_until_seconds:
        return None

    # 4. Fetch price samples
    price_a = await db.get_price_at_second(market.market_id, market.started_at, price_a_seconds)
    price_b = await db.get_price_at_second(market.market_id, market.started_at, price_b_seconds)
    if price_a is None or price_b is None:
        return None

    # 5. Calculate momentum
    momentum = price_b - price_a

    # 6. Direction filter and signal creation
    if momentum >= threshold and direction_filter in ('both', 'up_only'):
        entry_price = price_b
        direction = 'Up'
    elif momentum <= -threshold and direction_filter in ('both', 'down_only'):
        entry_price = 1 - price_b
        direction = 'Down'
    else:
        return None

    # 7. Entry price range filter
    if entry_price < price_min or entry_price > price_max:
        return None

    # 8. Stop-loss logic — only attach if enabled AND bet_size >= $5 platform minimum
    stop_loss_active = stop_loss_enabled and bet_size >= 5.0

    # 9. Return signal
    return Signal(
        direction=direction,
        strategy_name=f'momentum_{tier}',
        entry_price=entry_price,
        signal_data={
            'tier': tier,
            'price_a_seconds': price_a_seconds,
            'price_b_seconds': price_b_seconds,
            'price_a': price_a,
            'price_b': price_b,
            'momentum_value': round(momentum, 6),
            'entry_price': entry_price,
            'seconds_elapsed': round(seconds_elapsed, 1),
            'stop_loss_price': stop_loss_price if stop_loss_active else None,
            'bet_size': bet_size,
        }
    )


async def evaluate_streak(market: db.MarketInfo, live_config: dict) -> Signal | None:
    """Streak: fade consecutive same-direction outcomes (mean reversion)."""
    streak_length = int(live_config.get('streak_length', str(config.STREAK_LENGTH)))
    streak_direction = live_config.get('streak_direction', config.STREAK_DIRECTION)

    recent = await db.get_recent_outcomes(market.market_type, streak_length)
    if len(recent) < streak_length:
        return None

    current_price = await db.get_latest_price(market.market_id)
    if current_price is None:
        return None

    sig_data = {
        "streak_length": streak_length,
        "streak_direction": streak_direction,
        "recent_outcomes": recent,
    }

    # Check if all recent outcomes are the same — then fade the streak
    if all(o == "Up" for o in recent) and streak_direction in ("Up", "both"):
        return Signal("Down", "streak", 1 - current_price, sig_data)

    if all(o == "Down" for o in recent) and streak_direction in ("Down", "both"):
        return Signal("Up", "streak", current_price, sig_data)

    return None


async def evaluate_calibration(market: db.MarketInfo, live_config: dict) -> Signal | None:
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


async def evaluate_late_dip_recovery(market: db.MarketInfo, live_config: dict) -> Signal | None:
    """Late dip recovery: buy Up when a strong uptrend dips late in a 15m window."""
    # Only 15m markets
    if '5m' in market.market_type:
        return None

    now = datetime.now(timezone.utc)
    seconds_elapsed = (now - market.started_at).total_seconds()

    # Only activate between minute 10 and minute 14
    if seconds_elapsed < 600 or seconds_elapsed > 840:


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

    # Stop-loss: dip too deep — market may actually be resolving Down
    use_stop_loss = live_config.get('late_dip_use_stop_loss', 'true') == 'true'
    exit_point = float(live_config.get('late_dip_exit_point', '0.35'))
    if use_stop_loss and current_price < exit_point:
        return None

    log.info("[CONFIDENCE] late_dip_recovery on %s — avg_5_10: %.2f, current: %.2f, drop: %.2f",
             market.market_type, avg_price_5_to_10, current_price, drop)

    return Signal('Up', 'late_dip_recovery', current_price, signal_data={
        'avg_price_5_to_10': avg_price_5_to_10,
        'current_price': current_price,
        'drop': round(drop, 4),
        'seconds_elapsed': seconds_elapsed,
    })


def calculate_confidence(signal_type: str, signal_data: dict, live_config: dict) -> float:
    """
    Returns a multiplier between BET_SIZE_MIN_MULTIPLIER and BET_SIZE_MAX_MULTIPLIER.
    Higher = more confident = bigger bet.
    """
    if signal_type.startswith('momentum_'):
        # Momentum tiers use fixed bet_size per tier, no confidence multiplier
        return 1.0

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


async def evaluate_strategies(market: db.MarketInfo, live_config: dict) -> list[Signal]:
    """Evaluate all enabled strategies. Momentum tiers fire independently; others use first-match."""

    # --- Momentum tiers: all enabled tiers evaluated independently ---
    momentum_signals = []
    for tier in ('broad', 'filtered', 'aggressive'):
        if live_config.get(f'strategy_momentum_{tier}_enabled', 'false') == 'true':
            if not await db.already_traded_this_market(market.market_id, f'momentum_{tier}'):
                signal = await evaluate_momentum_tier(market, live_config, tier)
                if signal:
                    signal.confidence_multiplier = 1.0
                    log.info("Signal: %s %s on %s (price=%.4f, tier=%s, bet=$%.2f)",
                             signal.strategy_name, signal.direction,
                             market.market_id[:16], signal.entry_price,
                             tier, signal.signal_data.get('bet_size', 0))
                    momentum_signals.append(signal)

    # --- Other strategies: first-match behavior ---
    other_strategies = []
    if live_config.get('strategy_streak_enabled', 'false') == 'true':
        other_strategies.append(("streak", evaluate_streak))
    if live_config.get('strategy_calibration_enabled', 'false') == 'true':
        other_strategies.append(("calibration", evaluate_calibration))
    if live_config.get('strategy_farming_enabled', 'false') == 'true':
        other_strategies.append(("farming", evaluate_farming))
    if live_config.get('strategy_late_dip_recovery_enabled', 'false') == 'true':
        other_strategies.append(("late_dip_recovery", evaluate_late_dip_recovery))

    other_signal = None
    for name, evaluate_fn in other_strategies:
        if not await db.already_traded_this_market(market.market_id, name):
            signal = await evaluate_fn(market, live_config)
            if signal:
                signal.confidence_multiplier = calculate_confidence(
                    signal.strategy_name, signal.signal_data or {}, live_config
                )
                log.info("Signal: %s %s on %s (price=%.4f, confidence=%.1fx)",
                         signal.strategy_name, signal.direction,
                         market.market_id[:16], signal.entry_price,
                         signal.confidence_multiplier)
                other_signal = signal
                break

    # Combine: all momentum signals + at most one other strategy signal
    signals = momentum_signals
    if other_signal:
        signals.append(other_signal)

    return signals

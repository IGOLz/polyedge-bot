"""M3 + M4 dual strategy for Polymarket 5m crypto markets.

M3 (Spike Reversion): Detects price spikes in first 15 seconds,
buys contrarian after 10% reversion. Hold to resolution.
Win rate: 54.4%, EV: +$0.1747/trade, ~16.3 trades/day.

M4 (Volatility): Detects high volatility at second 30, bets
against dominant direction. Hold to resolution.
Win rate: 43.7%, EV: +$0.0943/trade, ~47.7 trades/day.

Combined expected: ~64 trades/day, +$58.60/day at $200 bankroll.
All parameters hardcoded from empirical testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import db
from balance import get_usdc_balance
from constants import M3_CONFIG, M4_CONFIG, BET_SIZING
from utils import log, debug_log


# ── Signal dataclass ─────────────────────────────────────────────────────

@dataclass
class Signal:
    direction: str        # 'Up' or 'Down'
    strategy_name: str
    entry_price: float    # price of the token we'd buy (locked at signal time)
    signal_data: dict[str, Any] = field(default_factory=dict)
    confidence_multiplier: float = 1.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Locked execution parameters — calculated at signal time, never recalculated
    locked_shares: int = 0
    locked_cost: float = 0.0
    locked_balance: float = 0.0
    locked_bet_size: float = 0.0


# ── Bet sizing (shared by M3 and M4) ─────────────────────────────────────

def calculate_dynamic_bet_size(current_balance: float, strategy: str | None = None) -> float:
    """
    Calculate bet size based on current bankroll.
    Both M3 and M4 get the same sizing (4% of bankroll).

    Examples:
    - $200 balance -> $8 bet
    - $300 balance -> $12 bet
    - $500 balance -> $20 bet
    - $1,000 balance -> $40 bet
    """
    bet_percentage = BET_SIZING['bet_percentage']
    min_bet = BET_SIZING['min_bet']
    max_bet = BET_SIZING['max_bet']

    calculated_bet = current_balance * bet_percentage
    bet_size = max(min_bet, min(calculated_bet, max_bet))

    return round(bet_size, 2)


def calculate_shares(entry_price: float, bet_size: float) -> int:
    """
    Calculate number of shares to buy.
    shares = floor(bet_size / entry_price), minimum 1.
    """
    if entry_price <= 0:
        return 1
    shares = int(bet_size / entry_price)
    return max(shares, 1)


# ── Volatility calculation (M4) ──────────────────────────────────────────

def calculate_price_volatility(price_history: list[float], window_seconds: int = 10) -> float:
    """
    Calculate volatility (standard deviation) of prices over a window.

    Takes the last `window_seconds` prices from price_history.
    Returns population std dev rounded to 6 decimals.
    """
    if len(price_history) < 2 or window_seconds < 1:
        return 0.0

    window_prices = price_history[-window_seconds:] if len(price_history) >= window_seconds else price_history

    if len(window_prices) < 2:
        return 0.0

    mean_price = sum(window_prices) / len(window_prices)
    variance = sum((p - mean_price) ** 2 for p in window_prices) / len(window_prices)
    volatility = variance ** 0.5

    return round(volatility, 6)


# ── Combined Trade Tracker ────────────────────────────────────────────────

class CombinedStrategyTracker:
    """Track both M3 and M4 trades with separate tallies."""

    def __init__(self):
        self.start_balance = BET_SIZING['starting_bankroll']
        self.current_balance = BET_SIZING['starting_bankroll']
        self.all_trades: list[dict] = []
        self.m3_trades: list[dict] = []
        self.m4_trades: list[dict] = []

    def add_trade(self, trade_record: dict):
        """
        trade_record keys: strategy, timestamp, market_id, direction,
        entry_price, shares, cost, result ('win'|'loss'|'pending'), pnl
        """
        self.all_trades.append(trade_record)
        strategy = trade_record.get('strategy', '')
        if 'M3' in strategy:
            self.m3_trades.append(trade_record)
        elif 'M4' in strategy:
            self.m4_trades.append(trade_record)
        if trade_record.get('result') in ('win', 'loss'):
            self.current_balance += trade_record.get('pnl', 0)

    def get_stats(self, strategy: str = 'combined') -> dict:
        """Get performance metrics. strategy: 'M3', 'M4', or 'combined'."""
        if strategy == 'M3':
            trades = self.m3_trades
        elif strategy == 'M4':
            trades = self.m4_trades
        else:
            trades = self.all_trades

        completed = [t for t in trades if t.get('result') in ('win', 'loss')]
        wins = len([t for t in completed if t['result'] == 'win'])
        losses = len([t for t in completed if t['result'] == 'loss'])
        total_pnl = sum(t.get('pnl', 0) for t in completed)

        return {
            'strategy': strategy,
            'total_trades': len(trades),
            'completed_trades': len(completed),
            'wins': wins,
            'losses': losses,
            'win_rate': wins / len(completed) * 100 if completed else 0,
            'total_pnl': total_pnl,
            'avg_pnl': total_pnl / len(completed) if completed else 0,
            'balance': self.current_balance,
        }

    def get_daily_stats(self) -> dict:
        """Backwards-compatible: return combined daily stats."""
        return self.get_stats('combined')


# Module-level tracker instance
strategy_tracker = CombinedStrategyTracker()


# ── Logging helpers ───────────────────────────────────────────────────────

def log_trade_execution(signal: Signal, execution_result: dict):
    """Log complete trade execution with profitability thesis."""
    tag = 'M3' if 'M3' in signal.strategy_name else 'M4'
    log.info(
        "[%s] TRADE EXECUTED | Direction=%s | EntryPrice=$%.4f | "
        "Shares=%s | Cost=$%.2f | Balance=$%.2f | BetSize=$%.2f | Status=%s",
        tag,
        signal.direction,
        signal.entry_price,
        signal.signal_data.get('shares'),
        signal.signal_data.get('actual_cost', 0),
        signal.signal_data.get('current_balance', 0),
        signal.signal_data.get('bet_size', 0),
        execution_result.get('status', '?'),
    )

    thesis = signal.signal_data.get('profitability_thesis', '')
    bet_size = signal.signal_data.get('bet_size', 0)
    # M3 EV per dollar: 0.1747/$10 = 0.01747; M4: 0.0943/$10 = 0.00943
    ev_per_dollar = 0.01747 if 'M3' in signal.strategy_name else 0.00943
    expected_ev = bet_size * ev_per_dollar

    log.info("[%s] THESIS: %s | Expected_Value=+$%.2f", tag, thesis, expected_ev)


def log_performance_metrics():
    """Log running performance vs backtest expectations for both strategies."""
    m3_stats = strategy_tracker.get_stats('M3')
    m4_stats = strategy_tracker.get_stats('M4')
    combined = strategy_tracker.get_stats('combined')

    if m3_stats['total_trades'] > 0:
        log.info(
            "[M3] Daily Summary | Trades: %d | Win%%: %.1f%% (target: 54.4%%) | "
            "PnL: $%.2f | AvgPnL: $%.2f",
            m3_stats['total_trades'], m3_stats['win_rate'],
            m3_stats['total_pnl'], m3_stats['avg_pnl'],
        )

    if m4_stats['total_trades'] > 0:
        log.info(
            "[M4] Daily Summary | Trades: %d | Win%%: %.1f%% (target: 43.7%%) | "
            "PnL: $%.2f | AvgPnL: $%.2f",
            m4_stats['total_trades'], m4_stats['win_rate'],
            m4_stats['total_pnl'], m4_stats['avg_pnl'],
        )

    bal = strategy_tracker.current_balance
    bet_size = bal * BET_SIZING['bet_percentage']
    # Expected daily: 16.3 M3 trades + 47.7 M4 trades
    m3_daily_ev = 16.3 * bet_size * 0.01747
    m4_daily_ev = 47.7 * bet_size * 0.00943

    log.info(
        "[COMBINED] Daily Summary | Total Trades: %d | Win%%: %.1f%% | "
        "PnL: $%.2f (expected: $%.2f) | Balance: $%.2f (started: $%.2f)",
        combined['total_trades'], combined['win_rate'],
        combined['total_pnl'], m3_daily_ev + m4_daily_ev,
        combined['balance'], strategy_tracker.start_balance,
    )


# ── M3 Signal evaluation (Spike Reversion) ───────────────────────────────

async def evaluate_m3_signal(market: db.MarketInfo, ticks: list[db.Tick]) -> Signal | None:
    """
    Evaluate M3 spike reversion strategy.

    Detection logic:
    1. Look for spike in first 15 seconds (UP >= 0.80 OR UP <= 0.20)
    2. After spike detected, wait for 10% reversion (>= 10 ticks later)
    3. When reversion happens, buy the losing token at >= 0.35
    4. Hold to market resolution

    Returns Signal if spike+reversion pattern found, None otherwise.
    """

    # GUARD 1: Strategy enabled?
    if not M3_CONFIG['enabled']:
        debug_log.info("[M3-DEBUG] Strategy DISABLED, skipping all evaluation")
        return None

    # GUARD 2: 5-min market only
    if M3_CONFIG['only_5min_markets']:
        if '5m' not in market.market_type and '5min' not in market.market_type:
            debug_log.info("[M3-DEBUG] %s - Rejected: not 5-min market (type=%s)", market.market_id[:16], market.market_type)
            return None

    # GUARD 3: Asset filter
    market_type_lower = market.market_type.lower()
    asset_match = any(asset in market_type_lower for asset in M3_CONFIG['allowed_assets'])
    if not asset_match:
        debug_log.info("[M3-DEBUG] %s - Rejected: asset not allowed (type=%s)", market.market_id[:16], market.market_type)
        return None

    # GUARD 4: Already traded this market?
    if await db.already_traded_this_market(market.market_id, 'M3_spike_reversion'):
        debug_log.info("[M3-DEBUG] %s - Rejected: already traded this market", market.market_id[:16])
        return None

    # GUARD 5: Still in detection window? (first 15 seconds)
    seconds_elapsed = (datetime.now(timezone.utc) - market.started_at).total_seconds()
    detection_window = M3_CONFIG['spike_detection_window_seconds']

    debug_log.info("[M3-DEBUG] %s - Evaluating market type=%s | elapsed=%.1fs | window=%ds | ticks=%d",
             market.market_id[:16], market.market_type, seconds_elapsed, detection_window, len(ticks))

    if seconds_elapsed > detection_window:
        debug_log.info("[M3-DEBUG] %s - Rejected: outside detection window (%.1fs > %ds)",
                 market.market_id[:16], seconds_elapsed, detection_window)
        return None

    # GUARD 6: Enough ticks?
    if len(ticks) < 2:
        debug_log.info("[M3-DEBUG] %s - Rejected: not enough ticks (%d < 2)", market.market_id[:16], len(ticks))
        return None

    # ── STEP 1: DETECT SPIKE ──────────────────────────────────────────

    spike_threshold_up = M3_CONFIG['spike_threshold_up']      # 0.80
    spike_threshold_down = M3_CONFIG['spike_threshold_down']   # 0.20

    # UP-dominant spike: up_price went very high
    max_up_price = max(t.up_price for t in ticks)
    spike_up = max_up_price >= spike_threshold_up

    # DOWN-dominant spike: up_price went very low (down_price very high)
    min_up_price = min(t.up_price for t in ticks)
    spike_down = min_up_price <= spike_threshold_down

    debug_log.info("[M3-DEBUG] %s - Spike check: max_up=%.4f (spike=%s, threshold=%.2f) | min_up=%.4f (spike=%s, threshold=%.2f)",
             market.market_id[:16], max_up_price, spike_up, spike_threshold_up,
             min_up_price, spike_down, spike_threshold_down)

    if not (spike_up or spike_down):
        debug_log.info("[M3-DEBUG] %s - Rejected: NO SPIKE detected", market.market_id[:16])
        return None

    debug_log.info("[M3-DEBUG] %s - SPIKE DETECTED: %s (max_up=%.4f, min_up=%.4f)",
             market.market_id[:16],
             "UP" if spike_up else "DOWN",
             max_up_price, min_up_price)

    # ── STEP 2: DETECT REVERSION ──────────────────────────────────────

    reversion_pct = M3_CONFIG['reversion_reversal_pct']
    min_reversion_ticks = M3_CONFIG['min_reversion_ticks']

    reversion_found = False
    reversion_tick_index = None
    spike_direction = None
    spike_price = None
    spike_tick_index = None
    reversion_target = None
    direction = None
    entry_price = None

    if spike_up:
        # UP was extreme — find spike tick, then look for reversion
        spike_direction = 'Up'
        spike_price = max_up_price
        spike_tick_index = next(i for i, t in enumerate(ticks) if t.up_price == max_up_price)

        # Reversion: UP must fall below spike * (1 - 10%)
        reversion_target = spike_price * (1 - reversion_pct)

        for i in range(spike_tick_index, len(ticks)):
            if ticks[i].up_price <= reversion_target:
                if (i - spike_tick_index) >= min_reversion_ticks:
                    reversion_found = True
                    reversion_tick_index = i
                    break

        if reversion_found:
            # UP reverted — bet DOWN (contrarian)
            direction = 'Down'
            entry_price = ticks[reversion_tick_index].down_price

    if not reversion_found and spike_down:
        # DOWN was extreme (UP went very low) — find spike tick
        spike_direction = 'Down'
        spike_price = 1 - min_up_price  # the extreme DOWN price
        spike_tick_index = next(i for i, t in enumerate(ticks) if t.up_price == min_up_price)

        # Reversion: DOWN must fall below its spike * (1 - 10%)
        # i.e., down_price must drop below spike_price * 0.90
        reversion_target = spike_price * (1 - reversion_pct)

        for i in range(spike_tick_index, len(ticks)):
            if ticks[i].down_price <= reversion_target:
                if (i - spike_tick_index) >= min_reversion_ticks:
                    reversion_found = True
                    reversion_tick_index = i
                    break

        if reversion_found:
            # DOWN reverted — bet UP (contrarian)
            direction = 'Up'
            entry_price = ticks[reversion_tick_index].up_price

    if not reversion_found:
        debug_log.info("[M3-DEBUG] %s - Rejected: spike %s to %.4f but NO REVERSION (target: %.4f, need %d+ ticks gap)",
                 market.market_id[:16], spike_direction or '?',
                 spike_price or 0, reversion_target or 0, min_reversion_ticks)
        return None

    debug_log.info("[M3-DEBUG] %s - REVERSION CONFIRMED: %s reverted to %.4f at tick %d (spike at tick %d, %d ticks gap)",
             market.market_id[:16], spike_direction, reversion_target,
             reversion_tick_index, spike_tick_index, reversion_tick_index - spike_tick_index)

    # ── STEP 3: ENTRY PRICE VALIDATION ────────────────────────────────

    entry_threshold = M3_CONFIG['entry_price_threshold']
    debug_log.info("[M3-DEBUG] %s - Entry validation: price=%.4f, threshold=%.2f, direction=%s",
             market.market_id[:16], entry_price, entry_threshold, direction)
    if entry_price < entry_threshold:
        debug_log.info("[M3-DEBUG] %s - Rejected: entry price too low (%.4f < %.2f)",
                 market.market_id[:16], entry_price, entry_threshold)
        return None

    # GUARD: Minimum seconds remaining
    seconds_remaining = 300 - seconds_elapsed
    if seconds_remaining < M3_CONFIG['min_seconds_remaining']:
        if M3_CONFIG['log_rejection_reasons']:
            log.debug("M3: %s — too late (%.0fs remaining)", market.market_id[:16], seconds_remaining)
        return None

    # ── STEP 4: BET SIZING ────────────────────────────────────────────

    current_balance = await get_usdc_balance()
    if current_balance <= 0:
        log.warning("M3: Could not fetch balance for %s, skipping", market.market_id[:16])
        return None

    bet_size = calculate_dynamic_bet_size(current_balance, strategy='M3')
    shares = calculate_shares(entry_price, bet_size)
    actual_cost = shares * entry_price

    # Don't risk more than max_single_trade_pct in one trade
    if actual_cost > current_balance * BET_SIZING['max_single_trade_pct']:
        log.warning("M3: %s — bet too large ($%.2f > %.0f%% of $%.2f)",
                    market.market_id[:16], actual_cost,
                    BET_SIZING['max_single_trade_pct'] * 100, current_balance)
        return None

    # ── STEP 5: CREATE SIGNAL ─────────────────────────────────────────

    reversion_seconds = reversion_tick_index - spike_tick_index
    opposite = 'Down' if direction == 'Up' else 'Up'
    thesis = (
        f"Spike detected: {spike_direction} reached {spike_price:.4f} at tick {spike_tick_index}. "
        f"Reversion confirmed: {opposite} reverted to {reversion_target:.4f} by tick {reversion_tick_index} "
        f"({reversion_seconds} ticks, 10% mean reversion). Betting contrarian {direction}."
    )

    signal = Signal(
        direction=direction,
        strategy_name='M3_spike_reversion',
        entry_price=entry_price,
        locked_shares=shares,
        locked_cost=round(actual_cost, 4),
        locked_balance=round(current_balance, 2),
        locked_bet_size=round(bet_size, 2),
        signal_data={
            'spike_direction': spike_direction,
            'spike_price': round(spike_price, 4),
            'spike_tick': spike_tick_index,
            'reversion_target': round(reversion_target, 4),
            'reversion_tick': reversion_tick_index,
            'reversion_ticks_elapsed': reversion_seconds,
            'entry_price': round(entry_price, 4),
            'seconds_elapsed': round(seconds_elapsed, 1),
            'seconds_remaining': round(seconds_remaining, 1),
            'current_balance': round(current_balance, 2),
            'bet_size': round(bet_size, 2),
            'bet_cost': round(actual_cost, 4),
            'shares': shares,
            'actual_cost': round(actual_cost, 2),
            'stop_loss_price': None,  # M3 holds to resolution
            'price_min': 0.01,        # M3 handles its own price validation
            'price_max': 0.99,
            'profitability_thesis': thesis,
            'balance_at_signal': round(current_balance, 2),
        },
    )

    # LOG the signal
    log.info(
        "[M3] ✅ SIGNAL GENERATED | %s | Shares: %d, Cost: $%.2f | Balance: $%.2f",
        direction, shares, actual_cost, current_balance,
    )
    log.info(
        "[M3-DEBUG] %s - Full signal: Spike=%s to %.4f at tick %d | "
        "Reversion=%.4f at tick %d (%d ticks) | Entry=$%.4f | BetSize=$%.2f",
        market.market_id[:16],
        spike_direction, spike_price, spike_tick_index,
        reversion_target, reversion_tick_index, reversion_seconds,
        entry_price, bet_size,
    )
    log.info("M3 THESIS: %s", thesis)

    return signal


# ── M4 Signal evaluation (Volatility) ────────────────────────────────────

async def evaluate_m4_signal(market: db.MarketInfo, ticks: list[db.Tick]) -> Signal | None:
    """
    Evaluate M4 volatility strategy for a market.

    Logic: at second 30, check if spread is in [0.05, 0.50] and
    volatility (std dev over 10s) exceeds 0.05. If so, bet contrarian
    to the dominant price direction.

    Returns Signal if all conditions pass, None otherwise.
    """

    # GUARD 1: Strategy enabled?
    if not M4_CONFIG['enabled']:
        return None

    # GUARD 2: 5-min market only
    if M4_CONFIG['only_5min_markets']:
        if '5m' not in market.market_type and '5min' not in market.market_type:
            if M4_CONFIG['log_rejection_reasons']:
                log.debug("M4: Skipping %s — not 5-min (%s)", market.market_id[:16], market.market_type)
            return None

    # GUARD 3: Asset filter
    market_type_lower = market.market_type.lower()
    asset_match = any(asset in market_type_lower for asset in M4_CONFIG['allowed_assets'])
    if not asset_match:
        if M4_CONFIG['log_rejection_reasons']:
            log.debug("M4: Skipping %s — asset not allowed (%s)", market.market_id[:16], market.market_type)
        return None

    # GUARD 4: Already traded this market?
    if await db.already_traded_this_market(market.market_id, 'M4_volatility'):
        log.debug("M4: Already traded %s", market.market_id[:16])
        return None

    # GUARD 5: Evaluation timing (eval_second +-2s window)
    seconds_elapsed = (datetime.now(timezone.utc) - market.started_at).total_seconds()
    eval_second = M4_CONFIG['eval_second']

    eval_window = M4_CONFIG['eval_window']
    if not (eval_second - eval_window <= seconds_elapsed <= eval_second + eval_window):
        return None  # fires constantly, don't log

    # GUARD 6: Enough tick data for volatility window
    vol_window = M4_CONFIG['volatility_window_seconds']
    if len(ticks) < vol_window:
        log.warning("M4: Not enough ticks for %s (%d < %d)", market.market_id[:16], len(ticks), vol_window)
        return None

    # EXTRACT: Current prices from most recent tick
    current_tick = ticks[-1]
    up_price = current_tick.up_price
    down_price = current_tick.down_price

    # CALCULATE: Spread (absolute distance between up/down prices)
    # Since down_price is derived as 1 - up_price, spread = |2*up_price - 1|
    spread = abs(up_price - down_price)

    # CHECK: Spread in valid range
    if spread < M4_CONFIG['min_spread']:
        if M4_CONFIG['log_rejection_reasons']:
            log.debug("M4: %s — spread too tight (%.4f < %.2f)", market.market_id[:16], spread, M4_CONFIG['min_spread'])
        return None

    if spread > M4_CONFIG['max_spread']:
        if M4_CONFIG['log_rejection_reasons']:
            log.debug("M4: %s — spread too wide (%.4f > %.2f)", market.market_id[:16], spread, M4_CONFIG['max_spread'])
        return None

    # CALCULATE: Volatility over last N ticks
    up_price_history = [t.up_price for t in ticks[-vol_window:]]
    down_price_history = [t.down_price for t in ticks[-vol_window:]]

    volatility_up = calculate_price_volatility(up_price_history, vol_window)
    volatility_down = calculate_price_volatility(down_price_history, vol_window)
    volatility = (volatility_up + volatility_down) / 2.0

    # CHECK: Volatility exceeds threshold
    vol_threshold = M4_CONFIG['volatility_threshold']
    if volatility < vol_threshold:
        if M4_CONFIG['log_rejection_reasons']:
            log.debug("M4: %s — low volatility (%.6f < %.2f)", market.market_id[:16], volatility, vol_threshold)
        return None

    # DETERMINE: Bet direction (momentum — follow the dominant direction)
    # If UP price > 0.50, market momentum favors UP -> bet UP
    # If UP price < 0.50, market momentum favors DOWN -> bet DOWN
    if up_price > 0.50:
        direction = 'Up'
        entry_price = up_price
    else:
        direction = 'Down'
        entry_price = down_price

    debug_log.info(
        "[M4-DEBUG] Direction decided: up=%.4f, down=%.4f → %s (momentum) | entry=$%.4f",
        up_price, down_price, direction, entry_price,
    )

    # GUARD: Minimum seconds remaining
    market_total_seconds = 300  # 5-min market
    seconds_remaining = market_total_seconds - seconds_elapsed
    if seconds_remaining < M4_CONFIG['min_seconds_remaining']:
        if M4_CONFIG['log_rejection_reasons']:
            log.debug("M4: %s — too late (%.0fs remaining)", market.market_id[:16], seconds_remaining)
        return None

    # CALCULATE: Bet size based on current balance
    current_balance = await get_usdc_balance()
    if current_balance <= 0:
        log.warning("M4: Could not fetch balance for %s, skipping", market.market_id[:16])
        return None

    bet_size = calculate_dynamic_bet_size(current_balance, strategy='M4')
    shares = calculate_shares(entry_price, bet_size)
    actual_cost = shares * entry_price

    # GUARD: Don't risk more than max_single_trade_pct of balance in one trade
    if actual_cost > current_balance * BET_SIZING['max_single_trade_pct']:
        log.warning("M4: %s — bet too large ($%.2f > %.0f%% of $%.2f)",
                    market.market_id[:16], actual_cost,
                    BET_SIZING['max_single_trade_pct'] * 100, current_balance)
        return None

    # Stop-loss
    sl_active = M4_CONFIG['stop_loss_enabled'] and actual_cost >= 5.0
    sl_price = M4_CONFIG['stop_loss_price'] if sl_active else None

    # Profitability thesis
    thesis = (
        f"Volatility spike (sigma={volatility:.4f}) at second {seconds_elapsed:.0f}. "
        f"Betting {direction} momentum — following dominant direction "
        f"(up={up_price:.4f}, down={down_price:.4f}, spread={spread:.4f})."
    )

    signal = Signal(
        direction=direction,
        strategy_name='M4_volatility',
        entry_price=entry_price,
        locked_shares=shares,
        locked_cost=round(actual_cost, 4),
        locked_balance=round(current_balance, 2),
        locked_bet_size=round(bet_size, 2),
        signal_data={
            'eval_second': eval_second,
            'seconds_elapsed': round(seconds_elapsed, 1),
            'seconds_remaining': round(seconds_remaining, 1),
            'up_price': round(up_price, 4),
            'down_price': round(down_price, 4),
            'spread': round(spread, 4),
            'volatility_up': round(volatility_up, 6),
            'volatility_down': round(volatility_down, 6),
            'volatility_avg': round(volatility, 6),
            'entry_price': round(entry_price, 4),
            'current_balance': round(current_balance, 2),
            'bet_size': round(bet_size, 2),
            'bet_cost': round(actual_cost, 4),
            'shares': shares,
            'actual_cost': round(actual_cost, 2),
            'stop_loss_price': sl_price,
            'price_min': 0.01,   # M4 handles its own price validation
            'price_max': 0.99,
            'profitability_thesis': thesis,
            'balance_at_signal': round(current_balance, 2),
        },
    )

    # LOG the signal
    log.info(
        "M4 SIGNAL: %s on %s | Vol=%.4f (threshold=%.2f) | Spread=%.4f | "
        "Entry=$%.4f | Shares=%d | Cost=$%.2f | Balance=$%.2f",
        direction, market.market_id[:16],
        volatility, vol_threshold,
        spread,
        entry_price,
        shares,
        actual_cost,
        current_balance,
    )
    log.info("M4 THESIS: %s", thesis)

    return signal


# ── Main entry point ──────────────────────────────────────────────────────

async def evaluate_strategies(market: db.MarketInfo, ticks: list[db.Tick]) -> list[Signal]:
    """
    Main entry point for BOTH M3 and M4 strategies.

    Execution order:
    1. M3 (early market, first 15 seconds)
    2. M4 (second 30 +- 2s)

    Both can trigger on same market (separate bets).
    Returns list of signals (0, 1, or 2 signals possible per market).
    """
    signals = []

    seconds_elapsed = (datetime.now(timezone.utc) - market.started_at).total_seconds()

    # ── M3: Early detection (first 15 seconds) ───────────────────────
    if M3_CONFIG['enabled'] and seconds_elapsed <= M3_CONFIG['spike_detection_window_seconds']:
        m3_signal = await evaluate_m3_signal(market, ticks)
        if m3_signal:
            log.info("[M3] Signal generated for %s", market.market_id[:16])
            signals.append(m3_signal)

    # ── M4: Precise timing (second 30 +- 2s) ─────────────────────────
    if M4_CONFIG['enabled']:
        eval_second = M4_CONFIG['eval_second']
        eval_window = M4_CONFIG['eval_window']
        if eval_second - eval_window <= seconds_elapsed <= eval_second + eval_window:
            m4_signal = await evaluate_m4_signal(market, ticks)
            if m4_signal:
                log.info("[M4] Signal generated for %s", market.market_id[:16])
                signals.append(m4_signal)

    # ── Log multi-signal markets ──────────────────────────────────────
    if len(signals) > 1:
        log.info("Multi-signal market %s: %d signals (%s)",
                 market.market_id[:16], len(signals),
                 [s.strategy_name for s in signals])

    return signals

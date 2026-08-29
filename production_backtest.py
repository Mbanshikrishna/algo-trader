"""Point-in-time backtest that mirrors the production trading workflow.

The engine deliberately separates data acquisition from simulation.  Tests can
feed deterministic candle bundles into :func:`backtest_day`, while the command
line runner downloads and caches Angel One candles.

Historical OHLCV cannot reproduce two live fields: order-book buy pressure and
bid/ask spread.  Buy pressure is therefore neutral (0.5), and an explicit
assumed spread is checked against the production limit.  These assumptions are
printed in every report instead of being hidden.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from requests import HTTPError

from broker.angelone_client import AngelOneClient
from config.settings import load_settings
from execution.entry_validator import (
    MAX_SPREAD_PCT,
    MIN_RANGE_POSITION,
    MIN_VOLUME_RATIO,
)
from monitor.position_tracker import (
    HARD_MAX_LOSS_PCT,
    INITIAL_ATR_MULT,
    INTRADAY_LOCK_FLOOR_PCT,
    INTRADAY_LOCK_THRESHOLD,
    INTRADAY_LOCK_TRAIL_PCT,
    LATE_SESSION_TRAIL_REDUCTION,
    MIN_STOP_DISTANCE_PCT,
    TARGET_PROFIT_PCT,
    TRAIL_TIERS,
)
from monitor.risk_state import calculate_position_size
from strategy.market_scanner import (
    INDIA_VIX_TOKEN,
    MAX_PRICE,
    MIN_GAIN_PCT,
    MIN_PRICE,
    MIN_VOLUME,
    NIFTY_50_CONSTITUENTS,
    NIFTY_50_TOKEN,
    NIFTYBEES_TOKEN,
    load_nse_equity_tokens,
    score_candidate_quote,
)
from utils.atr import compute_atr_from_candles

logger = logging.getLogger("production_backtest")
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class BacktestConfig:
    capital: float = 100_000.0
    leverage: float = 5.0
    risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 2.0
    max_consecutive_losses: int = 2
    max_reentry_rounds: int = 2
    slippage_bps: float = 5.0
    fees_bps_per_side: float = 10.0
    assumed_spread_bps: float = 10.0
    sizing_mode: str = "risk"
    scan_start: str = "10:00"
    scan_end: str = "14:00"
    force_exit: str = "15:05"
    bar_minutes: int = 5
    intraday_interval: str = "FIVE_MINUTE"
    scan_pool_size: int = 5
    fno_only: bool = False
    # A production scan, validation and broker submission cannot fill at the
    # already-observed signal price.  On one-minute data the next bar open is
    # the first price whose full OHLC path is safe to simulate.
    entry_delay_minutes: int = 1
    # Entry-quality experiments. These are configurable so the stricter
    # candidate can be compared with the recorded production baseline before
    # any live deployment.
    entry_range_position_min: float = MIN_RANGE_POSITION
    completed_minute_volume_required: bool = False
    breakout_confirmation: str = "production"
    breakout_persistence_bars: int = 2
    breakout_retest_tolerance_pct: float = 0.002
    # Observation-derived stop experiment. Disabled by default so historical
    # baseline reports remain reproducible. Tuples are
    # (confirmed MFE from entry, stop floor relative to entry).
    staged_stops_enabled: bool = False
    staged_stop_floors: tuple[tuple[float, float], ...] = (
        (0.01, -0.0125),
        (0.02, -0.0035),
        (0.03, 0.01),
    )
    break_even_plus_cost_enabled: bool = False
    break_even_plus_cost_trigger: float = 0.02


@dataclass(frozen=True)
class MarketGate:
    score: int
    bullish: bool
    index_pass: bool
    breadth_pass: bool
    strength_pass: bool
    volatility_pass: bool
    advancing: int = 0
    declining: int = 0


@dataclass
class HistoricalStock:
    symbol: str
    token: str
    previous_close: float
    completed_daily_candles: list[list]
    intraday_candles: list[list]
    breakout_candles: list[list] = field(default_factory=list)
    restricted_reason: str = ""
    is_fno: bool | None = None
    tradability_lists_complete: bool | None = None


@dataclass
class MarketData:
    previous_closes: dict[str, float]
    intraday_candles: dict[str, list[list]]


@dataclass
class BacktestTrade:
    date: str
    symbol: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: int
    gross_pnl: float
    fees: float
    pnl: float
    exit_reason: str
    reentry_round: int
    market_score: int
    composite_score: float
    signal_time: str = ""
    entry_atr: float = 0.0


@dataclass
class BacktestDay:
    date: str
    trades: list[BacktestTrade] = field(default_factory=list)
    market_checks: list[tuple[str, int]] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def pnl(self) -> float:
        return round(sum(trade.pnl for trade in self.trades), 2)


def _time_of(candle: list) -> str:
    stamp = str(candle[0])
    return stamp[11:16] if len(stamp) >= 16 else stamp[-5:]


def _bars_before(candles: list[list], decision_time: str) -> list[list]:
    """Return bars fully known at a decision time.

    Angel One timestamps intraday candles by their opening time.  Consequently
    the 09:55 five-minute candle is available at 10:00, while the 10:00 candle
    is not.  Strictly using ``<`` prevents same-bar look-ahead.
    """
    return [c for c in candles if _time_of(c) < decision_time]


def _bars_from(candles: list[list], decision_time: str) -> list[list]:
    return [c for c in candles if _time_of(c) >= decision_time]


def _completed_bars(
    candles: list[list], decision_time: str, duration_minutes: int
) -> list[list]:
    """Return candles whose closing instant is known at ``decision_time``."""
    decision_hour, decision_minute = map(int, decision_time.split(":"))
    decision_total = decision_hour * 60 + decision_minute
    completed = []
    for candle in candles:
        hour, minute = map(int, _time_of(candle).split(":"))
        if hour * 60 + minute + duration_minutes <= decision_total:
            completed.append(candle)
    return completed


def _add_minutes(value: str, minutes: int) -> str:
    hour, minute = map(int, value.split(":"))
    total = hour * 60 + minute + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


def scan_times(start: str, end: str, step_minutes: int = 5) -> list[str]:
    values: list[str] = []
    current = start
    while current <= end:
        values.append(current)
        current = _add_minutes(current, step_minutes)
    return values


def calculate_backtest_quantity(
    entry_price: float, stop_price: float, config: BacktestConfig
) -> int:
    """Size a replay using either current risk rules or the sizing seen in logs."""
    maximum_notional = config.capital * config.leverage * 0.95
    if config.sizing_mode == "logged_production_notional":
        return int(maximum_notional // entry_price) if entry_price > 0 else 0
    if config.sizing_mode != "risk":
        raise ValueError(
            "BACKTEST_SIZING_MODE must be 'risk' or "
            "'logged_production_notional'"
        )
    return calculate_position_size(
        capital=config.capital,
        entry_price=entry_price,
        stop_price=stop_price,
        risk_per_trade_pct=config.risk_per_trade_pct,
        maximum_notional=maximum_notional,
    )


def entry_atr_at(
    stock: HistoricalStock,
    decision_time: str,
    fallback_price: float,
) -> float:
    """Calculate the production five-minute ATR without seeing an open bar."""
    if stock.breakout_candles:
        source = _completed_bars(stock.breakout_candles, decision_time, 5)
    else:
        # FIVE_MINUTE runs already carry the ATR source in intraday_candles.
        source = _completed_bars(stock.intraday_candles, decision_time, 5)
    atr = compute_atr_from_candles(source[-13:])
    return atr if atr > 0 else fallback_price * 0.005


def resolve_backtest_dates(now: datetime, lookback: int) -> tuple[date, date]:
    """Resolve a reproducible report period, optionally from explicit dates."""
    start_override = os.getenv("BACKTEST_START_DATE", "").strip()
    end_override = os.getenv("BACKTEST_END_DATE", "").strip()
    if bool(start_override) != bool(end_override):
        raise ValueError(
            "BACKTEST_START_DATE and BACKTEST_END_DATE must be set together"
        )
    if start_override:
        start_date = date.fromisoformat(start_override)
        end_date = date.fromisoformat(end_override)
    else:
        end_date = now.date()
        start_date = (now - timedelta(days=lookback)).date()
    if start_date > end_date:
        raise ValueError("BACKTEST_START_DATE must not be after BACKTEST_END_DATE")
    return start_date, end_date


def resolve_slippage_bps(
    configured: str,
    report_root: str | Path = "data/replay_reports",
) -> tuple[float, str]:
    """Resolve fixed slippage or the latest confirmed-fill calibration."""
    value = configured.strip().lower()
    if value != "auto":
        return float(value), "configured"

    candidates = sorted(
        Path(report_root).glob("**/slippage_calibration.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if int(payload.get("confirmed_fills", 0)) <= 0:
                continue
            return (
                float(payload["recommended_backtest_slippage_bps"]),
                str(path.resolve()),
            )
        except (OSError, ValueError, TypeError, KeyError):
            continue
    fallback = float(os.getenv("BACKTEST_FALLBACK_SLIPPAGE_BPS", "5"))
    logger.warning(
        "No confirmed-fill slippage calibration found; using %.1f bps", fallback
    )
    return fallback, "fallback:no_confirmed_fill_calibration"


def build_point_in_time_quote(
    stock: HistoricalStock, decision_time: str
) -> dict[str, Any] | None:
    bars = _bars_before(stock.intraday_candles, decision_time)
    if not bars or stock.previous_close <= 0:
        return None
    return {
        "_symbol": stock.symbol,
        "_token": stock.token,
        "_name": stock.symbol,
        "ltp": float(bars[-1][4]),
        "close": stock.previous_close,
        "open": float(bars[0][1]),
        "high": max(float(c[2]) for c in bars),
        "low": min(float(c[3]) for c in bars),
        "tradeVolume": sum(float(c[5]) for c in bars),
        # Historical OHLCV has no order-book totals.  Zero makes the shared
        # production scorer apply its documented neutral value of 0.5.
        "totBuyQuan": 0,
        "totSellQuan": 0,
        "lowerCircuit": 0,
        "upperCircuit": 0,
    }


def rank_candidates_at(
    stocks: Iterable[HistoricalStock],
    decision_time: str,
    excluded_symbols: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Run the production scanner cross-sectionally at one historical instant."""
    excluded = excluded_symbols or set()
    ranked: list[dict[str, Any]] = []
    for stock in stocks:
        if stock.symbol in excluded:
            continue
        quote = build_point_in_time_quote(stock, decision_time)
        if not quote:
            continue
        scored = score_candidate_quote(quote, stock.completed_daily_candles)
        if scored:
            ranked.append(scored)
    ranked.sort(key=lambda item: (-item["composite_score"], item["symbol"]))
    return ranked


def validate_candidate_at(
    candidate: dict[str, Any],
    stock: HistoricalStock,
    decision_time: str,
    config: BacktestConfig,
) -> tuple[bool, str]:
    """Mirror production entry validation with point-in-time 5-minute data."""
    bars = _bars_before(stock.intraday_candles, decision_time)
    if len(bars) < 3:
        return False, "insufficient completed intraday bars"

    ltp = float(candidate["ltp"])
    gain = (ltp - stock.previous_close) / stock.previous_close * 100
    if not 5.0 <= gain <= 10.0:
        return False, "gain outside production validation range"

    day_high = max(float(c[2]) for c in bars)
    day_low = min(float(c[3]) for c in bars)
    day_range = day_high - day_low
    range_position = (ltp - day_low) / day_range if day_range > 0 else 1.0
    if range_position < config.entry_range_position_min:
        return False, "price below required range position"

    breakout_source = stock.breakout_candles or stock.intraday_candles
    breakout_duration = 5 if stock.breakout_candles else config.bar_minutes
    completed_breakout = _completed_bars(
        breakout_source, decision_time, breakout_duration
    )
    if config.breakout_confirmation == "production":
        near_breakout = (
            range_position >= MIN_RANGE_POSITION and 5.0 <= gain < 7.0
        )
        if not near_breakout:
            if not completed_breakout:
                return False, "insufficient completed breakout bars"
            if ltp < float(completed_breakout[-1][2]):
                return False, "no micro breakout"
    elif config.breakout_confirmation == "persistence_or_retest":
        if config.intraday_interval != "ONE_MINUTE":
            return False, "breakout confirmation requires one-minute data"
        if not completed_breakout:
            return False, "insufficient completed breakout bars"
        breakout_bar = completed_breakout[-1]
        breakout_level = float(breakout_bar[2])
        breakout_available = _add_minutes(
            _time_of(breakout_bar), breakout_duration
        )
        confirmation_bars = [
            candle
            for candle in _completed_bars(
                stock.intraday_candles, decision_time, 1
            )
            if _time_of(candle) >= breakout_available
        ]
        if not breakout_persisted_or_retested(
            confirmation_bars,
            breakout_level,
            persistence_bars=config.breakout_persistence_bars,
            retest_tolerance_pct=config.breakout_retest_tolerance_pct,
        ):
            return False, "breakout did not persist or retest successfully"
    else:
        raise ValueError(
            "BACKTEST_BREAKOUT_CONFIRMATION must be 'production' or "
            "'persistence_or_retest'"
        )

    if (
        config.completed_minute_volume_required
        and config.intraday_interval != "ONE_MINUTE"
    ):
        return False, "completed-minute volume requires one-minute data"
    volume_bars = (
        _completed_bars(stock.intraday_candles, decision_time, 1)
        if config.intraday_interval == "ONE_MINUTE"
        else bars
    )
    if len(volume_bars) < 3:
        return False, "insufficient completed volume bars"
    current_volume = float(volume_bars[-1][5])
    prior_count = 9 if config.intraday_interval == "ONE_MINUTE" else 5
    prior = [float(c[5]) for c in volume_bars[-(prior_count + 1) : -1]]
    average = sum(prior) / len(prior) if prior else 0.0
    if average <= 0 or current_volume / average < MIN_VOLUME_RATIO:
        return False, "volume confirmation failed"

    if config.assumed_spread_bps / 10_000 > MAX_SPREAD_PCT:
        return False, "assumed spread exceeds production limit"
    return True, "all reproducible checks passed"


def breakout_persisted_or_retested(
    completed_minute_bars: list[list],
    breakout_level: float,
    *,
    persistence_bars: int = 2,
    retest_tolerance_pct: float = 0.002,
) -> bool:
    """Confirm a breakout without using a forming one-minute candle.

    Persistence requires consecutive completed closes at or above the level.
    A retest requires an earlier completed close above the level followed by a
    completed candle that touches the level within tolerance and closes back
    above it.
    """
    if breakout_level <= 0 or persistence_bars < 1:
        return False
    if len(completed_minute_bars) >= persistence_bars and all(
        float(candle[4]) >= breakout_level
        for candle in completed_minute_bars[-persistence_bars:]
    ):
        return True

    lower = breakout_level * (1 - retest_tolerance_pct)
    upper = breakout_level * (1 + retest_tolerance_pct)
    breakout_seen = False
    for candle in completed_minute_bars:
        low = float(candle[3])
        close = float(candle[4])
        if breakout_seen and lower <= low <= upper and close >= breakout_level:
            return True
        breakout_seen = breakout_seen or close > breakout_level
    return False


def market_gate_at(market: MarketData, decision_time: str) -> MarketGate:
    """Reproduce the four production market factors at a historical instant."""
    nifty = _bars_before(market.intraday_candles.get(NIFTY_50_TOKEN, []), decision_time)
    vix = _bars_before(market.intraday_candles.get(INDIA_VIX_TOKEN, []), decision_time)
    bees = _bars_before(market.intraday_candles.get(NIFTYBEES_TOKEN, []), decision_time)
    nifty_previous = market.previous_closes.get(NIFTY_50_TOKEN, 0.0)
    vix_previous = market.previous_closes.get(INDIA_VIX_TOKEN, 0.0)

    index_pass = bool(
        nifty and nifty_previous > 0 and float(nifty[-1][4]) > nifty_previous
    )

    advancing = declining = 0
    for token in NIFTY_50_CONSTITUENTS.values():
        bars = _bars_before(market.intraday_candles.get(token, []), decision_time)
        previous = market.previous_closes.get(token, 0.0)
        if not bars or previous <= 0:
            continue
        current = float(bars[-1][4])
        if current > previous:
            advancing += 1
        elif current < previous:
            declining += 1
    breadth_ratio = advancing / declining if declining else (99.0 if advancing else 0.0)
    # Production uses the ratio directly and has no minimum-response count.
    breadth_pass = breadth_ratio > 1.2

    strength_pass = False
    if nifty:
        nifty_above_open = float(nifty[-1][4]) > float(nifty[0][1])
        volume = sum(float(c[5]) for c in bees)
        vwap = (
            sum(
                ((float(c[2]) + float(c[3]) + float(c[4])) / 3) * float(c[5])
                for c in bees
            )
            / volume
            if volume > 0
            else 0.0
        )
        bees_ltp = float(bees[-1][4]) if bees else 0.0
        # Match production's explicit fallback: unavailable ETF VWAP does not
        # penalize the factor.
        bees_above_vwap = bees_ltp > vwap if vwap > 0 else True
        strength_pass = nifty_above_open and bees_above_vwap

    vix_change = (
        (float(vix[-1][4]) - vix_previous) / vix_previous * 100
        if vix and vix_previous > 0
        else 0.0
    )
    # Match production, where a missing VIX quote becomes 0% and passes.
    volatility_pass = vix_change < 5.0
    score = sum((index_pass, breadth_pass, strength_pass, volatility_pass))
    return MarketGate(
        score=score,
        bullish=score >= 2,
        index_pass=index_pass,
        breadth_pass=breadth_pass,
        strength_pass=strength_pass,
        volatility_pass=volatility_pass,
        advancing=advancing,
        declining=declining,
    )


def _trail_pct(profit_pct: float, candle_time: str) -> float:
    late = candle_time >= "14:30"
    for threshold, trail in TRAIL_TIERS:
        if profit_pct >= threshold:
            return max(trail - LATE_SESSION_TRAIL_REDUCTION, 0.01) if late else trail
    trail = TRAIL_TIERS[-1][1]
    return max(trail - LATE_SESSION_TRAIL_REDUCTION, 0.01) if late else trail


def staged_stop_floor(
    entry_price: float,
    highest_price: float,
    config: BacktestConfig,
) -> float | None:
    """Return the strongest observation-derived floor earned so far.

    The caller supplies a high from completed prior candles, ensuring that the
    returned floor can only affect a later candle.
    """
    if not config.staged_stops_enabled or entry_price <= 0:
        return None

    mfe = (highest_price - entry_price) / entry_price
    floor: float | None = None
    for trigger, floor_from_entry in sorted(config.staged_stop_floors):
        if mfe >= trigger:
            floor = entry_price * (1 + floor_from_entry)
    return round(floor, 2) if floor is not None else None


def break_even_plus_cost_floor(
    entry_price: float,
    highest_price: float,
    config: BacktestConfig,
) -> float | None:
    """Return the raw stop required to cover modeled exit costs after +2%.

    Entry price is already the simulated fill. The floor compensates for both
    fee sides and adverse exit slippage, using the exact zero-net fee equation.
    """
    if (
        not config.break_even_plus_cost_enabled
        or entry_price <= 0
        or highest_price < entry_price * (1 + config.break_even_plus_cost_trigger)
    ):
        return None
    fee_rate = config.fees_bps_per_side / 10_000
    exit_slippage = config.slippage_bps / 10_000
    if fee_rate >= 1 or exit_slippage >= 1:
        raise ValueError("backtest costs must be below 100%")
    required_exit_fill = entry_price * (1 + fee_rate) / (1 - fee_rate)
    raw_stop = required_exit_fill / (1 - exit_slippage)
    return round(raw_stop, 2)


def simulate_trade(
    stock: HistoricalStock,
    decision_time: str,
    quantity: int,
    reentry_round: int,
    market_score: int,
    composite_score: float,
    config: BacktestConfig,
) -> BacktestTrade:
    """Conservative OHLC execution from the first executable unseen bar."""
    known = _bars_before(stock.intraday_candles, decision_time)
    signal_price = float(known[-1][4])
    fill_not_before = _add_minutes(decision_time, config.entry_delay_minutes)
    future = _bars_from(stock.intraday_candles, fill_not_before)
    if not future:
        # Scan windows end well before market close, so this indicates corrupt
        # or incomplete intraday data rather than a legitimate unfilled trade.
        raise ValueError(
            f"no executable candle for {stock.symbol} after {fill_not_before}"
        )
    entry_time = _time_of(future[0])
    raw_entry = float(future[0][1])
    entry = raw_entry * (1 + config.slippage_bps / 10_000)
    atr = entry_atr_at(stock, decision_time, signal_price)
    distance = max(
        min(atr * INITIAL_ATR_MULT, entry * HARD_MAX_LOSS_PCT),
        entry * MIN_STOP_DISTANCE_PCT,
    )
    stop = round(entry - distance, 2)
    hard_stop = round(
        max(entry - atr * INITIAL_ATR_MULT, entry * (1 - HARD_MAX_LOSS_PCT)), 2
    )
    target = entry * (1 + TARGET_PROFIT_PCT)
    highest = entry
    profit_locked = False
    staged_floor = None
    cost_floor = None
    raw_exit = entry
    exit_time = config.force_exit
    exit_reason = "MARKET_CLOSE"

    for candle in future:
        candle_time = _time_of(candle)
        candle_open, candle_high, candle_low, candle_close = map(float, candle[1:5])
        if candle_time >= config.force_exit:
            raw_exit, exit_time, exit_reason = candle_open, candle_time, "MARKET_CLOSE"
            break

        active_stop = max(stop, hard_stop)
        if candle_low <= active_stop:
            raw_exit = min(candle_open, active_stop)
            exit_time = candle_time
            exit_reason = (
                "HARD_STOP"
                if active_stop == hard_stop
                else (
                    "PROFIT_LOCK"
                    if profit_locked
                    else (
                        "STAGED_STOP"
                        if staged_floor is not None and active_stop == staged_floor
                        else (
                            "BREAK_EVEN_PLUS_COST"
                            if cost_floor is not None and active_stop == cost_floor
                            else "TRAILING_STOP"
                        )
                    )
                )
            )
            break
        if candle_high >= target:
            raw_exit = max(candle_open, target)
            exit_time, exit_reason = candle_time, "TARGET_HIT"
            break

        # The current candle high is only allowed to protect the next candle.
        highest = max(highest, candle_high)
        intraday_gain = (
            (highest - stock.previous_close) / stock.previous_close
            if stock.previous_close > 0
            else 0.0
        )
        if intraday_gain >= INTRADAY_LOCK_THRESHOLD:
            profit_locked = True
            new_stop = max(
                stock.previous_close * (1 + INTRADAY_LOCK_FLOOR_PCT),
                highest * (1 - INTRADAY_LOCK_TRAIL_PCT),
            )
        else:
            profit = (highest - entry) / entry
            new_stop = highest * (1 - _trail_pct(profit, candle_time))
        staged_floor = staged_stop_floor(entry, highest, config)
        if staged_floor is not None:
            new_stop = max(new_stop, staged_floor)
        cost_floor = break_even_plus_cost_floor(entry, highest, config)
        if cost_floor is not None:
            new_stop = max(new_stop, cost_floor)
        stop = max(stop, round(new_stop, 2))
        raw_exit = candle_close
        exit_time = candle_time

    exit_price = raw_exit * (1 - config.slippage_bps / 10_000)
    gross = (exit_price - entry) * quantity
    fees = (entry + exit_price) * quantity * config.fees_bps_per_side / 10_000
    return BacktestTrade(
        date=str(stock.intraday_candles[0][0])[:10],
        symbol=stock.symbol,
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=round(entry, 4),
        exit_price=round(exit_price, 4),
        quantity=quantity,
        gross_pnl=round(gross, 2),
        fees=round(fees, 2),
        pnl=round(gross - fees, 2),
        exit_reason=exit_reason,
        reentry_round=reentry_round,
        market_score=market_score,
        composite_score=composite_score,
        signal_time=decision_time,
        entry_atr=round(atr, 6),
    )


def _find_entry(
    stocks: list[HistoricalStock],
    start: str,
    end: str,
    excluded: set[str],
    config: BacktestConfig,
    max_attempts: int | None = None,
) -> tuple[HistoricalStock, dict[str, Any], str] | None:
    by_symbol = {stock.symbol: stock for stock in stocks}
    for attempts, decision_time in enumerate(
        scan_times(start, end, config.bar_minutes),
        start=1,
    ):
        if max_attempts is not None and attempts > max_attempts:
            break
        ranked = rank_candidates_at(stocks, decision_time, excluded)[
            : config.scan_pool_size
        ]
        for candidate in ranked:
            stock = by_symbol[candidate["symbol"]]
            if stock.restricted_reason:
                continue
            if config.fno_only and stock.is_fno is not True:
                continue
            if not _bars_from(
                stock.intraday_candles,
                _add_minutes(decision_time, config.entry_delay_minutes),
            ):
                continue
            valid, _ = validate_candidate_at(candidate, stock, decision_time, config)
            if valid:
                return stock, candidate, decision_time
    return None


def backtest_day(
    day: str,
    stocks: list[HistoricalStock],
    market: MarketData,
    config: BacktestConfig | None = None,
) -> BacktestDay:
    """Execute the production daily state machine on point-in-time candles."""
    config = config or BacktestConfig()
    result = BacktestDay(date=day)

    activation_time = ""
    initial_gate: MarketGate | None = None
    for check_time in ("10:00", "11:00", "12:00", "13:00", "14:00"):
        gate = market_gate_at(market, check_time)
        result.market_checks.append((check_time, gate.score))
        if gate.bullish:
            activation_time, initial_gate = check_time, gate
            break
    if not initial_gate:
        result.skipped_reason = "market gate never reached 2/4"
        return result

    contra = initial_gate.score == 2
    scan_end = "12:00" if contra else config.scan_end

    excluded: set[str] = set()
    consecutive_losses = 0
    # Production's persistent risk state is updated from confirmed fill P&L;
    # fees are reported separately and therefore do not drive its entry gate.
    daily_risk_pnl = 0.0
    next_scan = activation_time

    for reentry_round in range((0 if contra else config.max_reentry_rounds) + 1):
        if consecutive_losses >= config.max_consecutive_losses:
            break
        if daily_risk_pnl <= -(config.capital * config.max_daily_loss_pct / 100):
            break

        attempts = None if reentry_round == 0 else 3
        effective_end = scan_end
        # The live loop always performs its first scan before checking the
        # retry cutoff. Logs show this when contra mode activates after noon.
        if reentry_round == 0 and next_scan >= scan_end:
            effective_end = next_scan
            attempts = 1
        found = _find_entry(
            stocks, next_scan, effective_end, excluded, config, attempts
        )
        if not found:
            if not result.trades:
                result.skipped_reason = "no point-in-time candidate passed validation"
            break

        stock, candidate, decision_time = found
        atr = entry_atr_at(stock, decision_time, float(candidate["ltp"]))
        provisional_stop = max(
            float(candidate["ltp"]) - atr * INITIAL_ATR_MULT,
            float(candidate["ltp"]) * (1 - HARD_MAX_LOSS_PCT),
        )
        quantity = calculate_backtest_quantity(
            float(candidate["ltp"]), provisional_stop, config
        )
        if quantity <= 0:
            excluded.add(stock.symbol)
            continue

        trade = simulate_trade(
            stock,
            decision_time,
            quantity,
            reentry_round,
            initial_gate.score,
            float(candidate["composite_score"]),
            config,
        )
        result.trades.append(trade)
        excluded.add(stock.symbol)
        daily_risk_pnl += trade.gross_pnl
        consecutive_losses = consecutive_losses + 1 if trade.gross_pnl < 0 else 0

        # These are the four production smart re-entry gates.
        if (
            trade.exit_reason != "PROFIT_LOCK"
            or trade.gross_pnl < 0
            or trade.exit_time >= "12:30"
        ):
            break
        next_scan = _add_minutes(trade.exit_time, 15)
        if next_scan >= config.scan_end:
            break
        reentry_gate = market_gate_at(market, next_scan)
        result.market_checks.append((next_scan, reentry_gate.score))
        if not reentry_gate.bullish:
            break

    return result


class CandleStore:
    """Incremental, restart-safe Angel One candle cache."""

    def __init__(
        self, broker: AngelOneClient, root: str | Path = "data/production_backtest"
    ) -> None:
        self.broker = broker
        self.root = Path(root)

    def _path(self, token: str, interval: str, key: str) -> Path:
        return self.root / interval.lower() / f"{token}_{key}.json"

    @staticmethod
    def _range_key(key: str, start: str, end: str) -> str:
        """Prevent a cached history request from serving a different period."""
        if key != "history":
            return key
        digest = hashlib.sha256(f"{start}|{end}".encode()).hexdigest()[:16]
        return f"history_{digest}"

    @staticmethod
    def _write(path: Path, candles: list[list]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(candles), encoding="utf-8")
        temporary.replace(path)

    def _fetch(
        self,
        token: str,
        interval: str,
        start: str,
        end: str,
        *,
        retry_ambiguous_rate_limit: bool = True,
    ) -> list[list]:
        """Fetch candles, waiting out Angel One's rolling historical quota."""
        cooldown = float(
            os.getenv("BACKTEST_RATE_LIMIT_COOLDOWN_SECONDS", "65")
        )
        max_cooldowns = int(os.getenv("BACKTEST_RATE_LIMIT_MAX_COOLDOWNS", "60"))
        for cooldown_number in range(max_cooldowns + 1):
            try:
                return (
                    self.broker.get_candle_data(
                        "NSE", token, interval, start, end
                    )
                    or []
                )
            except (HTTPError, ValueError) as exc:
                message = str(exc).lower()
                # Angel One can use this ValueError both for rolling quotas and
                # for an oversized range response.  Chunk callers must split
                # the range instead of retrying the same deterministic failure.
                if isinstance(exc, ValueError) and not retry_ambiguous_rate_limit:
                    raise
                rate_limited = any(
                    marker in message
                    for marker in (
                        "exceeding access rate",
                        "too many requests",
                        "status=429",
                    )
                )
                if not rate_limited or cooldown_number >= max_cooldowns:
                    raise
                logger.warning(
                    "Historical-data quota reached; waiting %.0fs before retry "
                    "(%d/%d)",
                    cooldown,
                    cooldown_number + 1,
                    max_cooldowns,
                )
                time.sleep(cooldown)
        raise RuntimeError("unreachable historical-data retry state")

    def get(
        self, token: str, interval: str, start: str, end: str, key: str
    ) -> list[list]:
        path = self._path(token, interval, self._range_key(key, start, end))
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        candles = self._fetch(token, interval, start, end)
        self._write(path, candles)
        time.sleep(float(os.getenv("BACKTEST_API_DELAY_SECONDS", "0.2")))
        return candles

    def prefetch_intraday_days(
        self,
        token: str,
        days: list[str],
        interval: str = "FIVE_MINUTE",
        chunk_size: int = 60,
        max_calendar_days: int = 85,
    ) -> None:
        """Fetch multiple trading days per request and populate daily cache files."""
        missing = [
            day for day in days if not self._path(token, interval, day).exists()
        ]
        chunks: list[list[str]] = []
        chunk: list[str] = []
        for day in missing:
            span = (
                (date.fromisoformat(day) - date.fromisoformat(chunk[0])).days
                if chunk
                else 0
            )
            if chunk and (len(chunk) >= chunk_size or span > max_calendar_days):
                chunks.append(chunk)
                chunk = []
            chunk.append(day)
        if chunk:
            chunks.append(chunk)

        for chunk in chunks:
            try:
                candles = self._fetch(
                    token,
                    interval,
                    f"{chunk[0]} 09:15",
                    f"{chunk[-1]} 15:30",
                    retry_ambiguous_rate_limit=False,
                )
                by_day: dict[str, list[list]] = {day: [] for day in chunk}
                for candle in candles:
                    candle_day = str(candle[0])[:10]
                    if candle_day in by_day:
                        by_day[candle_day].append(candle)
                # The broker can silently cap large responses.  Never cache a
                # requested trading day as empty merely because it was omitted
                # from a truncated range response; recover it with a day query.
                missing_from_response: list[str] = []
                for day, rows in by_day.items():
                    if rows:
                        self._write(self._path(token, interval, day), rows)
                    else:
                        missing_from_response.append(day)
                time.sleep(float(os.getenv("BACKTEST_API_DELAY_SECONDS", "0.2")))
                for day in missing_from_response:
                    self.get(
                        token,
                        interval,
                        f"{day} 09:15",
                        f"{day} 15:30",
                        day,
                    )
            except Exception as exc:
                logger.warning(
                    "Chunked intraday fetch failed for %s (%s to %s): %s; "
                    "falling back to daily requests",
                    token,
                    chunk[0],
                    chunk[-1],
                    exc,
                )
                for day in chunk:
                    self.get(
                        token,
                        interval,
                        f"{day} 09:15",
                        f"{day} 15:30",
                        day,
                    )


def _daily_index(candles: list[list]) -> dict[str, int]:
    return {str(candle[0])[:10]: index for index, candle in enumerate(candles)}


def _stock_can_qualify(candles: list[list], index: int) -> bool:
    """Lossless data-acquisition filter; it never drives the trading decision."""
    if index < 1:
        return False
    today, previous = candles[index], candles[index - 1]
    previous_close = float(previous[4])
    return bool(
        previous_close > 0
        and float(today[2]) >= previous_close * (1 + MIN_GAIN_PCT / 100)
        and float(today[5]) >= MIN_VOLUME
        and float(today[2]) >= MIN_PRICE
        and float(today[3]) <= MAX_PRICE
    )


def load_universe_snapshots(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Load dated symbol/token snapshots used to eliminate survivorship bias.

    File format::

        {"2025-01-01": [{"symbol": "SBIN-EQ", "token": "3045"}, ...]}

    A snapshot remains active until the next dated snapshot.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise ValueError(
            "point-in-time universe file must be a non-empty date-to-stocks object"
        )
    snapshots: dict[str, list[dict[str, Any]]] = {}
    for snapshot_date, rows in payload.items():
        date.fromisoformat(str(snapshot_date))
        if not isinstance(rows, list):
            raise TypeError(f"universe snapshot {snapshot_date} must be a list")
        normalized = []
        for row in rows:
            symbol = str(row.get("symbol", ""))
            token = str(row.get("token", ""))
            if not symbol or not token:
                raise ValueError(
                    f"universe snapshot {snapshot_date} contains a missing symbol/token"
                )
            normalized.append(
                {
                    "symbol": symbol,
                    "token": token,
                    "name": str(row.get("name", "")),
                    "restricted_reason": str(row.get("restricted_reason", "")),
                    "is_fno": row.get("is_fno"),
                    "tradability_lists_complete": row.get(
                        "tradability_lists_complete"
                    ),
                }
            )
        snapshots[str(snapshot_date)] = normalized
    return snapshots


def universe_for_day(
    snapshots: dict[str, list[dict[str, Any]]],
    day: str,
) -> list[dict[str, Any]]:
    eligible = [snapshot_date for snapshot_date in snapshots if snapshot_date <= day]
    if not eligible:
        raise ValueError(
            f"no point-in-time universe snapshot exists on or before {day}"
        )
    return snapshots[max(eligible)]


def run_production_backtest() -> tuple[list[BacktestTrade], list[BacktestDay]]:
    """Download/cache required data and execute the production-equivalent engine."""
    settings = load_settings()
    intraday_interval = os.getenv(
        "BACKTEST_INTRADAY_INTERVAL", "ONE_MINUTE"
    ).strip().upper()
    if intraday_interval not in {"ONE_MINUTE", "FIVE_MINUTE"}:
        raise ValueError(
            "BACKTEST_INTRADAY_INTERVAL must be ONE_MINUTE or FIVE_MINUTE"
        )
    default_scan_step = "2" if intraday_interval == "ONE_MINUTE" else "5"
    slippage_bps, slippage_source = resolve_slippage_bps(
        os.getenv("BACKTEST_SLIPPAGE_BPS", "auto"),
        os.getenv("REPLAY_REPORT_DIR", "data/replay_reports"),
    )
    config = BacktestConfig(
        capital=settings.capital,
        leverage=settings.intraday_leverage,
        risk_per_trade_pct=settings.risk_per_trade_pct,
        max_daily_loss_pct=settings.max_daily_loss_pct,
        max_consecutive_losses=settings.max_consecutive_losses,
        slippage_bps=slippage_bps,
        fees_bps_per_side=float(os.getenv("BACKTEST_FEES_BPS_PER_SIDE", "10")),
        assumed_spread_bps=float(os.getenv("BACKTEST_ASSUMED_SPREAD_BPS", "10")),
        sizing_mode=os.getenv("BACKTEST_SIZING_MODE", "risk").strip().lower(),
        bar_minutes=int(
            os.getenv("BACKTEST_SCAN_STEP_MINUTES", default_scan_step)
        ),
        intraday_interval=intraday_interval,
        fno_only=settings.fno_only,
        entry_delay_minutes=max(
            0, int(os.getenv("BACKTEST_ENTRY_DELAY_MINUTES", "1"))
        ),
        entry_range_position_min=float(
            os.getenv("BACKTEST_ENTRY_RANGE_POSITION_MIN", "0.95")
        ),
        completed_minute_volume_required=os.getenv(
            "BACKTEST_COMPLETED_MINUTE_VOLUME", "true"
        ).strip().lower() in {"1", "true", "yes", "on"},
        breakout_confirmation=os.getenv(
            "BACKTEST_BREAKOUT_CONFIRMATION", "persistence_or_retest"
        ).strip().lower(),
        breakout_persistence_bars=max(
            1, int(os.getenv("BACKTEST_BREAKOUT_PERSISTENCE_BARS", "2"))
        ),
        breakout_retest_tolerance_pct=float(
            os.getenv("BACKTEST_BREAKOUT_RETEST_TOLERANCE_PCT", "0.002")
        ),
        staged_stops_enabled=os.getenv(
            "BACKTEST_STAGED_STOPS", "false"
        ).strip().lower() in {"1", "true", "yes", "on"},
        break_even_plus_cost_enabled=os.getenv(
            "BACKTEST_BREAK_EVEN_PLUS_COST", "false"
        ).strip().lower() in {"1", "true", "yes", "on"},
        break_even_plus_cost_trigger=float(
            os.getenv("BACKTEST_BREAK_EVEN_TRIGGER_PCT", "0.02")
        ),
    )
    lookback = int(os.getenv("BACKTEST_LOOKBACK_DAYS", "365"))
    now = datetime.now(IST)
    period_start, period_end = resolve_backtest_dates(now, lookback)
    history_start = period_start - timedelta(days=10)
    start_text = f"{history_start.isoformat()} 09:00"
    end_text = f"{period_end.isoformat()} 15:30"

    universe_path = os.getenv("BACKTEST_POINT_IN_TIME_UNIVERSE", "").strip()
    generated_universe = Path("data/universe_snapshots.json")
    if not universe_path and generated_universe.exists():
        universe_path = str(generated_universe)
    require_point_in_time = os.getenv(
        "BACKTEST_REQUIRE_POINT_IN_TIME_UNIVERSE",
        "true",
    ).strip().lower() in {"1", "true", "yes", "on"}
    universe_snapshots: dict[str, list[dict[str, Any]]] | None = None
    universe: list[dict[str, Any]] | None = None
    if universe_path:
        universe_snapshots = load_universe_snapshots(universe_path)
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for rows in universe_snapshots.values():
            for row in rows:
                unique[(row["symbol"], row["token"])] = row
        universe = list(unique.values())
        logger.info(
            "Loaded %d dated universe snapshots (%d unique securities)",
            len(universe_snapshots),
            len(universe),
        )
    else:
        if require_point_in_time:
            raise RuntimeError(
                "A production-grade backtest requires BACKTEST_POINT_IN_TIME_UNIVERSE. "
                "Provide dated NSE symbol/token snapshots, or deliberately set "
                "BACKTEST_REQUIRE_POINT_IN_TIME_UNIVERSE=false for an exploratory "
                "survivorship-biased run."
            )

    tradability_snapshot_complete = bool(universe_snapshots) and all(
        row.get("tradability_lists_complete") is True
        for rows in (universe_snapshots or {}).values()
        for row in rows
    )
    if universe_snapshots and not tradability_snapshot_complete:
        logger.warning(
            "Universe snapshots predate tradability metadata; ASM/GSM/F&O "
            "membership cannot be reproduced for those dates"
        )
    broker = AngelOneClient.login(
        api_key=settings.api_key,
        client_id=settings.client_id,
        pin=settings.pin,
        totp_secret=settings.totp_secret,
    )
    store = CandleStore(broker)
    if universe is None:
        universe = load_nse_equity_tokens(broker)
        logger.warning("Using today's scrip master: results have survivorship bias")
    limit = int(os.getenv("BACKTEST_STOCK_LIMIT", "0"))
    if limit > 0:
        logger.warning(
            "BACKTEST_STOCK_LIMIT=%d makes results non-production-equivalent", limit
        )
        universe = universe[:limit]

    logger.info("Loading daily history for %d universe stocks", len(universe))
    daily: dict[str, list[list]] = {}
    for number, stock in enumerate(universe, 1):
        token = stock["token"]
        daily[token] = store.get(token, "ONE_DAY", start_text, end_text, "history")
        if number % 100 == 0:
            logger.info("Daily history %d/%d", number, len(universe))

    market_tokens = set(NIFTY_50_CONSTITUENTS.values()) | {
        NIFTY_50_TOKEN,
        INDIA_VIX_TOKEN,
        NIFTYBEES_TOKEN,
    }
    for token in market_tokens:
        if token not in daily:
            daily[token] = store.get(token, "ONE_DAY", start_text, end_text, "history")

    daily_indices = {token: _daily_index(series) for token, series in daily.items()}
    index_daily = daily[NIFTY_50_TOKEN]
    trading_days = [
        str(c[0])[:10]
        for c in index_daily
        if period_start.isoformat() <= str(c[0])[:10] <= period_end.isoformat()
    ]
    default_chunk_size = "12" if intraday_interval == "ONE_MINUTE" else "60"
    default_calendar_days = "18" if intraday_interval == "ONE_MINUTE" else "85"
    chunk_size = int(
        os.getenv("BACKTEST_INTRADAY_CHUNK_DAYS", default_chunk_size)
    )
    max_calendar_days = int(
        os.getenv("BACKTEST_INTRADAY_MAX_CALENDAR_DAYS", default_calendar_days)
    )
    logger.info(
        "Prefetching market-gate intraday history for %d tokens in chunks of "
        "at most %d trading/%d calendar days",
        len(market_tokens),
        chunk_size,
        max_calendar_days,
    )
    for number, token in enumerate(sorted(market_tokens), 1):
        store.prefetch_intraday_days(
            token,
            trading_days,
            interval=intraday_interval,
            chunk_size=chunk_size,
            max_calendar_days=max_calendar_days,
        )
        if number % 10 == 0:
            logger.info("Market intraday history %d/%d", number, len(market_tokens))

    market_by_day: dict[str, MarketData] = {}
    active_days: set[str] = set()
    for day in trading_days:
        market_intraday: dict[str, list[list]] = {}
        market_previous: dict[str, float] = {}
        for token in market_tokens:
            series = daily[token]
            idx = daily_indices[token].get(day, -1)
            if idx >= 1:
                market_previous[token] = float(series[idx - 1][4])
            market_intraday[token] = store.get(
                token,
                intraday_interval,
                f"{day} 09:15",
                f"{day} 15:30",
                day,
            )

        market_for_day = MarketData(
            previous_closes=market_previous,
            intraday_candles=market_intraday,
        )
        market_by_day[day] = market_for_day
        if any(
            market_gate_at(market_for_day, check_time).bullish
            for check_time in ("10:00", "11:00", "12:00", "13:00", "14:00")
        ):
            active_days.add(day)

    required_candidate_days: dict[str, list[str]] = {}
    for day in sorted(active_days):
        day_universe = (
            universe_for_day(universe_snapshots, day)
            if universe_snapshots
            else universe
        )
        for stock in day_universe:
            token = stock["token"]
            series = daily.get(token, [])
            idx = daily_indices.get(token, {}).get(day, -1)
            if _stock_can_qualify(series, idx):
                required_candidate_days.setdefault(token, []).append(day)

    logger.info(
        "Prefetching candidate intraday history for %d securities across %d market-approved days",
        len(required_candidate_days),
        len(active_days),
    )
    for number, (token, required_days) in enumerate(
        sorted(required_candidate_days.items()), start=1
    ):
        store.prefetch_intraday_days(
            token,
            required_days,
            interval=intraday_interval,
            chunk_size=chunk_size,
            max_calendar_days=max_calendar_days,
        )
        if intraday_interval == "ONE_MINUTE":
            store.prefetch_intraday_days(
                token,
                required_days,
                interval="FIVE_MINUTE",
                chunk_size=60,
                max_calendar_days=85,
            )
        if number % 50 == 0:
            logger.info(
                "Candidate intraday history %d/%d",
                number,
                len(required_candidate_days),
            )

    all_trades: list[BacktestTrade] = []
    days: list[BacktestDay] = []
    for day_number, day in enumerate(trading_days, 1):
        logger.info(
            "[%d/%d] building point-in-time dataset for %s",
            day_number,
            len(trading_days),
            day,
        )
        market_for_day = market_by_day[day]
        if day not in active_days:
            summary = backtest_day(day, [], market_for_day, config)
            days.append(summary)
            logger.info("%s: trades=0 pnl=+0.00 %s", day, summary.skipped_reason)
            continue

        day_universe = (
            universe_for_day(universe_snapshots, day)
            if universe_snapshots
            else universe
        )
        stocks_for_day: list[HistoricalStock] = []
        for stock in day_universe:
            token = stock["token"]
            series = daily.get(token, [])
            idx = daily_indices.get(token, {}).get(day, -1)
            if not _stock_can_qualify(series, idx):
                continue
            bars = store.get(
                token,
                intraday_interval,
                f"{day} 09:15",
                f"{day} 15:30",
                day,
            )
            if bars:
                breakout_bars = (
                    store.get(
                        token,
                        "FIVE_MINUTE",
                        f"{day} 09:15",
                        f"{day} 15:30",
                        day,
                    )
                    if intraday_interval == "ONE_MINUTE"
                    else []
                )
                stocks_for_day.append(
                    HistoricalStock(
                        symbol=stock["symbol"],
                        token=token,
                        previous_close=float(series[idx - 1][4]),
                        completed_daily_candles=series[max(0, idx - 5) : idx],
                        intraday_candles=bars,
                        breakout_candles=breakout_bars,
                        restricted_reason=str(
                            stock.get("restricted_reason", "")
                        ),
                        is_fno=stock.get("is_fno"),
                        tradability_lists_complete=stock.get(
                            "tradability_lists_complete"
                        ),
                    )
                )

        summary = backtest_day(
            day,
            stocks_for_day,
            market_for_day,
            config,
        )
        days.append(summary)
        all_trades.extend(summary.trades)
        logger.info(
            "%s: trades=%d pnl=%+.2f %s",
            day,
            len(summary.trades),
            summary.pnl,
            summary.skipped_reason,
        )

    universe_fingerprint = "current-scrip-master"
    if universe_path:
        universe_fingerprint = hashlib.sha256(
            Path(universe_path).read_bytes()
        ).hexdigest()
    report_paths = write_report_files(
        all_trades,
        days,
        config=config,
        metadata={
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "point_in_time_universe": bool(universe_snapshots),
            "universe_path": universe_path,
            "universe_sha256": universe_fingerprint,
            "slippage_source": slippage_source,
            "historical_order_book": False,
            "historical_partial_minute_volume": False,
            "historical_circuit_limits": False,
            "historical_tradability_lists": tradability_snapshot_complete,
        },
    )
    print_report(
        all_trades, days, config, point_in_time_universe=bool(universe_snapshots)
    )
    print(f"Daily report: {report_paths['daily']}")
    print(f"Trade report: {report_paths['trades']}")
    print(f"Run manifest: {report_paths['manifest']}")
    return all_trades, days


def write_report_files(
    trades: list[BacktestTrade],
    days: list[BacktestDay],
    *,
    config: BacktestConfig | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Path]:
    """Persist machine-readable daily and trade reports for the latest run."""
    report_dir = Path(
        os.getenv("BACKTEST_REPORT_DIR", "data/production_backtest/reports")
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    daily_path = report_dir / "daily_pnl.csv"
    trade_path = report_dir / "trades.csv"
    manifest_path = report_dir / "run_manifest.json"

    with daily_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=("date", "pnl", "trades", "skipped_reason")
        )
        writer.writeheader()
        for summary in days:
            writer.writerow(
                {
                    "date": summary.date,
                    "pnl": f"{summary.pnl:.2f}",
                    "trades": len(summary.trades),
                    "skipped_reason": summary.skipped_reason,
                }
            )

    trade_fields = tuple(BacktestTrade.__dataclass_fields__)
    with trade_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=trade_fields)
        writer.writeheader()
        for trade in trades:
            writer.writerow(
                {field_name: getattr(trade, field_name) for field_name in trade_fields}
            )

    manifest_payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        "config": asdict(config) if config else None,
        "metadata": metadata or {},
        "results": {
            "days": len(days),
            "trades": len(trades),
            "net_pnl": round(sum(trade.pnl for trade in trades), 2),
        },
    }
    fingerprint_source = json.dumps(
        {
            "config": manifest_payload["config"],
            "metadata": manifest_payload["metadata"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    manifest_payload["run_fingerprint"] = hashlib.sha256(
        fingerprint_source.encode("utf-8")
    ).hexdigest()
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return {
        "daily": daily_path.resolve(),
        "trades": trade_path.resolve(),
        "manifest": manifest_path.resolve(),
    }


def print_report(
    trades: list[BacktestTrade],
    days: list[BacktestDay],
    config: BacktestConfig,
    *,
    point_in_time_universe: bool = False,
) -> None:
    wins = [trade for trade in trades if trade.pnl > 0]
    losses = [trade for trade in trades if trade.pnl <= 0]
    gross_profit = sum(trade.pnl for trade in wins)
    gross_loss = abs(sum(trade.pnl for trade in losses))
    total = gross_profit - gross_loss
    win_rate = len(wins) / len(trades) * 100 if trades else 0.0
    average_win = gross_profit / len(wins) if wins else 0.0
    average_loss = -gross_loss / len(losses) if losses else 0.0
    cumulative = peak = maximum_drawdown = 0.0
    for trade in trades:
        cumulative += trade.pnl
        peak = max(peak, cumulative)
        maximum_drawdown = max(maximum_drawdown, peak - cumulative)
    exit_reasons: dict[str, tuple[int, float]] = {}
    for trade in trades:
        count, pnl = exit_reasons.get(trade.exit_reason, (0, 0.0))
        exit_reasons[trade.exit_reason] = (count + 1, pnl + trade.pnl)
    print("\nPRODUCTION-EQUIVALENT POINT-IN-TIME BACKTEST")
    print("=" * 58)
    if days:
        print(f"Period: {days[0].date} to {days[-1].date}")
    print(f"Trading days: {len(days)} | Trades: {len(trades)}")
    print(f"Wins: {len(wins)} | Losses: {len(losses)} | Win rate: {win_rate:.1f}%")
    print(f"Net P&L: Rs.{total:+,.2f}")
    print(f"Average win/loss: Rs.{average_win:+,.2f} / Rs.{average_loss:+,.2f}")
    print(f"Maximum drawdown: Rs.{maximum_drawdown:,.2f}")
    print(
        f"Profit factor: {gross_profit / gross_loss:.2f}"
        if gross_loss
        else "Profit factor: n/a"
    )
    print(
        f"Costs: {config.slippage_bps:.1f} bps slippage and {config.fees_bps_per_side:.1f} bps fees per side"
    )
    print(f"Sizing mode: {config.sizing_mode}")
    print(
        "Entry confirmation: "
        f"range>={config.entry_range_position_min:.2f}, "
        f"completed-minute-volume={config.completed_minute_volume_required}, "
        f"breakout={config.breakout_confirmation}"
    )
    print(
        "Protection experiment: "
        f"staged={config.staged_stops_enabled}, "
        f"break-even-plus-cost={config.break_even_plus_cost_enabled}"
    )
    print("Data assumptions:")
    if config.intraday_interval == "ONE_MINUTE":
        print(
            f"  - One-minute bars drive {config.bar_minutes}-minute scans; live "
            "ticks inside each minute remain unavailable."
        )
    else:
        print("  - Five-minute bars approximate the production two-minute scan loop.")
    print("  - Historical order-book buy pressure is neutral (0.5).")
    print("  - Entry ATR uses only completed five-minute candles.")
    print(
        f"  - Fills occur at the first bar at least {config.entry_delay_minutes} "
        "minute(s) after the signal."
    )
    print(
        f"  - Historical bid/ask spread is assumed at {config.assumed_spread_bps:.1f} bps."
    )
    universe_label = (
        "dated point-in-time snapshots"
        if point_in_time_universe
        else "current scrip master (survivorship bias)"
    )
    print(f"  - Universe: {universe_label}.")
    if exit_reasons:
        print("Exit reasons:")
        for reason, (count, pnl) in sorted(exit_reasons.items()):
            print(f"  {reason}: {count} trades, Rs.{pnl:+,.2f}")
    if days:
        print("Daily P&L:")
        for summary in days:
            print(
                f"  {summary.date}: Rs.{summary.pnl:+,.2f} ({len(summary.trades)} trades)"
            )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    run_production_backtest()

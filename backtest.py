"""Year-long backtest of the full trading workflow.

Uses disk caching to avoid re-fetching candle data across runs.
Simulates: scan → validate → enter → ATR trailing stop → exit.
"""

from __future__ import annotations

import json
import os
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from broker.angelone_client import AngelOneClient
from config.settings import load_settings
from strategy.market_scanner import (
    MIN_GAIN_PCT, MAX_GAIN_PCT, MIN_PRICE, MAX_PRICE, MIN_VOLUME,
    load_nse_equity_tokens,
)
from monitor.position_tracker import (
    INITIAL_ATR_MULT, HARD_MAX_LOSS_PCT, MIN_STOP_DISTANCE_PCT,
    TRAIL_TIERS, INTRADAY_LOCK_THRESHOLD, INTRADAY_LOCK_TRAIL_PCT,
    INTRADAY_LOCK_FLOOR_PCT, TARGET_PROFIT_PCT,
)
from utils.atr import compute_atr_from_candles
from monitor.risk_state import calculate_position_size

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("backtest")

IST = ZoneInfo("Asia/Kolkata")
CACHE_DIR = Path("data/backtest_cache")

# --- Backtest parameters (mirror production) ---
CAPITAL = 100_000
LEVERAGE = 5.0
TOP_N = 1
MAX_CONSECUTIVE_LOSSES = 2
MAX_REENTRY_ROUNDS = 2
REENTRY_COOLDOWN_CANDLES = 3  # 15 min cooldown = 3 x 5-min candles.
LOOKBACK_DAYS = 1650  # ~4.5 years (max available from Angel One API).
STOCK_COUNT = 500  # Top N stocks from scrip master to scan.
MIN_RANGE_POSITION = 0.85
MIN_VOLUME_RATIO = 1.2
CANDLE_DELAY = 0.35
SLIPPAGE_BPS = float(os.getenv("BACKTEST_SLIPPAGE_BPS", "5"))
FEES_BPS_PER_SIDE = float(os.getenv("BACKTEST_FEES_BPS_PER_SIDE", "10"))


@dataclass
class Trade:
    date: str
    symbol: str
    token: str
    entry_price: float
    entry_time: str
    exit_price: float = 0.0
    exit_time: str = ""
    exit_reason: str = ""
    prev_close: float = 0.0
    atr: float = 0.0
    highest_price: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    quantity: int = 0
    reentry_round: int = 0
    entry_gain_pct: float = 0.0  # Stock's intraday gain at entry time.
    nifty_pct: float = 0.0      # Nifty close % change on this day.
    vix_pct: float = 0.0        # VIX % change on this day.
    market_score: int = 0       # Market bullishness score (3 or 4).
    nifty_lo_pct: float = 0.0   # Nifty intraday low % from prev close.
    fees: float = 0.0
    gross_pnl: float = 0.0


@dataclass
class DaySummary:
    date: str
    trades: list[Trade] = field(default_factory=list)
    daily_pnl: float = 0.0
    skipped_reason: str = ""


# === DISK CACHE ===

def cache_path(kind: str, token: str) -> Path:
    d = CACHE_DIR / kind
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{token}.json"


def read_cache(kind: str, token: str) -> list | None:
    p = cache_path(kind, token)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None


def write_cache(kind: str, token: str, data: list) -> None:
    p = cache_path(kind, token)
    p.write_text(json.dumps(data))


# === DATA FETCHING ===

def get_trading_days(broker: AngelOneClient, lookback: int) -> list[str]:
    """Get trading days from Nifty daily candles."""
    now = datetime.now(IST)
    start = (now - timedelta(days=lookback + 10)).strftime("%Y-%m-%d 09:00")
    end = now.strftime("%Y-%m-%d %H:%M")
    candles = broker.get_candle_data("NSE", "99926000", "ONE_DAY", start, end)
    today_str = now.strftime("%Y-%m-%d")
    return [c[0][:10] for c in candles if c[0][:10] != today_str]


def prefetch_daily_candles(
    broker: AngelOneClient, stocks: list[dict], lookback: int,
) -> dict[str, list[list]]:
    """Fetch daily candles for all stocks. Uses disk cache."""
    now = datetime.now(IST)
    start = (now - timedelta(days=lookback + 10)).strftime("%Y-%m-%d 09:00")
    end = now.strftime("%Y-%m-%d %H:%M")
    result: dict[str, list[list]] = {}
    total = len(stocks)

    min_candles = max(200, lookback // 2)  # Scale cache threshold with lookback.
    for i, stock in enumerate(stocks):
        token = stock["token"]
        cached = read_cache("daily", token)
        if cached and len(cached) > min_candles:
            result[token] = cached
        else:
            try:
                candles = broker.get_candle_data("NSE", token, "ONE_DAY", start, end)
                if candles:
                    result[token] = candles
                    write_cache("daily", token, candles)
                time.sleep(CANDLE_DELAY)
            except Exception:
                pass

        if (i + 1) % 100 == 0 or i + 1 == total:
            logger.info("  Daily candles: %d/%d (cached+fetched=%d)", i + 1, total, len(result))

    return result


def fetch_intraday_candles(
    broker: AngelOneClient, token: str, day_str: str,
) -> list[list]:
    """Fetch 5-min candles for one stock on one day. Uses disk cache."""
    cache_key = f"{token}_{day_str}"
    cached = read_cache("5min", cache_key)
    if cached:
        return cached

    start = day_str + " 09:15"
    end = day_str + " 15:30"
    try:
        time.sleep(CANDLE_DELAY)
        candles = broker.get_candle_data("NSE", token, "FIVE_MINUTE", start, end)
        if candles:
            write_cache("5min", cache_key, candles)
            return candles
    except Exception as exc:
        logger.warning("  5m candle fetch failed for %s on %s: %s", token, day_str, exc)
    return []


# === MARKET BULLISHNESS CHECK (historical) ===

# Nifty 50 constituent tokens (same as production).
NIFTY_50_TOKEN = "99926000"
INDIA_VIX_TOKEN = "99926017"

# Hardcoded Nifty 50 constituent tokens for breadth calculation.
NIFTY_50_CONSTITUENTS = {
    "RELIANCE": "2885", "TCS": "11536", "HDFCBANK": "1333", "INFY": "1594",
    "ICICIBANK": "4963", "HINDUNILVR": "1394", "ITC": "1660", "SBIN": "3045",
    "BHARTIARTL": "10604", "KOTAKBANK": "1922", "LT": "11483", "AXISBANK": "5900",
    "BAJFINANCE": "317", "ASIANPAINT": "236", "MARUTI": "10999", "HCLTECH": "7229",
    "TITAN": "3506", "SUNPHARMA": "3351", "ULTRACEMCO": "11532", "NTPC": "11630",
    "WIPRO": "3787", "TATAMOTORS": "3456", "M&M": "2031", "POWERGRID": "14977",
    "BAJAJFINSV": "16675", "NESTLEIND": "17963", "ONGC": "2475", "JSWSTEEL": "11723",
    "TATASTEEL": "3499", "ADANIENT": "25", "TECHM": "13538", "HDFCLIFE": "467",
    "INDUSINDBK": "5258", "DIVISLAB": "10940", "GRASIM": "1232", "SBILIFE": "21808",
    "COALINDIA": "20374", "BAJAJ-AUTO": "16669", "BRITANNIA": "547", "CIPLA": "694",
    "EICHERMOT": "910", "DRREDDY": "881", "APOLLOHOSP": "157", "TATACONSUM": "3432",
    "HEROMOTOCO": "1348", "BPCL": "526", "HINDALCO": "1363", "ADANIPORTS": "15083",
    "SHRIRAMFIN": "4306", "BEL": "383",
}


def check_market_bullish_historical(
    daily_candles: dict[str, list[list]],
    nifty_candles: list[list],
    vix_candles: list[list],
    day_str: str,
) -> tuple[bool, str]:
    """Point-in-time market gate using only data known before/at today's open."""
    nifty_idx = next(
        (i for i, candle in enumerate(nifty_candles) if candle[0][:10] == day_str), -1,
    )
    if nifty_idx < 2:
        return False, "Insufficient prior Nifty data"

    today, prior, prior2 = (
        nifty_candles[nifty_idx], nifty_candles[nifty_idx - 1], nifty_candles[nifty_idx - 2]
    )
    prior_close = float(prior[4])
    gap_pct = ((float(today[1]) - prior_close) / prior_close) * 100 if prior_close else 0.0
    prior_trend_pct = (
        ((float(prior[4]) - float(prior2[4])) / float(prior2[4])) * 100
        if float(prior2[4]) else 0.0
    )
    index_pass = gap_pct >= -0.5
    strength_pass = prior_trend_pct > 0

    advancing = declining = 0
    for token in NIFTY_50_CONSTITUENTS.values():
        candles = daily_candles.get(token) or []
        idx = next((i for i, candle in enumerate(candles) if candle[0][:10] == day_str), -1)
        if idx < 2:
            continue
        previous, previous2 = float(candles[idx - 1][4]), float(candles[idx - 2][4])
        if previous > previous2:
            advancing += 1
        elif previous < previous2:
            declining += 1
    breadth_ratio = advancing / declining if declining else (99.0 if advancing else 0.0)
    breadth_pass = advancing + declining >= 20 and breadth_ratio > 1.2

    vix_idx = next(
        (i for i, candle in enumerate(vix_candles) if candle[0][:10] == day_str), -1,
    )
    vix_pct = 0.0
    volatility_pass = False
    if vix_idx >= 2:
        vix_prior = float(vix_candles[vix_idx - 1][4])
        vix_prior2 = float(vix_candles[vix_idx - 2][4])
        if vix_prior2 > 0:
            vix_pct = ((vix_prior - vix_prior2) / vix_prior2) * 100
            volatility_pass = vix_pct < 5.0

    score = sum((index_pass, breadth_pass, strength_pass, volatility_pass))
    bullish = score >= 3
    reason = (
        f"OpenGap={gap_pct:+.1f}%({'Y' if index_pass else 'N'}) "
        f"PriorBreadth={advancing}A/{declining}D={breadth_ratio:.1f}({'Y' if breadth_pass else 'N'}) "
        f"PriorTrend={prior_trend_pct:+.1f}%({'Y' if strength_pass else 'N'}) "
        f"PriorVIX={vix_pct:+.1f}%({'Y' if volatility_pass else 'N'}) "
        f"Score={score}/4 → {'BULLISH' if bullish else 'BEARISH'}"
    )
    return bullish, reason


# === SCANNING ===

def scan_for_candidates(
    daily_candles: dict[str, list[list]], stocks: list[dict], day_str: str,
) -> list[dict]:
    """Build a universe using only information available before the session."""
    candidates: list[dict] = []
    for stock in stocks:
        token = stock["token"]
        candles = daily_candles.get(token) or []
        idx = next((i for i, candle in enumerate(candles) if candle[0][:10] == day_str), -1)
        if idx < 1:
            continue
        prior = candles[idx - 1]
        prev_close = float(prior[4])
        prior_volume = int(float(prior[5]))
        if prev_close < MIN_PRICE or prev_close > MAX_PRICE or prior_volume < MIN_VOLUME:
            continue
        candidates.append({
            "symbol": stock.get("symbol", ""),
            "token": token,
            "prev_close": prev_close,
            "prior_volume": prior_volume,
        })
    candidates.sort(key=lambda item: item["prior_volume"], reverse=True)
    return candidates


# === ENTRY VALIDATION & TRADE SIMULATION ===

def try_entry_and_simulate(
    candles_5m: list[list], symbol: str, token: str,
    prev_close: float, day_str: str, leveraged_capital: float,
    already_entered: int, earliest_time: str = "10:00",
    max_gain_pct: float = MAX_GAIN_PCT,
    latest_time: str = "13:45",
) -> Trade | None:
    """Try entry at scan times (from earliest_time onward), validate, and simulate."""
    candle_by_time: dict[str, int] = {}
    for idx, c in enumerate(candles_5m):
        candle_by_time[c[0][11:16]] = idx

    scan_times = [
        "10:00", "10:15", "10:30", "10:45",
        "11:00", "11:15", "11:30", "11:45",
        "12:00", "12:15", "12:30", "12:45",
        "13:00", "13:15", "13:30", "13:45",
    ]

    for scan_time in scan_times:
        if scan_time < earliest_time:
            continue
        if scan_time > latest_time:
            break

        idx = candle_by_time.get(scan_time)
        if idx is None or idx < 3:
            continue

        candle = candles_5m[idx]
        ltp = float(candle[4])
        vol = float(candle[5])

        # Check 1: Gain in allowed range.
        gain_pct = ((ltp - prev_close) / prev_close) * 100
        if gain_pct < MIN_GAIN_PCT or gain_pct > max_gain_pct:
            continue

        # Check 2: Range position >= 0.85.
        running_high = max(float(c[2]) for c in candles_5m[:idx + 1])
        running_low = min(float(c[3]) for c in candles_5m[:idx + 1])
        day_range = running_high - running_low
        range_pos = (ltp - running_low) / day_range if day_range > 0 else 1.0
        if range_pos < MIN_RANGE_POSITION:
            continue

        # Check 3: Micro breakout (LTP >= last candle high).
        # Skip breakout check for "near-breakout" stocks: range >= 0.85 and
        # gain 5-7%.  These are at the top of the day's range but pulled back
        # one tick — analysis shows 51% win rate, 1.48 PF when entered anyway.
        near_breakout = range_pos >= MIN_RANGE_POSITION and 5.0 <= gain_pct < 7.0
        if not near_breakout and ltp < float(candles_5m[idx - 1][2]):
            continue

        # Check 4: Volume confirmation.
        prior_vols = [float(candles_5m[j][5]) for j in range(max(0, idx - 5), idx)]
        avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0
        if avg_vol <= 0:
            continue  # No prior volume data — skip.
        vol_ratio = vol / avg_vol
        if vol_ratio < MIN_VOLUME_RATIO:
            continue

        # Compute ATR.
        atr_candles = candles_5m[max(0, idx - 12):idx + 1]
        atr = compute_atr_from_candles(atr_candles)
        if atr <= 0:
            atr = ltp * 0.005

        provisional_stop = max(
            ltp - (atr * INITIAL_ATR_MULT),
            ltp * (1 - HARD_MAX_LOSS_PCT),
        )
        qty = calculate_position_size(
            capital=CAPITAL,
            entry_price=ltp,
            stop_price=provisional_stop,
            risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "1.0")),
            maximum_notional=leveraged_capital * 0.95,
        )
        if qty <= 0:
            continue

        trade = simulate_trade(
            candles_5m, idx, ltp, prev_close, atr, qty,
            symbol, token, day_str, scan_time,
        )
        trade.entry_gain_pct = round(gain_pct, 2)
        return trade

    return None


def simulate_trade(
    candles_5m: list[list], entry_idx: int, entry_price: float,
    prev_close: float, atr: float, qty: int,
    symbol: str, token: str, day_str: str, entry_time: str,
    lock_threshold: float = INTRADAY_LOCK_THRESHOLD,
    lock_floor_pct: float = INTRADAY_LOCK_FLOOR_PCT,
) -> Trade:
    """Conservative OHLC simulation with slippage, fees, and no intrabar look-ahead."""
    entry_fill = entry_price * (1 + SLIPPAGE_BPS / 10_000)
    trade = Trade(
        date=day_str, symbol=symbol, token=token,
        entry_price=entry_fill, entry_time=entry_time,
        prev_close=prev_close, atr=atr, quantity=qty,
    )
    atr_dist = atr * INITIAL_ATR_MULT
    pct_dist = entry_fill * HARD_MAX_LOSS_PCT
    min_dist = entry_fill * MIN_STOP_DISTANCE_PCT
    stop_loss = round(entry_fill - max(min(atr_dist, pct_dist), min_dist), 2)
    hard_stop = round(max(
        entry_fill - (atr * INITIAL_ATR_MULT),
        entry_fill * (1 - HARD_MAX_LOSS_PCT),
    ), 2)
    target_price = entry_fill * (1 + TARGET_PROFIT_PCT)
    highest = entry_fill
    profit_locked = False
    exit_price = 0.0

    for i in range(entry_idx + 1, len(candles_5m)):
        candle = candles_5m[i]
        candle_time = candle[0][11:16]
        c_open, c_high, c_low, c_close = map(float, candle[1:5])
        if candle_time >= "15:05":
            exit_price, trade.exit_time, trade.exit_reason = c_close, candle_time, "MARKET_CLOSE"
            break

        active_stop = max(stop_loss, hard_stop)
        stop_hit, target_hit = c_low <= active_stop, c_high >= target_price
        if stop_hit:
            exit_price = min(c_open, active_stop)
            trade.exit_time = candle_time
            trade.exit_reason = (
                "HARD_STOP" if active_stop == hard_stop
                else ("PROFIT_LOCK" if profit_locked else "TRAILING_STOP")
            )
            break
        if target_hit:
            exit_price = max(c_open, target_price)
            trade.exit_time, trade.exit_reason = candle_time, "TARGET_HIT"
            highest = max(highest, c_high)
            break

        # This candle's high tightens the stop only for the next candle.
        highest = max(highest, c_high)
        intraday_gain = (highest - prev_close) / prev_close if prev_close > 0 else 0
        if intraday_gain >= lock_threshold:
            profit_locked = True
            new_stop = round(max(
                prev_close * (1 + lock_floor_pct),
                highest * (1 - INTRADAY_LOCK_TRAIL_PCT),
            ), 2)
        else:
            profit_pct = (highest - entry_fill) / entry_fill
            trail_pct = get_trail_pct(profit_pct, candle_time)
            new_stop = round(
                highest - max(highest * trail_pct, highest * MIN_STOP_DISTANCE_PCT), 2,
            )
        stop_loss = max(stop_loss, new_stop)
    else:
        last = candles_5m[-1]
        exit_price = float(last[4])
        trade.exit_time, trade.exit_reason = last[0][11:16], "MARKET_CLOSE"

    exit_fill = exit_price * (1 - SLIPPAGE_BPS / 10_000)
    trade.exit_price = round(exit_fill, 4)
    trade.highest_price = highest
    trade.gross_pnl = round((exit_fill - entry_fill) * qty, 2)
    trade.fees = round((entry_fill + exit_fill) * qty * FEES_BPS_PER_SIDE / 10_000, 2)
    trade.pnl = round(trade.gross_pnl - trade.fees, 2)
    trade.pnl_pct = round((trade.pnl / (entry_fill * qty)) * 100, 2) if qty else 0.0
    return trade


def get_trail_pct(profit_pct: float, candle_time: str = "") -> float:
    """Look up fixed trail percentage, with late-session tightening after 14:30."""
    late_session = candle_time >= "14:30" if candle_time else False
    for threshold, trail in TRAIL_TIERS:
        if profit_pct >= threshold:
            if late_session:
                trail = max(trail - 0.005, 0.01)
            return trail
    base = TRAIL_TIERS[-1][1]
    if late_session:
        base = max(base - 0.005, 0.01)
    return base


# === DAY SIMULATION ===

def _add_candles_to_time(time_str: str, n_candles: int) -> str:
    """Add n 5-min candles to a time string. E.g. '10:30' + 3 = '10:45'."""
    h, m = int(time_str[:2]), int(time_str[3:5])
    total_min = h * 60 + m + n_candles * 5
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def _parse_market_context(
    reason: str, nifty_candles: list[list], day_str: str,
) -> tuple[float, float, int, float]:
    """Extract Nifty%, VIX%, score, and Nifty intraday low% from reason + candles."""
    import re
    nifty_pct = vix_pct = 0.0
    score = 0
    nifty_lo_pct = 0.0

    m = re.search(r"Nifty=([+\-\d.]+)%", reason)
    if m:
        nifty_pct = float(m.group(1))
    m = re.search(r"VIX=([+\-\d.]+)%", reason)
    if m:
        vix_pct = float(m.group(1))
    m = re.search(r"Score=(\d)/4", reason)
    if m:
        score = int(m.group(1))

    for j, c in enumerate(nifty_candles):
        if c[0][:10] == day_str and j > 0:
            prev_close = float(nifty_candles[j - 1][4])
            if prev_close > 0:
                nifty_lo_pct = ((float(c[3]) - prev_close) / prev_close) * 100
            break

    return nifty_pct, vix_pct, score, nifty_lo_pct


def backtest_one_day(
    broker: AngelOneClient, stocks: list[dict],
    daily_candles: dict[str, list[list]],
    nifty_candles: list[list], vix_candles: list[list],
    day_str: str,
) -> DaySummary:
    summary = DaySummary(date=day_str)

    # Market bullishness filter (4-factor model).
    bullish, reason = check_market_bullish_historical(
        daily_candles, nifty_candles, vix_candles, day_str,
    )
    if not bullish:
        summary.skipped_reason = f"Market not bullish: {reason}"
        return summary

    # Parse market context for tagging trades.
    nifty_pct, vix_pct, market_score, nifty_lo_pct = _parse_market_context(
        reason, nifty_candles, day_str,
    )

    # Contra-momentum mode: score=2 days — only enter before 12:00, no re-entry.
    contra_mode = market_score == 2

    candidates = scan_for_candidates(daily_candles, stocks, day_str)
    if not candidates:
        summary.skipped_reason = "No 5-10% gainers"
        return summary

    logger.info("  %s%d candidates", "CONTRA MODE — " if contra_mode else "", len(candidates))
    leveraged = CAPITAL * LEVERAGE
    consecutive_losses = 0
    traded_symbols: set[str] = set()  # Never re-enter the same stock.
    earliest_time = "10:00"

    # Contra-mode: no re-entry, scan only until 12:00.
    max_rounds = 0 if contra_mode else MAX_REENTRY_ROUNDS
    scan_cutoff = "14:00"

    # Pre-fetch 5-min candles for top candidates (cache avoids re-fetching).
    candle_cache: dict[str, list[list]] = {}

    for reentry_round in range(max_rounds + 1):
        if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            break
        if earliest_time >= scan_cutoff:  # Past scan window.
            break

        # Find the best candidate not already traded today.
        trade = None
        for cand in candidates:
            if cand["symbol"] in traded_symbols:
                continue

            token = cand["token"]
            if token not in candle_cache:
                candles_5m = fetch_intraday_candles(broker, token, day_str)
                candle_cache[token] = candles_5m
            else:
                candles_5m = candle_cache[token]

            if not candles_5m or len(candles_5m) < 5:
                continue

            trade = try_entry_and_simulate(
                candles_5m, cand["symbol"], token,
                cand["prev_close"], day_str, leveraged,
                already_entered=0, earliest_time=earliest_time,
                max_gain_pct=MAX_GAIN_PCT,
                latest_time="11:45" if contra_mode else "13:45",
            )
            if trade:
                traded_symbols.add(cand["symbol"])
                break

        if not trade:
            if reentry_round == 0:
                summary.skipped_reason = "No valid entries after validation"
            break

        trade.reentry_round = reentry_round
        trade.nifty_pct = nifty_pct
        trade.vix_pct = vix_pct
        trade.market_score = market_score
        trade.nifty_lo_pct = nifty_lo_pct
        summary.trades.append(trade)
        summary.daily_pnl += trade.pnl

        if trade.pnl < 0:
            consecutive_losses += 1
        else:
            consecutive_losses = 0

        round_label = f"R{reentry_round}" if reentry_round > 0 else ""
        logger.info(
            "  %s%s: entry=%.2f@%s exit=%.2f@%s pnl=%.2f (%.1f%%) [%s]",
            round_label + " " if round_label else "",
            trade.symbol, trade.entry_price, trade.entry_time,
            trade.exit_price, trade.exit_time,
            trade.pnl, trade.pnl_pct, trade.exit_reason,
        )

        # For re-entry: earliest scan time = exit time + cooldown.
        # Exit reasons like MARKET_CLOSE mean no re-entry possible.
        if trade.exit_reason == "MARKET_CLOSE":
            break

        # --- Smart re-entry gates ---
        # Gate 1: If this trade hit HARD_STOP, market is rejecting momentum — stop.
        if trade.exit_reason == "HARD_STOP":
            break

        # Gate 2: Only re-enter if this trade was profitable.
        if trade.pnl < 0:
            break

        # Gate 3: No re-entry after 12:30 — not enough time for big moves.
        if trade.exit_time >= "12:30":
            break

        # Gate 4: Only re-enter after PROFIT_LOCK — confirms strong momentum day.
        if trade.exit_reason != "PROFIT_LOCK":
            break

        earliest_time = _add_candles_to_time(trade.exit_time, REENTRY_COOLDOWN_CANDLES)

    return summary


# === REPORT ===

def print_report(trades: list[Trade], summaries: list[DaySummary]) -> None:
    print("\n" + "=" * 70)
    print("BACKTEST REPORT — 1 YEAR")
    print("=" * 70)

    if not trades:
        print("No trades executed.")
        return

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in trades)
    win_rate = len(wins) / len(trades) * 100

    bearish_days = sum(1 for s in summaries if "not bullish" in s.skipped_reason)
    print(f"\nPeriod: {summaries[0].date} to {summaries[-1].date}")
    print(f"Trading days: {len(summaries)}")
    print(f"Days market was bullish: {len(summaries) - bearish_days}")
    print(f"Days filtered out (bearish): {bearish_days}")
    print(f"Days with trades: {sum(1 for s in summaries if s.trades)}")
    print(f"Days skipped (other): {sum(1 for s in summaries if s.skipped_reason and 'not bullish' not in s.skipped_reason)}")
    print(f"\nTotal trades: {len(trades)}")
    print(f"Winners: {len(wins)} ({win_rate:.1f}%)")
    print(f"Losers: {len(losses)} ({100 - win_rate:.1f}%)")
    print(f"\nTotal P&L: Rs.{total_pnl:+,.2f}")
    print(f"Capital: {CAPITAL:,.0f} x {LEVERAGE}x = {CAPITAL * LEVERAGE:,.0f}")
    print(f"Return on capital: {total_pnl / CAPITAL * 100:+.2f}%")

    if wins:
        avg_win = sum(t.pnl for t in wins) / len(wins)
        best = max(wins, key=lambda t: t.pnl)
        print(f"\nAvg win: +Rs.{avg_win:,.2f}")
        print(f"Best trade: {best.symbol} on {best.date} — +Rs.{best.pnl:,.2f} ({best.pnl_pct:+.1f}%)")

    if losses:
        avg_loss = sum(t.pnl for t in losses) / len(losses)
        worst = min(losses, key=lambda t: t.pnl)
        print(f"Avg loss: Rs.{avg_loss:,.2f}")
        print(f"Worst trade: {worst.symbol} on {worst.date} — Rs.{worst.pnl:,.2f} ({worst.pnl_pct:+.1f}%)")

    # Exit reasons.
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    print("\nExit reasons:")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        pnl_for_reason = sum(t.pnl for t in trades if t.exit_reason == reason)
        print(f"  {reason}: {count} trades, P&L Rs.{pnl_for_reason:+,.2f}")

    # Monthly breakdown.
    monthly: dict[str, list[Trade]] = {}
    for t in trades:
        m = t.date[:7]
        monthly.setdefault(m, []).append(t)
    print("\nMonthly breakdown:")
    print(f"  {'Month':<10} {'Trades':>6} {'Wins':>5} {'Win%':>6} {'P&L':>12}")
    for m in sorted(monthly):
        mt = monthly[m]
        mw = [t for t in mt if t.pnl > 0]
        mp = sum(t.pnl for t in mt)
        wr = len(mw) / len(mt) * 100 if mt else 0
        print(f"  {m:<10} {len(mt):>6} {len(mw):>5} {wr:>5.1f}% Rs.{mp:>+11,.2f}")

    # Max drawdown (cumulative, not single-day).
    cumulative = peak = max_dd = 0.0
    dd_start_date = dd_end_date = ""
    current_dd_start = ""
    for t in trades:
        cumulative += t.pnl
        if cumulative > peak:
            peak = cumulative
            current_dd_start = t.date
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
            dd_start_date = current_dd_start
            dd_end_date = t.date
    print(f"\nMax drawdown: Rs.{max_dd:,.2f} (from {dd_start_date} to {dd_end_date})")
    print("  This is a CUMULATIVE drawdown — losses accumulated over multiple days,")
    print("  not a single-day loss. It measures peak-to-trough of your equity curve.")

    # Daily P&L breakdown — worst and best days.
    daily_pnl: dict[str, float] = {}
    daily_trades_count: dict[str, int] = {}
    for t in trades:
        daily_pnl[t.date] = daily_pnl.get(t.date, 0) + t.pnl
        daily_trades_count[t.date] = daily_trades_count.get(t.date, 0) + 1

    worst_days = sorted(daily_pnl.items(), key=lambda x: x[1])[:5]
    best_days = sorted(daily_pnl.items(), key=lambda x: x[1], reverse=True)[:5]

    print("\nWorst 5 days (single-day P&L):")
    for date, pnl in worst_days:
        n = daily_trades_count[date]
        pct_of_capital = pnl / CAPITAL * 100
        print(f"  {date}: Rs.{pnl:>+11,.2f} ({pct_of_capital:+.1f}% of capital, {n} trades)")

    print("\nBest 5 days (single-day P&L):")
    for date, pnl in best_days:
        n = daily_trades_count[date]
        pct_of_capital = pnl / CAPITAL * 100
        print(f"  {date}: Rs.{pnl:>+11,.2f} ({pct_of_capital:+.1f}% of capital, {n} trades)")

    # Profit factor.
    gross_profit = sum(t.pnl for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl for t in losses)) if losses else 1
    print(f"\nProfit factor: {gross_profit / gross_loss:.2f}")

    # Top 10 trades.
    print("\nTop 10 winning trades:")
    for t in sorted(trades, key=lambda x: x.pnl, reverse=True)[:10]:
        print(f"  {t.date} {t.symbol:<16} {t.pnl_pct:>+6.1f}% Rs.{t.pnl:>+10,.2f} [{t.exit_reason}]")

    print("\nTop 10 losing trades:")
    for t in sorted(trades, key=lambda x: x.pnl)[:10]:
        print(f"  {t.date} {t.symbol:<16} {t.pnl_pct:>+6.1f}% Rs.{t.pnl:>+10,.2f} [{t.exit_reason}]")

    # === RE-ENTRY ANALYSIS ===
    round0 = [t for t in trades if t.reentry_round == 0]
    reentry = [t for t in trades if t.reentry_round > 0]

    if reentry:
        print("\n" + "=" * 70)
        print("RE-ENTRY ANALYSIS")
        print("=" * 70)

        r0_wins = [t for t in round0 if t.pnl > 0]
        re_wins = [t for t in reentry if t.pnl > 0]
        r0_pnl = sum(t.pnl for t in round0)
        re_pnl = sum(t.pnl for t in reentry)

        print(f"\nRound 0:  {len(round0)} trades, {len(r0_wins)} wins ({len(r0_wins)/len(round0)*100:.1f}%), P&L Rs.{r0_pnl:+,.2f}")
        print(f"Re-entry: {len(reentry)} trades, {len(re_wins)} wins ({len(re_wins)/len(reentry)*100:.1f}%), P&L Rs.{re_pnl:+,.2f}")

        # Analyze: when round 0 loses, what happens to re-entry?
        days_with_reentry: dict[str, list[Trade]] = {}
        for t in trades:
            days_with_reentry.setdefault(t.date, []).append(t)

        r0_loss_then_re = []  # Days where round 0 lost and re-entry happened.
        r0_win_then_re = []   # Days where round 0 won and re-entry happened.
        for date, day_trades in days_with_reentry.items():
            if len(day_trades) < 2:
                continue
            r0_trade = [t for t in day_trades if t.reentry_round == 0][0]
            re_trades = [t for t in day_trades if t.reentry_round > 0]
            if r0_trade.pnl < 0:
                r0_loss_then_re.append((date, r0_trade, re_trades))
            else:
                r0_win_then_re.append((date, r0_trade, re_trades))

        if r0_loss_then_re:
            re_after_loss_pnl = sum(t.pnl for _, _, rts in r0_loss_then_re for t in rts)
            re_after_loss_wins = sum(1 for _, _, rts in r0_loss_then_re for t in rts if t.pnl > 0)
            re_after_loss_total = sum(len(rts) for _, _, rts in r0_loss_then_re)
            print("\nWhen Round 0 LOSES → re-entry trades:")
            print(f"  {re_after_loss_total} trades, {re_after_loss_wins} wins "
                  f"({re_after_loss_wins/re_after_loss_total*100:.1f}%), "
                  f"P&L Rs.{re_after_loss_pnl:+,.2f}")

        if r0_win_then_re:
            re_after_win_pnl = sum(t.pnl for _, _, rts in r0_win_then_re for t in rts)
            re_after_win_wins = sum(1 for _, _, rts in r0_win_then_re for t in rts if t.pnl > 0)
            re_after_win_total = sum(len(rts) for _, _, rts in r0_win_then_re)
            print("\nWhen Round 0 WINS → re-entry trades:")
            print(f"  {re_after_win_total} trades, {re_after_win_wins} wins "
                  f"({re_after_win_wins/re_after_win_total*100:.1f}%), "
                  f"P&L Rs.{re_after_win_pnl:+,.2f}")

        # Analyze re-entry by entry time.
        print("\nRe-entry trades by entry time:")
        re_by_time: dict[str, list[Trade]] = {}
        for t in reentry:
            hour = t.entry_time[:2]
            re_by_time.setdefault(hour, []).append(t)
        for hour in sorted(re_by_time):
            ht = re_by_time[hour]
            hw = [t for t in ht if t.pnl > 0]
            hp = sum(t.pnl for t in ht)
            print(f"  {hour}:xx — {len(ht)} trades, {len(hw)} wins ({len(hw)/len(ht)*100:.0f}%), P&L Rs.{hp:+,.2f}")

        # Analyze re-entry by round 0 exit reason.
        print("\nRe-entry outcome by Round 0 exit reason:")
        for date, r0_trade, re_trades in sorted(r0_loss_then_re + r0_win_then_re, key=lambda x: x[1].pnl):
            re_pnl_day = sum(t.pnl for t in re_trades)
            re_results = ", ".join(f"{t.symbol} {t.pnl_pct:+.1f}%" for t in re_trades)
            print(f"  {date}: R0={r0_trade.symbol} {r0_trade.pnl_pct:+.1f}% [{r0_trade.exit_reason}] "
                  f"→ Re-entry: {re_results} (Rs.{re_pnl_day:+,.2f})")

    # === LOSS PATTERN ANALYSIS (Round 0 only) ===
    print("\n" + "=" * 70)
    print("LOSS PATTERN ANALYSIS — What predicts bad days?")
    print("=" * 70)

    r0_only = [t for t in trades if t.reentry_round == 0]
    r0_wins = [t for t in r0_only if t.pnl > 0]
    r0_losses = [t for t in r0_only if t.pnl <= 0]

    if r0_only and r0_wins and r0_losses:
        # 1. Nifty close %
        avg_nifty_w = sum(t.nifty_pct for t in r0_wins) / len(r0_wins)
        avg_nifty_l = sum(t.nifty_pct for t in r0_losses) / len(r0_losses)
        print(f"\nNifty close %:  Wins avg {avg_nifty_w:+.2f}%  |  Losses avg {avg_nifty_l:+.2f}%")

        # 2. VIX change
        avg_vix_w = sum(t.vix_pct for t in r0_wins) / len(r0_wins)
        avg_vix_l = sum(t.vix_pct for t in r0_losses) / len(r0_losses)
        print(f"VIX change:     Wins avg {avg_vix_w:+.2f}%  |  Losses avg {avg_vix_l:+.2f}%")

        # 3. Nifty intraday low
        avg_nlo_w = sum(t.nifty_lo_pct for t in r0_wins) / len(r0_wins)
        avg_nlo_l = sum(t.nifty_lo_pct for t in r0_losses) / len(r0_losses)
        print(f"Nifty low:      Wins avg {avg_nlo_w:+.2f}%  |  Losses avg {avg_nlo_l:+.2f}%")

        # 4. Entry gain
        avg_gain_w = sum(t.entry_gain_pct for t in r0_wins) / len(r0_wins)
        avg_gain_l = sum(t.entry_gain_pct for t in r0_losses) / len(r0_losses)
        print(f"Entry gain:     Wins avg {avg_gain_w:+.2f}%  |  Losses avg {avg_gain_l:+.2f}%")

        # 5. Score 3 vs 4
        print("\nMarket score:")
        for s in [3, 4]:
            st = [t for t in r0_only if t.market_score == s]
            if st:
                sw = sum(1 for t in st if t.pnl > 0)
                print(f"  Score={s}/4: {len(st)} trades, {sw} wins ({sw/len(st)*100:.0f}%)")

        # 6. Entry time
        print("\nEntry time:")
        for hour in ["10", "11", "12", "13"]:
            ht = [t for t in r0_only if t.entry_time[:2] == hour]
            if ht:
                hw = sum(1 for t in ht if t.pnl > 0)
                print(f"  {hour}:xx — {len(ht)} trades, {hw} wins ({hw/len(ht)*100:.0f}%)")

        # 7. Entry gain buckets
        print("\nEntry gain buckets:")
        for lo, hi, label in [(5.0, 6.0, "5-6%"), (6.0, 7.0, "6-7%"), (7.0, 8.0, "7-8%"), (8.0, 10.0, "8-10%")]:
            bt = [t for t in r0_only if lo <= t.entry_gain_pct < hi]
            if bt:
                bw = sum(1 for t in bt if t.pnl > 0)
                print(f"  Gain {label}: {len(bt)} trades, {bw} wins ({bw/len(bt)*100:.0f}%)")

        # 8. Nifty direction
        print("\nNifty direction:")
        nifty_neg = [t for t in r0_only if t.nifty_pct < 0]
        nifty_pos = [t for t in r0_only if t.nifty_pct >= 0]
        if nifty_neg:
            nw = sum(1 for t in nifty_neg if t.pnl > 0)
            print(f"  Nifty NEGATIVE: {len(nifty_neg)} trades, {nw} wins ({nw/len(nifty_neg)*100:.0f}%)")
        if nifty_pos:
            nw = sum(1 for t in nifty_pos if t.pnl > 0)
            print(f"  Nifty POSITIVE: {len(nifty_pos)} trades, {nw} wins ({nw/len(nifty_pos)*100:.0f}%)")

        # 9. Nifty intraday volatility thresholds
        print("\nNifty intraday dip:")
        for threshold in [-0.3, -0.5, -0.8, -1.0]:
            vol = [t for t in r0_only if t.nifty_lo_pct < threshold]
            calm = [t for t in r0_only if t.nifty_lo_pct >= threshold]
            if vol and calm:
                vw = sum(1 for t in vol if t.pnl > 0)
                cw = sum(1 for t in calm if t.pnl > 0)
                print(f"  Dipped below {threshold}%: {len(vol)} trades, {vw} wins ({vw/len(vol)*100:.0f}%) "
                      f"| Above: {len(calm)} trades, {cw} wins ({cw/len(calm)*100:.0f}%)")

        # 10. VIX thresholds
        print("\nVIX change:")
        for threshold in [0.0, 1.0, 2.0, 3.0]:
            vup = [t for t in r0_only if t.vix_pct > threshold]
            vdn = [t for t in r0_only if t.vix_pct <= threshold]
            if vup and vdn:
                vw = sum(1 for t in vup if t.pnl > 0)
                dw = sum(1 for t in vdn if t.pnl > 0)
                print(f"  VIX > +{threshold}%: {len(vup)} trades, {vw} wins ({vw/len(vup)*100:.0f}%) "
                      f"| VIX <= +{threshold}%: {len(vdn)} trades, {dw} wins ({dw/len(vdn)*100:.0f}%)")

        # 11. Combined: Nifty negative + VIX rising
        print("\nCombined filters:")
        bad = [t for t in r0_only if t.nifty_pct < 0 and t.vix_pct > 0]
        good = [t for t in r0_only if not (t.nifty_pct < 0 and t.vix_pct > 0)]
        if bad:
            bw = sum(1 for t in bad if t.pnl > 0)
            print(f"  Nifty<0 AND VIX rising: {len(bad)} trades, {bw} wins ({bw/len(bad)*100:.0f}%)")
        if good:
            gw = sum(1 for t in good if t.pnl > 0)
            print(f"  Otherwise:              {len(good)} trades, {gw} wins ({gw/len(good)*100:.0f}%)")

        # 12. All losing trades with context
        print(f"\nAll {len(r0_losses)} losing R0 trades:")
        print(f"  {'Date':<12} {'Nifty':>6} {'VIX':>6} {'Sc':>2} "
              f"{'Symbol':<16} {'Entry':>5} {'Gain':>5} {'PnL':>6} {'Reason':<15} {'NifLo':>6}")
        for t in sorted(r0_losses, key=lambda x: x.pnl_pct):
            print(f"  {t.date:<10} {t.nifty_pct:>+5.1f}% {t.vix_pct:>+5.1f}% {t.market_score:>2} "
                  f"{t.symbol:<16} {t.entry_time:>5} {t.entry_gain_pct:>+4.1f}% {t.pnl_pct:>+5.1f}% "
                  f"{t.exit_reason:<15} {t.nifty_lo_pct:>+5.1f}%")

    # === DAY-BY-DAY TRADE LOG ===
    print("\n" + "=" * 110)
    print("DAY-BY-DAY TRADE LOG")
    print("=" * 110)
    print(f"  {'Date':<12} {'Rd':>2} {'Symbol':<18} {'Entry':>6} {'Exit':>6} "
          f"{'EntryPr':>9} {'ExitPr':>9} {'Qty':>5} {'PnL%':>6} {'PnL(Rs)':>12} "
          f"{'DayPnL':>12} {'Reason':<15}")
    print(f"  {'-'*106}")

    # Group trades by date.
    trades_by_date: dict[str, list[Trade]] = {}
    for t in trades:
        trades_by_date.setdefault(t.date, []).append(t)

    cumulative = 0.0
    for date in sorted(trades_by_date):
        day_trades = trades_by_date[date]
        day_pnl = sum(t.pnl for t in day_trades)
        cumulative += day_pnl

        for i, t in enumerate(day_trades):
            # Show day P&L only on the last trade of the day.
            day_col = f"Rs.{day_pnl:>+10,.2f}" if i == len(day_trades) - 1 else ""
            marker = "✅" if t.pnl > 0 else "❌"
            print(f"{marker} {t.date:<10} R{t.reentry_round:>1} {t.symbol:<18} {t.entry_time:>6} {t.exit_time:>6} "
                  f"{t.entry_price:>9.2f} {t.exit_price:>9.2f} {t.quantity:>5} {t.pnl_pct:>+5.1f}% "
                  f"Rs.{t.pnl:>+10,.2f} {day_col:<12} {t.exit_reason}")

        # Print day separator with cumulative.
        if len(day_trades) > 1 or day_pnl < 0:
            cum_marker = "📈" if cumulative > 0 else "📉"
            print(f"  {'':>10} {'':>3} {'':>18} {'':>6} {'':>6} {'':>9} {'':>9} {'':>5} {'':>6} "
                  f"{'':>12} {cum_marker} Cum: Rs.{cumulative:>+10,.2f}")

    print(f"\n  TOTAL: {len(trades)} trades | Cumulative P&L: Rs.{cumulative:+,.2f}")


# === MAIN ===

def run_legacy_backtest() -> tuple[list[Trade], list[DaySummary]]:
    """Deprecated pre-production-equivalence runner kept for result comparison."""
    settings = load_settings()
    logger.info("Logging in to Angel One...")
    broker = AngelOneClient.login(
        api_key=settings.api_key, client_id=settings.client_id,
        pin=settings.pin, totp_secret=settings.totp_secret,
    )
    logger.info("Login successful.")

    logger.info("Loading NSE equity scrip master...")
    nse_stocks = load_nse_equity_tokens(broker)
    logger.info("Loaded %d stocks.", len(nse_stocks))

    focused = nse_stocks[:STOCK_COUNT]
    trading_days = get_trading_days(broker, LOOKBACK_DAYS)
    logger.info("Trading days: %d (from %s to %s)", len(trading_days), trading_days[0], trading_days[-1])

    logger.info("Prefetching daily candles for %d stocks...", len(focused))
    daily_candles = prefetch_daily_candles(broker, focused, LOOKBACK_DAYS)
    logger.info("Daily candles ready for %d stocks.", len(daily_candles))

    # Ensure Nifty 50 constituents are in the daily candle set (for breadth calc).
    missing_n50 = [
        {"symbol": sym + "-EQ", "token": tok}
        for sym, tok in NIFTY_50_CONSTITUENTS.items()
        if tok not in daily_candles
    ]
    if missing_n50:
        logger.info("Fetching %d missing Nifty 50 constituent candles...", len(missing_n50))
        extra = prefetch_daily_candles(broker, missing_n50, LOOKBACK_DAYS)
        daily_candles.update(extra)

    # Fetch Nifty index and VIX daily candles for market filter.
    logger.info("Fetching Nifty and VIX daily candles...")
    now = datetime.now(IST)
    start = (now - timedelta(days=LOOKBACK_DAYS + 10)).strftime("%Y-%m-%d 09:00")
    end = now.strftime("%Y-%m-%d %H:%M")

    min_idx_candles = max(200, LOOKBACK_DAYS // 2)
    nifty_candles_cached = read_cache("daily", NIFTY_50_TOKEN)
    if nifty_candles_cached and len(nifty_candles_cached) > min_idx_candles:
        nifty_candles = nifty_candles_cached
    else:
        nifty_candles = broker.get_candle_data("NSE", NIFTY_50_TOKEN, "ONE_DAY", start, end) or []
        if nifty_candles:
            write_cache("daily", NIFTY_50_TOKEN, nifty_candles)
        time.sleep(CANDLE_DELAY)

    vix_candles_cached = read_cache("daily", INDIA_VIX_TOKEN)
    if vix_candles_cached and len(vix_candles_cached) > min_idx_candles:
        vix_candles = vix_candles_cached
    else:
        vix_candles = broker.get_candle_data("NSE", INDIA_VIX_TOKEN, "ONE_DAY", start, end) or []
        if vix_candles:
            write_cache("daily", INDIA_VIX_TOKEN, vix_candles)

    logger.info("Nifty candles: %d, VIX candles: %d", len(nifty_candles), len(vix_candles))

    all_trades: list[Trade] = []
    day_summaries: list[DaySummary] = []

    for i, day_str in enumerate(trading_days):
        logger.info("[%d/%d] %s", i + 1, len(trading_days), day_str)
        summary = backtest_one_day(
            broker, focused, daily_candles, nifty_candles, vix_candles, day_str,
        )
        day_summaries.append(summary)
        all_trades.extend(summary.trades)

        if summary.trades:
            for t in summary.trades:
                logger.info(
                    "  %s: entry=%.2f@%s exit=%.2f@%s pnl=%+.2f (%.1f%%) [%s]",
                    t.symbol, t.entry_price, t.entry_time,
                    t.exit_price, t.exit_time, t.pnl, t.pnl_pct, t.exit_reason,
                )
        elif summary.skipped_reason:
            logger.info("  SKIP: %s", summary.skipped_reason)

    print_report(all_trades, day_summaries)
    return all_trades, day_summaries


def run_backtest():
    """Run the point-in-time engine that mirrors the production workflow."""
    from production_backtest import run_production_backtest

    return run_production_backtest()


if __name__ == "__main__":
    run_backtest()

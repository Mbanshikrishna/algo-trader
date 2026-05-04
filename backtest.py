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
    INTRADAY_LOCK_FLOOR_PCT,
)
from utils.atr import compute_atr_from_candles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("backtest")

IST = ZoneInfo("Asia/Kolkata")
CACHE_DIR = Path("data/backtest_cache")

# --- Backtest parameters (mirror production) ---
CAPITAL = 100_000
LEVERAGE = 5.0
TOP_N = 2
MAX_CONSECUTIVE_LOSSES = 2
LOOKBACK_DAYS = 365
STOCK_COUNT = 500  # Top N stocks from scrip master to scan.
MIN_RANGE_POSITION = 0.85
MIN_VOLUME_RATIO = 1.2
CANDLE_DELAY = 0.35


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

    for i, stock in enumerate(stocks):
        token = stock["token"]
        cached = read_cache("daily", token)
        if cached and len(cached) > 200:
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
    """Historical 4-factor market bullishness check using daily candles.

    Returns (is_bullish, reason_string).
    """
    # Find Nifty candle for this day and previous day.
    nifty_today = nifty_prev = None
    for j, c in enumerate(nifty_candles):
        if c[0][:10] == day_str:
            nifty_today = c
            if j > 0:
                nifty_prev = nifty_candles[j - 1]
            break

    if not nifty_today or not nifty_prev:
        return False, "No Nifty data"

    nifty_prev_close = float(nifty_prev[4])
    nifty_open = float(nifty_today[1])
    nifty_close = float(nifty_today[4])

    # Factor 1: Index Direction — Nifty close > prev close.
    nifty_pct = ((nifty_close - nifty_prev_close) / nifty_prev_close) * 100
    index_pass = nifty_pct > 0

    # Factor 2: Market Breadth — count advancing/declining Nifty 50 constituents.
    advancing = declining = 0
    for token in NIFTY_50_CONSTITUENTS.values():
        candles = daily_candles.get(token)
        if not candles:
            continue
        for j, c in enumerate(candles):
            if c[0][:10] == day_str and j > 0:
                prev_c = float(candles[j - 1][4])
                today_c = float(c[4])
                if prev_c > 0:
                    pct = ((today_c - prev_c) / prev_c) * 100
                    if pct > 0:
                        advancing += 1
                    elif pct < 0:
                        declining += 1
                break

    breadth_ratio = advancing / declining if declining > 0 else 99.0
    breadth_pass = breadth_ratio > 1.2

    # Factor 3: Intraday Strength — Nifty close > open (proxy for LTP > open).
    strength_pass = nifty_close > nifty_open

    # Factor 4: Volatility Filter — VIX change < +5%.
    vix_pct = 0.0
    for j, c in enumerate(vix_candles):
        if c[0][:10] == day_str and j > 0:
            vix_prev = float(vix_candles[j - 1][4])
            vix_today = float(c[4])
            if vix_prev > 0:
                vix_pct = ((vix_today - vix_prev) / vix_prev) * 100
            break
    volatility_pass = vix_pct < 5.0

    score = sum([index_pass, breadth_pass, strength_pass, volatility_pass])
    bullish = score >= 2

    reason = (
        f"Nifty={nifty_pct:+.1f}%({'Y' if index_pass else 'N'}) "
        f"Breadth={advancing}A/{declining}D={breadth_ratio:.1f}({'Y' if breadth_pass else 'N'}) "
        f"Strength={'Y' if strength_pass else 'N'} "
        f"VIX={vix_pct:+.1f}%({'Y' if volatility_pass else 'N'}) "
        f"Score={score}/4 → {'BULLISH' if bullish else 'BEARISH'}"
    )
    return bullish, reason


# === SCANNING ===

def scan_for_candidates(
    daily_candles: dict[str, list[list]], stocks: list[dict], day_str: str,
) -> list[dict]:
    """Find stocks that gained 5-10% on the given day (in-memory scan)."""
    token_map = {s["token"]: s for s in stocks}
    candidates = []

    for token, candles in daily_candles.items():
        day_candle = prev_candle = None
        for j, c in enumerate(candles):
            if c[0][:10] == day_str:
                day_candle = c
                if j > 0:
                    prev_candle = candles[j - 1]
                break

        if not day_candle or not prev_candle:
            continue

        prev_close = float(prev_candle[4])
        high = float(day_candle[2])
        low = float(day_candle[3])
        close = float(day_candle[4])
        volume = int(float(day_candle[5]))

        if prev_close <= 0 or volume < MIN_VOLUME:
            continue
        if close < MIN_PRICE or close > MAX_PRICE:
            continue

        high_gain = ((high - prev_close) / prev_close) * 100
        if high_gain < MIN_GAIN_PCT:
            continue

        info = token_map.get(token, {})
        candidates.append({
            "symbol": info.get("symbol", ""),
            "token": token,
            "prev_close": prev_close,
            "open": float(day_candle[1]),
            "high": high, "low": low, "close": close,
            "volume": volume,
            "high_gain_pct": round(high_gain, 2),
            "close_gain_pct": round(((close - prev_close) / prev_close) * 100, 2),
        })

    candidates.sort(key=lambda x: x["high_gain_pct"], reverse=True)
    return candidates


# === ENTRY VALIDATION & TRADE SIMULATION ===

def try_entry_and_simulate(
    candles_5m: list[list], symbol: str, token: str,
    prev_close: float, day_str: str, leveraged_capital: float, already_entered: int,
) -> Trade | None:
    """Try entry at scan times, validate, and simulate the trade."""
    candle_by_time: dict[str, int] = {}
    for idx, c in enumerate(candles_5m):
        candle_by_time[c[0][11:16]] = idx

    scan_times = [
        "10:00", "10:15", "10:30", "10:45",
        "11:00", "11:15", "11:30", "11:45", "12:00", "12:15", "12:30",
    ]

    for scan_time in scan_times:
        idx = candle_by_time.get(scan_time)
        if idx is None or idx < 3:
            continue

        candle = candles_5m[idx]
        ltp = float(candle[4])
        vol = float(candle[5])

        # Check 1: Gain in 5-10% range.
        gain_pct = ((ltp - prev_close) / prev_close) * 100
        if gain_pct < MIN_GAIN_PCT or gain_pct > MAX_GAIN_PCT:
            continue

        # Check 2: Range position >= 0.85.
        running_high = max(float(c[2]) for c in candles_5m[:idx + 1])
        running_low = min(float(c[3]) for c in candles_5m[:idx + 1])
        day_range = running_high - running_low
        range_pos = (ltp - running_low) / day_range if day_range > 0 else 1.0
        if range_pos < MIN_RANGE_POSITION:
            continue

        # Check 3: Micro breakout.
        if ltp <= float(candles_5m[idx - 1][2]):
            continue

        # Check 4: Volume confirmation.
        prior_vols = [float(candles_5m[j][5]) for j in range(max(0, idx - 5), idx)]
        avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0
        vol_ratio = vol / avg_vol if avg_vol > 0 else 99.0
        if vol_ratio < MIN_VOLUME_RATIO:
            continue

        # Compute ATR.
        atr_candles = candles_5m[max(0, idx - 12):idx + 1]
        atr = compute_atr_from_candles(atr_candles)
        if atr <= 0:
            atr = ltp * 0.005

        # Position sizing.
        cap = leveraged_capital * 0.5 if already_entered == 0 else leveraged_capital / (already_entered + 1)
        qty = int(cap // ltp)
        if qty <= 0:
            continue

        return simulate_trade(candles_5m, idx, ltp, prev_close, atr, qty, symbol, token, day_str, scan_time)

    return None


def simulate_trade(
    candles_5m: list[list], entry_idx: int, entry_price: float,
    prev_close: float, atr: float, qty: int,
    symbol: str, token: str, day_str: str, entry_time: str,
) -> Trade:
    """Simulate ATR trailing stop on 5-min candles."""
    trade = Trade(
        date=day_str, symbol=symbol, token=token,
        entry_price=entry_price, entry_time=entry_time,
        prev_close=prev_close, atr=atr, quantity=qty,
    )

    # Initial stops.
    atr_dist = atr * INITIAL_ATR_MULT
    pct_dist = entry_price * HARD_MAX_LOSS_PCT
    min_dist = entry_price * MIN_STOP_DISTANCE_PCT
    distance = max(min(atr_dist, pct_dist), min_dist)
    stop_loss = round(entry_price - distance, 2)

    atr_hard = entry_price - (atr * INITIAL_ATR_MULT)
    pct_hard = entry_price * (1 - HARD_MAX_LOSS_PCT)
    hard_stop = round(max(atr_hard, pct_hard), 2)

    highest = entry_price
    profit_locked = False

    for i in range(entry_idx + 1, len(candles_5m)):
        candle = candles_5m[i]
        candle_time = candle[0][11:16]
        c_high = float(candle[2])
        c_low = float(candle[3])
        c_close = float(candle[4])

        if candle_time >= "15:05":
            trade.exit_price = c_close
            trade.exit_time = candle_time
            trade.exit_reason = "MARKET_CLOSE"
            trade.highest_price = highest
            break

        if c_high > highest:
            highest = c_high

        # Compute trailing stop.
        intraday_gain = (highest - prev_close) / prev_close if prev_close > 0 else 0

        if intraday_gain >= INTRADAY_LOCK_THRESHOLD:
            profit_locked = True
            lock_floor = prev_close * (1 + INTRADAY_LOCK_FLOOR_PCT)
            lock_trail = highest * (1 - INTRADAY_LOCK_TRAIL_PCT)
            new_stop = round(max(lock_floor, lock_trail), 2)
        else:
            profit_pct = (highest - entry_price) / entry_price
            mult = get_trail_mult(profit_pct, candle_time)
            trail_d = max(atr * mult, highest * MIN_STOP_DISTANCE_PCT)
            new_stop = round(highest - trail_d, 2)

        if new_stop > stop_loss:
            stop_loss = new_stop

        if c_low <= hard_stop:
            trade.exit_price = hard_stop
            trade.exit_time = candle_time
            trade.exit_reason = "HARD_STOP"
            trade.highest_price = highest
            break
        if c_low <= stop_loss:
            trade.exit_price = stop_loss
            trade.exit_time = candle_time
            trade.exit_reason = "PROFIT_LOCK" if profit_locked else "TRAILING_STOP"
            trade.highest_price = highest
            break
    else:
        last = candles_5m[-1]
        trade.exit_price = float(last[4])
        trade.exit_time = last[0][11:16]
        trade.exit_reason = "MARKET_CLOSE"
        trade.highest_price = highest

    trade.pnl = round((trade.exit_price - entry_price) * qty, 2)
    trade.pnl_pct = round(((trade.exit_price - entry_price) / entry_price) * 100, 2)
    return trade


def get_trail_mult(profit_pct: float, candle_time: str = "") -> float:
    """Look up ATR multiplier, with late-session tightening after 14:30."""
    late_session = candle_time >= "14:30" if candle_time else False
    for threshold, mult in TRAIL_TIERS:
        if profit_pct >= threshold:
            if late_session:
                mult = max(mult - 0.5, 0.5)
            return mult
    base = TRAIL_TIERS[-1][1]
    if late_session:
        base = max(base - 0.5, 0.5)
    return base


# === DAY SIMULATION ===

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

    candidates = scan_for_candidates(daily_candles, stocks, day_str)
    if not candidates:
        summary.skipped_reason = "No 5-10% gainers"
        return summary

    logger.info("  %d candidates in 5-10%% range", len(candidates))
    leveraged = CAPITAL * LEVERAGE
    entered = 0
    consecutive_losses = 0

    for cand in candidates[:TOP_N * 3]:
        if entered >= TOP_N or consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            break

        candles_5m = fetch_intraday_candles(broker, cand["token"], day_str)
        if not candles_5m or len(candles_5m) < 5:
            continue

        trade = try_entry_and_simulate(
            candles_5m, cand["symbol"], cand["token"],
            cand["prev_close"], day_str, leveraged, entered,
        )
        if trade:
            summary.trades.append(trade)
            summary.daily_pnl += trade.pnl
            entered += 1
            if trade.pnl < 0:
                consecutive_losses += 1
            else:
                consecutive_losses = 0

    if not summary.trades:
        summary.skipped_reason = "No valid entries after validation"
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

    # Max drawdown.
    cumulative = peak = max_dd = 0.0
    for t in trades:
        cumulative += t.pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    print(f"\nMax drawdown: Rs.{max_dd:,.2f}")

    # Profit factor.
    gross_profit = sum(t.pnl for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl for t in losses)) if losses else 1
    print(f"Profit factor: {gross_profit / gross_loss:.2f}")

    # Top 10 trades.
    print("\nTop 10 winning trades:")
    for t in sorted(trades, key=lambda x: x.pnl, reverse=True)[:10]:
        print(f"  {t.date} {t.symbol:<16} {t.pnl_pct:>+6.1f}% Rs.{t.pnl:>+10,.2f} [{t.exit_reason}]")

    print("\nTop 10 losing trades:")
    for t in sorted(trades, key=lambda x: x.pnl)[:10]:
        print(f"  {t.date} {t.symbol:<16} {t.pnl_pct:>+6.1f}% Rs.{t.pnl:>+10,.2f} [{t.exit_reason}]")


# === MAIN ===

def run_backtest():
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

    nifty_candles_cached = read_cache("daily", NIFTY_50_TOKEN)
    if nifty_candles_cached and len(nifty_candles_cached) > 200:
        nifty_candles = nifty_candles_cached
    else:
        nifty_candles = broker.get_candle_data("NSE", NIFTY_50_TOKEN, "ONE_DAY", start, end) or []
        if nifty_candles:
            write_cache("daily", NIFTY_50_TOKEN, nifty_candles)
        time.sleep(CANDLE_DELAY)

    vix_candles_cached = read_cache("daily", INDIA_VIX_TOKEN)
    if vix_candles_cached and len(vix_candles_cached) > 200:
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


if __name__ == "__main__":
    run_backtest()


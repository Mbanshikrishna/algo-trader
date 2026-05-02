"""Backtest the full trading workflow using Angel One historical data.

Simulates: scan → validate → enter → ATR trailing stop → exit.
Fetches daily candles once for all stocks, then 5-min candles only for candidates.
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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

CAPITAL = 100_000
LEVERAGE = 5.0
TOP_N = 2
EXIT_TIME = "15:15"
LOOKBACK_DAYS = 25
MAX_CONSECUTIVE_LOSSES = 2
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


def run_backtest():
    """Main backtest entry point."""
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

    trading_days = get_trading_days(broker, LOOKBACK_DAYS)
    logger.info("Trading days to backtest: %d", len(trading_days))

    all_trades: list[Trade] = []
    day_summaries: list[DaySummary] = []

    # Use a focused watchlist to avoid burning API quota on 2500 stocks.
    # Filter to stocks in the ₹50-5000 range from scrip master.
    # Further narrow by fetching candles only for a manageable subset.
    focused = nse_stocks[:500]  # Top 500 by scrip master order (large/mid caps first).
    logger.info("Prefetching daily candles for %d stocks (~3 min)...", len(focused))
    daily_candles = prefetch_daily_candles(broker, focused, LOOKBACK_DAYS)
    logger.info("Prefetched daily candles for %d stocks.", len(daily_candles))

    for day_str in trading_days:
        logger.info("\n" + "=" * 60)
        logger.info("BACKTESTING: %s", day_str)
        summary = backtest_one_day(broker, nse_stocks, daily_candles, day_str)
        day_summaries.append(summary)
        all_trades.extend(summary.trades)

        if summary.skipped_reason:
            logger.info("  SKIPPED: %s", summary.skipped_reason)
        else:
            for t in summary.trades:
                logger.info(
                    "  %s: entry=%.2f@%s exit=%.2f@%s pnl=%+.2f (%.1f%%) [%s]",
                    t.symbol, t.entry_price, t.entry_time,
                    t.exit_price, t.exit_time, t.pnl, t.pnl_pct, t.exit_reason,
                )
            logger.info("  Day P&L: %+.2f", summary.daily_pnl)

    print_report(all_trades, day_summaries)


def get_trading_days(broker: AngelOneClient, lookback: int) -> list[str]:
    """Get recent trading days by fetching Nifty daily candles."""
    now = datetime.now(IST)
    start = now - timedelta(days=lookback + 10)
    candles = broker.get_candle_data(
        "NSE", "99926000", "ONE_DAY",
        start.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d %H:%M"),
    )
    today_str = now.strftime("%Y-%m-%d")
    days = []
    for c in candles:
        d = c[0][:10]
        if d != today_str:
            days.append(d)
    return days[-lookback:]


def backtest_one_day(
    broker: AngelOneClient, nse_stocks: list[dict],
    daily_candles: dict[str, list[list]], day_str: str,
) -> DaySummary:
    """Simulate one full trading day."""
    summary = DaySummary(date=day_str)

    # Phase 1: Find candidates from prefetched daily data (no API calls).
    candidates = scan_for_candidates(daily_candles, nse_stocks, day_str)
    if not candidates:
        summary.skipped_reason = "No 5-10% gainers found"
        return summary

    logger.info("  Found %d candidates in 5-10%% range", len(candidates))

    # Phase 2: Fetch 5-min candles for top candidates and simulate.
    leveraged = CAPITAL * LEVERAGE
    entered = 0
    consecutive_losses = 0

    for cand in candidates[:TOP_N * 3]:  # Try up to 3x TOP_N for fallbacks.
        if entered >= TOP_N:
            break
        if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            break

        symbol = cand["symbol"]
        token = cand["token"]
        prev_close = cand["prev_close"]
        day_high = cand["high"]
        day_low = cand["low"]

        # Fetch 5-min candles for this stock on this day.
        candles_5m = fetch_intraday_candles(broker, token, day_str)
        if not candles_5m or len(candles_5m) < 5:
            logger.info("  %s: insufficient 5-min candles (%d)", symbol, len(candles_5m))
            continue

        # Try entry at scan times within 9:45-12:30.
        trade = try_entry_and_simulate(
            candles_5m, symbol, token, prev_close, day_high, day_low, day_str, leveraged, entered,
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


def prefetch_daily_candles(
    broker: AngelOneClient, nse_stocks: list[dict], lookback: int,
) -> dict[str, list[list]]:
    """Prefetch daily candles for all stocks once. Returns {token: candles}.

    This is the most API-intensive step. We fetch sequentially with rate
    limiting. ~2500 stocks at 0.35s each = ~15 minutes.
    """
    now = datetime.now(IST)
    start = (now - timedelta(days=lookback + 10)).strftime("%Y-%m-%d 09:00")
    end = now.strftime("%Y-%m-%d %H:%M")

    all_candles: dict[str, list[list]] = {}
    total = len(nse_stocks)

    for i, stock in enumerate(nse_stocks):
        token = stock["token"]
        try:
            candles = broker.get_candle_data("NSE", token, "ONE_DAY", start, end)
            if candles:
                all_candles[token] = candles
        except Exception:
            pass

        time.sleep(CANDLE_DELAY)

        if (i + 1) % 100 == 0 or i + 1 == total:
            logger.info("  Prefetched daily candles: %d/%d (found data for %d)",
                        i + 1, total, len(all_candles))

    return all_candles


def scan_for_candidates(
    daily_candles: dict[str, list[list]],
    nse_stocks: list[dict],
    day_str: str,
) -> list[dict]:
    """Find stocks that gained 5-10% on the given day using prefetched data."""
    token_map = {s["token"]: s for s in nse_stocks}
    candidates = []

    for token, candles in daily_candles.items():
        day_candle = None
        prev_candle = None
        for j, c in enumerate(candles):
            if c[0][:10] == day_str:
                day_candle = c
                if j > 0:
                    prev_candle = candles[j - 1]
                break

        if not day_candle or not prev_candle:
            continue

        prev_close = float(prev_candle[4])
        open_p = float(day_candle[1])
        high = float(day_candle[2])
        low = float(day_candle[3])
        close = float(day_candle[4])
        volume = int(float(day_candle[5]))

        if prev_close <= 0:
            continue

        high_gain = ((high - prev_close) / prev_close) * 100
        if high_gain < MIN_GAIN_PCT:
            continue
        if close < MIN_PRICE or close > MAX_PRICE:
            continue
        if volume < MIN_VOLUME:
            continue

        stock_info = token_map.get(token, {})
        candidates.append({
            "symbol": stock_info.get("symbol", ""),
            "token": token,
            "prev_close": prev_close,
            "open": open_p,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "high_gain_pct": round(high_gain, 2),
            "close_gain_pct": round(((close - prev_close) / prev_close) * 100, 2),
        })

    candidates.sort(key=lambda x: x["high_gain_pct"], reverse=True)
    return candidates


def fetch_intraday_candles(
    broker: AngelOneClient, token: str, day_str: str,
) -> list[list]:
    """Fetch 5-minute candles for a stock on a specific day."""
    start = day_str + " 09:15"
    end = day_str + " 15:30"
    try:
        time.sleep(CANDLE_DELAY)
        candles = broker.get_candle_data("NSE", token, "FIVE_MINUTE", start, end)
        return candles or []
    except Exception as exc:
        logger.warning("  Failed to fetch 5m candles for %s: %s", token, exc)
        return []


def try_entry_and_simulate(
    candles_5m: list[list],
    symbol: str, token: str,
    prev_close: float, day_high: float, day_low: float,
    day_str: str, leveraged_capital: float, already_entered: int,
) -> Trade | None:
    """Try to enter at various scan times and simulate the trade."""

    # Build time-indexed candle map.
    candle_by_time: dict[str, int] = {}
    for idx, c in enumerate(candles_5m):
        t = c[0][11:16]  # "HH:MM"
        candle_by_time[t] = idx

    scan_times = [
        "09:45", "10:00", "10:15", "10:30", "10:45",
        "11:00", "11:15", "11:30", "11:45", "12:00", "12:15", "12:30",
    ]

    for scan_time in scan_times:
        idx = candle_by_time.get(scan_time)
        if idx is None or idx < 3:
            continue

        candle = candles_5m[idx]
        ltp = float(candle[4])  # Close of the 5-min candle = simulated LTP.
        high = float(candle[2])
        low = float(candle[3])
        vol = float(candle[5])

        # --- Validation check 1: Gain in 5-10% range at this moment ---
        gain_pct = ((ltp - prev_close) / prev_close) * 100
        if gain_pct < MIN_GAIN_PCT or gain_pct > MAX_GAIN_PCT:
            continue

        # --- Validation check 2: Range position >= 0.85 ---
        # Compute running high/low up to this candle.
        running_high = max(float(c[2]) for c in candles_5m[:idx + 1])
        running_low = min(float(c[3]) for c in candles_5m[:idx + 1])
        day_range = running_high - running_low
        if day_range > 0:
            range_pos = (ltp - running_low) / day_range
        else:
            range_pos = 1.0
        if range_pos < MIN_RANGE_POSITION:
            continue

        # --- Validation check 3: Micro breakout ---
        prev_candle_high = float(candles_5m[idx - 1][2])
        if ltp <= prev_candle_high:
            continue

        # --- Validation check 4: Volume confirmation ---
        prior_vols = [float(candles_5m[j][5]) for j in range(max(0, idx - 5), idx)]
        avg_prior_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0
        if avg_prior_vol > 0:
            vol_ratio = vol / avg_prior_vol
        else:
            vol_ratio = 99.0
        if vol_ratio < MIN_VOLUME_RATIO:
            continue

        # --- All checks passed — compute ATR and enter ---
        atr_candles = candles_5m[max(0, idx - 12):idx + 1]
        atr = compute_atr_from_candles(atr_candles)
        if atr <= 0:
            atr = ltp * 0.005  # Fallback.

        # Position sizing.
        if already_entered == 0:
            capital_per_stock = leveraged_capital * 0.5
        else:
            capital_per_stock = leveraged_capital / (already_entered + 1)
        qty = int(capital_per_stock // ltp)
        if qty <= 0:
            continue

        # Simulate the trade from this candle forward.
        trade = simulate_trade(
            candles_5m, idx, ltp, prev_close, atr, qty, symbol, token, day_str, scan_time,
        )
        return trade

    return None


def simulate_trade(
    candles_5m: list[list],
    entry_idx: int,
    entry_price: float,
    prev_close: float,
    atr: float,
    qty: int,
    symbol: str,
    token: str,
    day_str: str,
    entry_time: str,
) -> Trade:
    """Simulate ATR trailing stop on 5-min candles from entry to exit."""

    trade = Trade(
        date=day_str, symbol=symbol, token=token,
        entry_price=entry_price, entry_time=entry_time,
        prev_close=prev_close, atr=atr, quantity=qty,
    )

    # Compute initial stops.
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

    # Walk through candles after entry.
    for i in range(entry_idx + 1, len(candles_5m)):
        candle = candles_5m[i]
        candle_time = candle[0][11:16]
        c_high = float(candle[2])
        c_low = float(candle[3])
        c_close = float(candle[4])

        # Force exit at 15:15.
        if candle_time >= "15:15":
            trade.exit_price = c_close
            trade.exit_time = candle_time
            trade.exit_reason = "MARKET_CLOSE"
            trade.highest_price = highest
            break

        # Update highest.
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
            mult = _get_trail_mult(profit_pct)
            trail_d = max(atr * mult, highest * MIN_STOP_DISTANCE_PCT)
            new_stop = round(highest - trail_d, 2)

        if new_stop > stop_loss:
            stop_loss = new_stop

        # Check exit on candle low.
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
        # Reached end of candles without exit — use last close.
        last = candles_5m[-1]
        trade.exit_price = float(last[4])
        trade.exit_time = last[0][11:16]
        trade.exit_reason = "MARKET_CLOSE"
        trade.highest_price = highest

    trade.pnl = round((trade.exit_price - entry_price) * qty, 2)
    trade.pnl_pct = round(((trade.exit_price - entry_price) / entry_price) * 100, 2)
    return trade


def _get_trail_mult(profit_pct: float) -> float:
    """Mirror the production trail tier logic (without time-based adjustment)."""
    for threshold, mult in TRAIL_TIERS:
        if profit_pct >= threshold:
            return mult
    return TRAIL_TIERS[-1][1]


def print_report(trades: list[Trade], summaries: list[DaySummary]) -> None:
    """Print the full backtest report."""
    print("\n" + "=" * 70)
    print("BACKTEST REPORT")
    print("=" * 70)

    if not trades:
        print("No trades executed.")
        return

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0

    print(f"\nPeriod: {summaries[0].date} to {summaries[-1].date}")
    print(f"Trading days: {len(summaries)}")
    print(f"Days with trades: {sum(1 for s in summaries if s.trades)}")
    print(f"Days skipped: {sum(1 for s in summaries if s.skipped_reason)}")
    print(f"\nTotal trades: {len(trades)}")
    print(f"Winners: {len(wins)} ({win_rate:.1f}%)")
    print(f"Losers: {len(losses)} ({100 - win_rate:.1f}%)")
    print(f"\nTotal P&L: {total_pnl:+,.2f}")
    print(f"Capital: {CAPITAL:,.0f} x {LEVERAGE}x = {CAPITAL * LEVERAGE:,.0f}")
    print(f"Return: {total_pnl / CAPITAL * 100:+.2f}% on capital")

    if wins:
        avg_win = sum(t.pnl for t in wins) / len(wins)
        best = max(wins, key=lambda t: t.pnl)
        print(f"\nAvg win: +{avg_win:,.2f}")
        print(f"Best trade: {best.symbol} on {best.date} — +{best.pnl:,.2f} ({best.pnl_pct:+.1f}%)")

    if losses:
        avg_loss = sum(t.pnl for t in losses) / len(losses)
        worst = min(losses, key=lambda t: t.pnl)
        print(f"Avg loss: {avg_loss:,.2f}")
        print(f"Worst trade: {worst.symbol} on {worst.date} — {worst.pnl:,.2f} ({worst.pnl_pct:+.1f}%)")

    # Exit reason breakdown.
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    print("\nExit reasons:")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count}")

    # Daily P&L.
    print("\nDaily P&L:")
    cumulative = 0.0
    for s in summaries:
        if s.trades:
            cumulative += s.daily_pnl
            syms = ", ".join(f"{t.symbol}({t.pnl_pct:+.1f}%)" for t in s.trades)
            print(f"  {s.date}: {s.daily_pnl:+8,.2f}  cum={cumulative:+10,.2f}  [{syms}]")
        elif s.skipped_reason:
            print(f"  {s.date}: {'skip':>8s}  cum={cumulative:+10,.2f}  [{s.skipped_reason}]")

    # Trade log.
    print("\nDetailed trade log:")
    print(f"{'Date':<12} {'Symbol':<16} {'Entry':>8} {'Exit':>8} {'P&L':>10} {'P&L%':>7} "
          f"{'High':>8} {'ATR':>7} {'Reason':<16} {'EntryT':<6} {'ExitT':<6}")
    print("-" * 115)
    for t in trades:
        print(f"{t.date:<12} {t.symbol:<16} {t.entry_price:>8.2f} {t.exit_price:>8.2f} "
              f"{t.pnl:>+10.2f} {t.pnl_pct:>+6.1f}% {t.highest_price:>8.2f} "
              f"{t.atr:>7.4f} {t.exit_reason:<16} {t.entry_time:<6} {t.exit_time:<6}")

    # Max drawdown.
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cumulative += t.pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    print(f"\nMax drawdown: {max_dd:,.2f}")


if __name__ == "__main__":
    run_backtest()

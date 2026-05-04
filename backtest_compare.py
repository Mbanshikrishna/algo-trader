"""Compare backtest results: with market filter vs without."""

from __future__ import annotations
import time
from datetime import timedelta
from backtest import *


def run_both():
    settings = load_settings()
    logger.info("Logging in...")
    broker = AngelOneClient.login(
        api_key=settings.api_key, client_id=settings.client_id,
        pin=settings.pin, totp_secret=settings.totp_secret,
    )

    nse_stocks = load_nse_equity_tokens(broker)
    focused = nse_stocks[:STOCK_COUNT]
    trading_days = get_trading_days(broker, LOOKBACK_DAYS)

    logger.info("Prefetching daily candles...")
    daily_candles = prefetch_daily_candles(broker, focused, LOOKBACK_DAYS)

    # Nifty 50 constituents for breadth.
    missing_n50 = [
        {"symbol": sym + "-EQ", "token": tok}
        for sym, tok in NIFTY_50_CONSTITUENTS.items()
        if tok not in daily_candles
    ]
    if missing_n50:
        extra = prefetch_daily_candles(broker, missing_n50, LOOKBACK_DAYS)
        daily_candles.update(extra)

    # Nifty + VIX.
    now = datetime.now(IST)
    start = (now - timedelta(days=LOOKBACK_DAYS + 10)).strftime("%Y-%m-%d 09:00")
    end = now.strftime("%Y-%m-%d %H:%M")

    nifty_candles = read_cache("daily", NIFTY_50_TOKEN)
    if not nifty_candles or len(nifty_candles) < 200:
        nifty_candles = broker.get_candle_data("NSE", NIFTY_50_TOKEN, "ONE_DAY", start, end) or []
        if nifty_candles:
            write_cache("daily", NIFTY_50_TOKEN, nifty_candles)
        time.sleep(0.35)

    vix_candles = read_cache("daily", INDIA_VIX_TOKEN)
    if not vix_candles or len(vix_candles) < 200:
        vix_candles = broker.get_candle_data("NSE", INDIA_VIX_TOKEN, "ONE_DAY", start, end) or []
        if vix_candles:
            write_cache("daily", INDIA_VIX_TOKEN, vix_candles)

    logger.info("Running both backtests over %d days...", len(trading_days))

    # === RUN 1: WITH FILTER ===
    filtered_trades: list[Trade] = []
    filtered_days: list[DaySummary] = []
    for day_str in trading_days:
        summary = backtest_one_day(broker, focused, daily_candles, nifty_candles, vix_candles, day_str)
        filtered_days.append(summary)
        filtered_trades.extend(summary.trades)

    # === RUN 2: WITHOUT FILTER (bypass market check) ===
    raw_trades: list[Trade] = []
    raw_days: list[DaySummary] = []
    for day_str in trading_days:
        summary = backtest_one_day_no_filter(broker, focused, daily_candles, day_str)
        raw_days.append(summary)
        raw_trades.extend(summary.trades)

    # === PRINT COMPARISON ===
    print_comparison(filtered_trades, filtered_days, raw_trades, raw_days, trading_days)


def backtest_one_day_no_filter(
    broker: AngelOneClient, stocks: list[dict],
    daily_candles: dict[str, list[list]], day_str: str,
) -> DaySummary:
    """Same as backtest_one_day but skips the market bullishness check."""
    summary = DaySummary(date=day_str)
    candidates = scan_for_candidates(daily_candles, stocks, day_str)
    if not candidates:
        summary.skipped_reason = "No 5-10% gainers"
        return summary

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


def _month_stats(trades: list[Trade]) -> dict:
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_w = sum(t.pnl for t in wins)
    gross_l = abs(sum(t.pnl for t in losses))
    net = sum(t.pnl for t in trades)
    wr = len(wins) / len(trades) * 100 if trades else 0
    pf = gross_w / gross_l if gross_l > 0 else 0.0

    cum = peak = mdd = 0.0
    for t in trades:
        cum += t.pnl
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > mdd:
            mdd = dd

    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "wr": wr, "gross_w": gross_w, "gross_l": gross_l,
        "net": net, "pf": pf, "mdd": mdd,
    }


def _exit_stats(trades: list[Trade]) -> dict[str, dict]:
    reasons: dict[str, dict] = {}
    for t in trades:
        if t.exit_reason not in reasons:
            reasons[t.exit_reason] = {"count": 0, "pnl": 0.0, "wins": 0}
        reasons[t.exit_reason]["count"] += 1
        reasons[t.exit_reason]["pnl"] += t.pnl
        if t.pnl > 0:
            reasons[t.exit_reason]["wins"] += 1
    return reasons


def print_comparison(
    f_trades: list[Trade], f_days: list[DaySummary],
    r_trades: list[Trade], r_days: list[DaySummary],
    trading_days: list[str],
):
    # Group by month.
    f_monthly: dict[str, list[Trade]] = {}
    r_monthly: dict[str, list[Trade]] = {}
    for t in f_trades:
        f_monthly.setdefault(t.date[:7], []).append(t)
    for t in r_trades:
        r_monthly.setdefault(t.date[:7], []).append(t)

    all_months = sorted(set(list(f_monthly.keys()) + list(r_monthly.keys())))

    # Also count bearish/bullish days per month.
    f_bearish_m: dict[str, int] = {}
    for s in f_days:
        m = s.date[:7]
        if s.skipped_reason and "not bullish" in s.skipped_reason:
            f_bearish_m[m] = f_bearish_m.get(m, 0) + 1

    print()
    print("=" * 150)
    print("SIDE-BY-SIDE COMPARISON: WITH MARKET FILTER vs WITHOUT MARKET FILTER")
    print("=" * 150)

    # Monthly table.
    print()
    print(f"{'':>10}  {'--- WITH MARKET FILTER ---':^55}  {'--- WITHOUT MARKET FILTER ---':^55}  {'FILTER':>10}")
    print(
        f"{'Month':<10}  "
        f"{'#Tr':>4} {'Win':>4} {'Loss':>4} {'Win%':>6} {'Net P&L':>12} {'PF':>5} {'MaxDD':>9}  "
        f"{'#Tr':>4} {'Win':>4} {'Loss':>4} {'Win%':>6} {'Net P&L':>12} {'PF':>5} {'MaxDD':>9}  "
        f"{'Saved':>10}"
    )
    print("-" * 150)

    f_cum = r_cum = 0.0
    for m in all_months:
        ft = f_monthly.get(m, [])
        rt = r_monthly.get(m, [])
        fs = _month_stats(ft)
        rs = _month_stats(rt)
        f_cum += fs["net"]
        r_cum += rs["net"]
        saved = fs["net"] - rs["net"]
        bear = f_bearish_m.get(m, 0)

        print(
            f"{m:<10}  "
            f"{fs['trades']:>4} {fs['wins']:>4} {fs['losses']:>4} {fs['wr']:>5.1f}% {fs['net']:>+11,.0f} {fs['pf']:>5.2f} {fs['mdd']:>8,.0f}  "
            f"{rs['trades']:>4} {rs['wins']:>4} {rs['losses']:>4} {rs['wr']:>5.1f}% {rs['net']:>+11,.0f} {rs['pf']:>5.2f} {rs['mdd']:>8,.0f}  "
            f"{saved:>+9,.0f}"
        )

    print("-" * 150)
    fs = _month_stats(f_trades)
    rs = _month_stats(r_trades)
    saved_total = fs["net"] - rs["net"]
    print(
        f"{'TOTAL':<10}  "
        f"{fs['trades']:>4} {fs['wins']:>4} {fs['losses']:>4} {fs['wr']:>5.1f}% {fs['net']:>+11,.0f} {fs['pf']:>5.2f} {fs['mdd']:>8,.0f}  "
        f"{rs['trades']:>4} {rs['wins']:>4} {rs['losses']:>4} {rs['wr']:>5.1f}% {rs['net']:>+11,.0f} {rs['pf']:>5.2f} {rs['mdd']:>8,.0f}  "
        f"{saved_total:>+9,.0f}"
    )

    # Exit reason comparison.
    print()
    print("EXIT REASON COMPARISON")
    print("-" * 110)
    f_exits = _exit_stats(f_trades)
    r_exits = _exit_stats(r_trades)
    all_reasons = sorted(set(list(f_exits.keys()) + list(r_exits.keys())))

    print(
        f"  {'Reason':<18}  "
        f"{'--- WITH FILTER ---':^35}  "
        f"{'--- WITHOUT FILTER ---':^35}"
    )
    print(
        f"  {'':18}  "
        f"{'#Tr':>5} {'Win%':>6} {'P&L':>12} {'Avg':>9}  "
        f"{'#Tr':>5} {'Win%':>6} {'P&L':>12} {'Avg':>9}"
    )
    print(f"  {'-'*18}  {'-'*35}  {'-'*35}")
    for r in all_reasons:
        fd = f_exits.get(r, {"count": 0, "pnl": 0, "wins": 0})
        rd = r_exits.get(r, {"count": 0, "pnl": 0, "wins": 0})
        f_wr = fd["wins"] / fd["count"] * 100 if fd["count"] else 0
        r_wr = rd["wins"] / rd["count"] * 100 if rd["count"] else 0
        f_avg = fd["pnl"] / fd["count"] if fd["count"] else 0
        r_avg = rd["pnl"] / rd["count"] if rd["count"] else 0
        print(
            f"  {r:<18}  "
            f"{fd['count']:>5} {f_wr:>5.1f}% {fd['pnl']:>+11,.0f} {f_avg:>+8,.0f}  "
            f"{rd['count']:>5} {r_wr:>5.1f}% {rd['pnl']:>+11,.0f} {r_avg:>+8,.0f}"
        )

    # Cumulative equity curve comparison.
    print()
    print("CUMULATIVE P&L BY MONTH")
    print("-" * 70)
    print(f"  {'Month':<10} {'With Filter':>14} {'Without Filter':>14} {'Difference':>14}")
    print(f"  {'-'*10} {'-'*14} {'-'*14} {'-'*14}")
    f_cum = r_cum = 0.0
    for m in all_months:
        ft = f_monthly.get(m, [])
        rt = r_monthly.get(m, [])
        f_cum += sum(t.pnl for t in ft)
        r_cum += sum(t.pnl for t in rt)
        print(f"  {m:<10} {f_cum:>+13,.0f} {r_cum:>+13,.0f} {f_cum - r_cum:>+13,.0f}")

    # Summary comparison.
    f_bear = sum(1 for s in f_days if s.skipped_reason and "not bullish" in s.skipped_reason)
    f_trade_days = sum(1 for s in f_days if s.trades)
    r_trade_days = sum(1 for s in r_days if s.trades)

    f_losing_months = sum(1 for m in all_months if sum(t.pnl for t in f_monthly.get(m, [])) < 0)
    r_losing_months = sum(1 for m in all_months if sum(t.pnl for t in r_monthly.get(m, [])) < 0)

    print()
    print("=" * 70)
    print("SUMMARY COMPARISON")
    print("=" * 70)
    print(f"  {'Metric':<30} {'With Filter':>18} {'Without Filter':>18}")
    print(f"  {'-'*30} {'-'*18} {'-'*18}")
    print(f"  {'Trading days':<30} {len(trading_days):>18} {len(trading_days):>18}")
    print(f"  {'Bearish days (sat out)':<30} {f_bear:>18} {'0':>18}")
    print(f"  {'Days with trades':<30} {f_trade_days:>18} {r_trade_days:>18}")
    print(f"  {'Total trades':<30} {fs['trades']:>18} {rs['trades']:>18}")
    print(f"  {'Win rate':<30} {fs['wr']:>17.1f}% {rs['wr']:>17.1f}%")
    f_pnl_str = f"Rs.{fs['net']:>+,.0f}"
    r_pnl_str = f"Rs.{rs['net']:>+,.0f}"
    f_dd_str = f"Rs.{fs['mdd']:>,.0f}"
    r_dd_str = f"Rs.{rs['mdd']:>,.0f}"
    print(f"  {'Total P&L':<30} {f_pnl_str:>18} {r_pnl_str:>18}")
    print(f"  {'Return on Rs.1L':<30} {fs['net']/CAPITAL*100:>+17.1f}% {rs['net']/CAPITAL*100:>+17.1f}%")
    print(f"  {'Avg monthly return':<30} {fs['net']/CAPITAL*100/12:>+17.1f}% {rs['net']/CAPITAL*100/12:>+17.1f}%")
    print(f"  {'Profit factor':<30} {fs['pf']:>18.2f} {rs['pf']:>18.2f}")
    print(f"  {'Max drawdown':<30} {f_dd_str:>18} {r_dd_str:>18}")
    print(f"  {'Losing months':<30} {f_losing_months:>18} {r_losing_months:>18}")
    print(f"  {'Hard stops hit':<30} {f_exits.get('HARD_STOP', {}).get('count', 0):>18} {r_exits.get('HARD_STOP', {}).get('count', 0):>18}")
    print(f"  {'Profit locks triggered':<30} {f_exits.get('PROFIT_LOCK', {}).get('count', 0):>18} {r_exits.get('PROFIT_LOCK', {}).get('count', 0):>18}")

    # Verdict.
    print()
    pnl_diff = fs["net"] - rs["net"]
    dd_diff = rs["mdd"] - fs["mdd"]
    wr_diff = fs["wr"] - rs["wr"]
    hard_diff = r_exits.get("HARD_STOP", {}).get("count", 0) - f_exits.get("HARD_STOP", {}).get("count", 0)
    print("VERDICT")
    print("-" * 70)
    if pnl_diff < 0:
        print(f"  Filter reduced P&L by Rs.{abs(pnl_diff):,.0f} ({abs(pnl_diff)/rs['net']*100:.0f}% less)")
    else:
        print(f"  Filter increased P&L by Rs.{pnl_diff:,.0f}")
    print(f"  Filter reduced max drawdown by Rs.{dd_diff:,.0f} ({dd_diff/rs['mdd']*100:.0f}% smaller)")
    print(f"  Filter improved win rate by {wr_diff:+.1f}%")
    print(f"  Filter avoided {hard_diff} hard stop losses (Rs.{hard_diff * 7300:,.0f} saved)")
    print(f"  Filter cut trades from {rs['trades']} to {fs['trades']} ({(1 - fs['trades']/rs['trades'])*100:.0f}% fewer)")


if __name__ == "__main__":
    run_both()

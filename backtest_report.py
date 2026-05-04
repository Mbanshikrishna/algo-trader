"""Run the full backtest and print detailed monthly numbers."""

from __future__ import annotations
import time
from datetime import timedelta
from backtest import *


def main():
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

    logger.info("Running backtest over %d trading days...", len(trading_days))

    all_trades: list[Trade] = []
    day_summaries: list[DaySummary] = []

    for day_str in trading_days:
        summary = backtest_one_day(broker, focused, daily_candles, nifty_candles, vix_candles, day_str)
        day_summaries.append(summary)
        all_trades.extend(summary.trades)

    # === BUILD MONTHLY DATA ===
    monthly: dict[str, dict] = {}
    for s in day_summaries:
        m = s.date[:7]
        if m not in monthly:
            monthly[m] = {
                "trades": [], "bullish_days": 0, "bearish_days": 0,
                "no_entry_days": 0, "trade_days": 0, "total_days": 0,
            }
        monthly[m]["total_days"] += 1
        if s.skipped_reason and "not bullish" in s.skipped_reason:
            monthly[m]["bearish_days"] += 1
        elif s.trades:
            monthly[m]["trade_days"] += 1
            monthly[m]["bullish_days"] += 1
            monthly[m]["trades"].extend(s.trades)
        else:
            monthly[m]["bullish_days"] += 1
            monthly[m]["no_entry_days"] += 1

    # === PRINT REPORT ===
    hdr = (
        f"{'Month':<10} {'Days':>5} {'Bull':>5} {'Bear':>5} "
        f"{'Traded':>6} {'#Tr':>4} {'Win':>4} {'Loss':>4} "
        f"{'Win%':>6} {'Gross Win':>12} {'Gross Loss':>12} "
        f"{'Net P&L':>12} {'PF':>5} {'MaxDD':>10}"
    )

    print()
    print("=" * 130)
    print("MONTHLY PERFORMANCE — FULL WORKFLOW BACKTEST")
    print("=" * 130)
    print(hdr)
    print("-" * 130)

    cumulative = 0.0
    for m in sorted(monthly):
        d = monthly[m]
        t = d["trades"]
        wins = [x for x in t if x.pnl > 0]
        losses_t = [x for x in t if x.pnl <= 0]
        gross_w = sum(x.pnl for x in wins)
        gross_l = abs(sum(x.pnl for x in losses_t))
        net = sum(x.pnl for x in t)
        wr = len(wins) / len(t) * 100 if t else 0
        pf = gross_w / gross_l if gross_l > 0 else 0.0

        # Monthly max drawdown.
        cum = peak = mdd = 0.0
        for x in t:
            cum += x.pnl
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > mdd:
                mdd = dd

        cumulative += net
        print(
            f"{m:<10} {d['total_days']:>5} {d['bullish_days']:>5} {d['bearish_days']:>5} "
            f"{d['trade_days']:>6} {len(t):>4} {len(wins):>4} {len(losses_t):>4} "
            f"{wr:>5.1f}% {gross_w:>+11,.0f} {gross_l:>11,.0f} "
            f"{net:>+11,.0f} {pf:>5.2f} {mdd:>9,.0f}"
        )

    print("-" * 130)

    # Totals.
    total_w = [x for x in all_trades if x.pnl > 0]
    total_l = [x for x in all_trades if x.pnl <= 0]
    total_gw = sum(x.pnl for x in total_w)
    total_gl = abs(sum(x.pnl for x in total_l))
    total_net = sum(x.pnl for x in all_trades)
    total_wr = len(total_w) / len(all_trades) * 100 if all_trades else 0
    total_pf = total_gw / total_gl if total_gl > 0 else 0.0

    bull_d = sum(d["bullish_days"] for d in monthly.values())
    bear_d = sum(d["bearish_days"] for d in monthly.values())
    trade_d = sum(d["trade_days"] for d in monthly.values())
    total_d = sum(d["total_days"] for d in monthly.values())

    cum = peak = mdd = 0.0
    for x in all_trades:
        cum += x.pnl
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > mdd:
            mdd = dd

    print(
        f"{'TOTAL':<10} {total_d:>5} {bull_d:>5} {bear_d:>5} "
        f"{trade_d:>6} {len(all_trades):>4} {len(total_w):>4} {len(total_l):>4} "
        f"{total_wr:>5.1f}% {total_gw:>+11,.0f} {total_gl:>11,.0f} "
        f"{total_net:>+11,.0f} {total_pf:>5.2f} {mdd:>9,.0f}"
    )

    # === EXIT REASONS ===
    print()
    print("EXIT REASON BREAKDOWN")
    print("-" * 80)
    reasons: dict[str, dict] = {}
    for t in all_trades:
        if t.exit_reason not in reasons:
            reasons[t.exit_reason] = {"count": 0, "pnl": 0.0, "wins": 0}
        reasons[t.exit_reason]["count"] += 1
        reasons[t.exit_reason]["pnl"] += t.pnl
        if t.pnl > 0:
            reasons[t.exit_reason]["wins"] += 1

    print(f"  {'Reason':<18} {'Trades':>6} {'Win%':>7} {'Total P&L':>14} {'Avg P&L':>10}")
    print(f"  {'-'*18} {'-'*6} {'-'*7} {'-'*14} {'-'*10}")
    for r in sorted(reasons, key=lambda x: -reasons[x]["count"]):
        d = reasons[r]
        wr = d["wins"] / d["count"] * 100
        avg = d["pnl"] / d["count"]
        print(f"  {r:<18} {d['count']:>6} {wr:>6.1f}% {d['pnl']:>+13,.0f} {avg:>+9,.0f}")

    # === CUMULATIVE EQUITY CURVE ===
    print()
    print("CUMULATIVE P&L BY MONTH")
    print("-" * 50)
    cum = 0.0
    for m in sorted(monthly):
        net = sum(x.pnl for x in monthly[m]["trades"])
        cum += net
        bar = "+" * max(0, int(cum / 5000)) if cum > 0 else "-" * max(0, int(-cum / 5000))
        print(f"  {m}  {cum:>+12,.0f}  {bar}")

    # === TOP TRADES ===
    print()
    print("TOP 5 WINNING TRADES")
    print("-" * 100)
    print(f"  {'Date':<12} {'Symbol':<16} {'Entry':>8} {'Exit':>8} {'P&L':>10} {'P&L%':>7} {'Reason':<16}")
    for t in sorted(all_trades, key=lambda x: x.pnl, reverse=True)[:5]:
        print(f"  {t.date:<12} {t.symbol:<16} {t.entry_price:>8.2f} {t.exit_price:>8.2f} "
              f"{t.pnl:>+10,.0f} {t.pnl_pct:>+6.1f}% {t.exit_reason:<16}")

    print()
    print("TOP 5 LOSING TRADES")
    print("-" * 100)
    print(f"  {'Date':<12} {'Symbol':<16} {'Entry':>8} {'Exit':>8} {'P&L':>10} {'P&L%':>7} {'Reason':<16}")
    for t in sorted(all_trades, key=lambda x: x.pnl)[:5]:
        print(f"  {t.date:<12} {t.symbol:<16} {t.entry_price:>8.2f} {t.exit_price:>8.2f} "
              f"{t.pnl:>+10,.0f} {t.pnl_pct:>+6.1f}% {t.exit_reason:<16}")

    # === SUMMARY ===
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Capital:            Rs.{CAPITAL:>10,}")
    print(f"  Leverage:           {LEVERAGE}x")
    print(f"  Buying power:       Rs.{CAPITAL * LEVERAGE:>10,.0f}")
    print(f"  Period:             {trading_days[0]} to {trading_days[-1]}")
    print(f"  Trading days:       {total_d}")
    print(f"  Bullish days:       {bull_d} ({bull_d/total_d*100:.0f}%)")
    print(f"  Bearish (sat out):  {bear_d} ({bear_d/total_d*100:.0f}%)")
    print(f"  Total trades:       {len(all_trades)}")
    print(f"  Win rate:           {total_wr:.1f}%")
    print(f"  Total P&L:          Rs.{total_net:>+12,.0f}")
    print(f"  Return on capital:  {total_net / CAPITAL * 100:>+.1f}%")
    print(f"  Avg monthly return: {total_net / CAPITAL * 100 / 12:>+.1f}%")
    print(f"  Profit factor:      {total_pf:.2f}")
    print(f"  Max drawdown:       Rs.{mdd:>10,.0f}")
    print(f"  Best month:         {max(sorted(monthly), key=lambda m: sum(x.pnl for x in monthly[m]['trades']))}")
    best_m = max(sorted(monthly), key=lambda m: sum(x.pnl for x in monthly[m]["trades"]))
    worst_m = min(sorted(monthly), key=lambda m: sum(x.pnl for x in monthly[m]["trades"]))
    print(f"  Best month P&L:     Rs.{sum(x.pnl for x in monthly[best_m]['trades']):>+12,.0f} ({best_m})")
    print(f"  Worst month P&L:    Rs.{sum(x.pnl for x in monthly[worst_m]['trades']):>+12,.0f} ({worst_m})")
    winning_months = sum(1 for m in monthly if sum(x.pnl for x in monthly[m]["trades"]) > 0)
    print(f"  Winning months:     {winning_months}/{len(monthly)}")


if __name__ == "__main__":
    main()

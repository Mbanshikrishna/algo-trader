"""Backtest the entry strategy using Angel One historical candle data.

Architecture (minimizes API calls):
1. Prefetch daily candles for ALL NSE stocks once (covers full period).
2. For each day: in-memory scan for 5-10% gainers, score, pick top candidates.
3. Fetch 5-min intraday candles ONLY for candidates (few per day).
4. Validate entry + simulate trailing stop on intraday candles.
5. Report P&L, win rate, drawdown.
"""

from __future__ import annotations

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from broker.angelone_client import AngelOneClient
from config.settings import load_settings
from strategy.market_scanner import (
    MIN_GAIN_PCT, MAX_GAIN_PCT, MIN_PRICE, MAX_PRICE, MIN_VOLUME,
    NIFTY_50_TOKEN, load_nse_equity_tokens,
)
from monitor.position_tracker import (
    DEFAULT_TRAIL_PCT, TIGHT_TRAIL_PCT, TIGHT_TRAIL_PROFIT_THRESHOLD,
    LOCK_PROFIT_THRESHOLD, MAX_LOSS_PCT,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backtest")

IST = ZoneInfo("Asia/Kolkata")

CAPITAL = 100_000
LEVERAGE = 5.0
TOP_N = 2
SCAN_START_HOUR, SCAN_START_MIN = 9, 45   # Scan window start.
SCAN_END_HOUR, SCAN_END_MIN = 12, 30     # Scan window end.
EXIT_HOUR, EXIT_MIN = 15, 15
LOOKBACK_DAYS = 20


@dataclass
class TradeResult:
    date: str
    symbol: str
    token: str
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_pct: float
    exit_reason: str
    entry_time: str = ""
    exit_time: str = ""
    highest_price: float = 0.0
    gain_at_entry: float = 0.0


@dataclass
class DayResult:
    date: str
    trades: list[TradeResult] = field(default_factory=list)
    candidates_found: int = 0
    validated: int = 0
    skipped_reason: str = ""


def login_broker() -> AngelOneClient:
    settings = load_settings()
    logger.info("Logging in to Angel One...")
    client = AngelOneClient.login(
        api_key=settings.api_key, client_id=settings.client_id,
        pin=settings.pin, totp_secret=settings.totp_secret,
    )
    logger.info("Login successful.")
    return client


def _fetch_with_retry(client, exchange, token, interval, start_str, end_str, retries=5):
    """Fetch candle data with exponential backoff on rate limit errors."""
    for attempt in range(retries):
        try:
            return client.get_candle_data(exchange, token, interval, start_str, end_str)
        except Exception as e:
            if "403" in str(e) or "rate" in str(e).lower():
                wait = 10 * (attempt + 1)
                logger.info("  Rate limited, waiting %ds (attempt %d/%d)...", wait, attempt + 1, retries)
                time.sleep(wait)
            else:
                raise
    return []


def get_trading_days(client: AngelOneClient, lookback: int) -> list[datetime]:
    end = datetime.now(IST)
    start = end - timedelta(days=lookback + 15)
    candles = _fetch_with_retry(
        client, "NSE", NIFTY_50_TOKEN, "ONE_DAY",
        start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M"),
    )
    days = []
    for c in candles:
        ts = datetime.fromisoformat(c[0]).replace(tzinfo=IST)
        # Nifty index has volume=0 — each candle IS a trading day.
        days.append(ts)
    today = end.date()
    days = [d for d in days if d.date() < today]
    return days[-lookback:]


def prefetch_daily_candles(
    client: AngelOneClient, stocks: list[dict],
    start: datetime, end: datetime,
) -> dict[str, list[list]]:
    """Prefetch daily candles for all stocks. Strictly sequential to avoid rate limits."""
    s_str = start.strftime("%Y-%m-%d %H:%M")
    e_str = end.strftime("%Y-%m-%d %H:%M")
    result: dict[str, list[list]] = {}
    total = len(stocks)
    errors = 0

    for i, stock in enumerate(stocks):
        try:
            c = _fetch_with_retry(client, "NSE", stock["token"], "ONE_DAY", s_str, e_str, retries=3)
            if c:
                result[stock["token"]] = c
        except Exception:
            pass

        if (i + 1) % 200 == 0:
            logger.info("  Prefetched: %d/%d (cached=%d)", i + 1, total, len(result))
        time.sleep(0.15)  # ~6-7 req/s.

    return result


def scan_candidates_for_day(
    stocks: list[dict], daily_cache: dict[str, list[list]], day: datetime,
) -> list[dict]:
    """In-memory scan: find 5-10% gainers on a day. Zero API calls."""
    target = day.strftime("%Y-%m-%d")
    candidates = []
    for stock in stocks:
        candles = daily_cache.get(stock["token"])
        if not candles:
            continue
        day_c = prev_c = None
        for i, c in enumerate(candles):
            if target in c[0]:
                day_c = c
                prev_c = candles[i - 1] if i > 0 else None
                break
        if not day_c or not prev_c:
            continue
        pc = float(prev_c[4])
        ltp = float(day_c[4])
        hi = float(day_c[2])
        lo = float(day_c[3])
        vol = float(day_c[5])
        op = float(day_c[1])
        if pc <= 0 or ltp <= 0 or ltp < MIN_PRICE or ltp > MAX_PRICE or vol < MIN_VOLUME:
            continue
        hi_pct = ((hi - pc) / pc) * 100
        if hi_pct < MIN_GAIN_PCT:
            continue
        day_pct = ((ltp - pc) / pc) * 100
        # Scoring (same weights as live, buy_pressure=0.5 since unknown).
        pvols = [float(x[5]) for x in candles if x[0] < day_c[0] and float(x[5]) > 0]
        avg_pv = sum(pvols) / len(pvols) if pvols else vol
        rv = vol / avg_pv if avg_pv > 0 else 1.0
        pcloses = [float(x[4]) for x in candles if x[0] < day_c[0]]
        pds = 0.0
        if len(pcloses) >= 2:
            up = sum(1 for j in range(1, len(pcloses)) if pcloses[j] > pcloses[j-1])
            pds = up / (len(pcloses) - 1)
        gap = abs(op - pc) / pc * 100
        stab = max(0.0, 1.0 - gap / 5.0)
        dr = hi - lo
        mom = (ltp - lo) / dr if dr > 0 else 0.5
        vs = min(rv / 2.0, 1.0)
        comp = vs * 0.25 + mom * 0.25 + 0.5 * 0.20 + stab * 0.15 + pds * 0.15
        candidates.append({
            "symbol": stock["symbol"], "token": stock["token"],
            "prev_close": pc, "open": op, "high": hi, "low": lo,
            "close": ltp, "volume": vol,
            "day_pct": round(day_pct, 2), "high_pct": round(hi_pct, 2),
            "composite_score": round(comp, 3), "rel_vol": round(rv, 2),
        })
    candidates.sort(key=lambda x: x["composite_score"], reverse=True)
    return candidates


def fetch_5min_candles(client: AngelOneClient, token: str, day: datetime) -> list[list]:
    s = day.replace(hour=9, minute=15, second=0)
    e = day.replace(hour=15, minute=30, second=0)
    return _fetch_with_retry(
        client, "NSE", token, "FIVE_MINUTE",
        s.strftime("%Y-%m-%d %H:%M"), e.strftime("%Y-%m-%d %H:%M"),
    ) or []


def find_candle_at_time(candles: list[list], hour: int, minute: int) -> int:
    target = f"{hour:02d}:{minute:02d}"
    for i, c in enumerate(candles):
        t = c[0]
        tp = t.split("T")[1][:5] if "T" in t else t.split(" ")[1][:5]
        if tp >= target:
            return i
    return len(candles) - 1


def validate_entry_historical(
    candles_5m: list[list], prev_close: float, entry_idx: int,
) -> tuple[bool, float, str]:
    """Validate entry using historical 5-min candles at entry_idx."""
    if entry_idx >= len(candles_5m) or entry_idx < 1:
        return False, 0.0, "Not enough candles"
    c = candles_5m[entry_idx]
    ltp = float(c[4])
    if prev_close <= 0 or ltp <= 0:
        return False, 0.0, "Invalid price"
    # Check 1: Momentum 5-10%.
    g = ((ltp - prev_close) / prev_close) * 100
    if not (MIN_GAIN_PCT <= g <= MAX_GAIN_PCT):
        return False, ltp, f"Gain {g:+.2f}% outside 5-10%"
    # Check 2: Range position >= 0.85.
    rh = max(float(x[2]) for x in candles_5m[:entry_idx + 1])
    rl = min(float(x[3]) for x in candles_5m[:entry_idx + 1])
    dr = rh - rl
    rp = (ltp - rl) / dr if dr > 0 else 1.0
    if rp < 0.85:
        return False, ltp, f"Range pos {rp:.3f} < 0.85"
    # Check 3: Micro breakout.
    ph = float(candles_5m[entry_idx - 1][2])
    if ltp <= ph:
        return False, ltp, f"No breakout: {ltp:.2f} <= {ph:.2f}"
    # Check 4: Volume >= 1.2x avg prior 3.
    cv = float(c[5])
    pvs = [float(candles_5m[j][5]) for j in range(max(0, entry_idx - 3), entry_idx)]
    if pvs:
        av = sum(pvs) / len(pvs)
        if av > 0 and cv / av < 1.2:
            return False, ltp, f"Vol ratio {cv/av:.2f} < 1.2"
    return True, ltp, "All checks passed"


def simulate_trailing_stop(
    candles_5m: list[list], entry_price: float,
    entry_idx: int, exit_candle_idx: int,
) -> tuple[float, str, int, float]:
    """Simulate trailing stop on 5-min candles. Returns (exit_price, reason, idx, highest)."""
    highest = entry_price
    stop = round(highest * (1 - DEFAULT_TRAIL_PCT), 2)
    hard = round(entry_price * (1 - MAX_LOSS_PCT), 2)
    for i in range(entry_idx + 1, min(len(candles_5m), exit_candle_idx + 1)):
        cl = float(candles_5m[i][3])  # candle low
        ch = float(candles_5m[i][2])  # candle high
        eff = max(stop, hard)
        if cl <= eff:
            reason = "hard_stop" if hard >= stop else "trailing_stop"
            return eff, reason, i, highest
        if ch > highest:
            highest = ch
        pp = (highest - entry_price) / entry_price
        if pp >= LOCK_PROFIT_THRESHOLD:
            ns = round(entry_price * (1 + LOCK_PROFIT_THRESHOLD), 2)
        elif pp >= TIGHT_TRAIL_PROFIT_THRESHOLD:
            ns = round(highest * (1 - TIGHT_TRAIL_PCT), 2)
        else:
            ns = round(highest * (1 - DEFAULT_TRAIL_PCT), 2)
        if ns > stop:
            stop = ns
    lc = float(candles_5m[min(len(candles_5m) - 1, exit_candle_idx)][4])
    return lc, "market_close", exit_candle_idx, highest


def backtest_one_day(
    client: AngelOneClient, day: datetime, candidates: list[dict],
) -> DayResult:
    dr = DayResult(date=day.strftime("%Y-%m-%d"), candidates_found=len(candidates))
    if not candidates:
        dr.skipped_reason = "No 5-10% gainers found"
        return dr
    top = candidates[:TOP_N * 5]  # Check more candidates for validation.
    validated = []
    rejection_stats: dict[str, int] = {}

    # Try entry at multiple times within the scan window (every 15 min).
    # This simulates the bot retrying every SCAN_RETRY_SECONDS.
    scan_start = SCAN_START_HOUR * 60 + SCAN_START_MIN  # 9:45 = 585
    scan_end = SCAN_END_HOUR * 60 + SCAN_END_MIN        # 12:30 = 750
    entry_times = list(range(scan_start, scan_end + 1, 15))  # Every 15 min.

    for cand in top:
        if len(validated) >= TOP_N:
            break
        time.sleep(0.3)  # Rate limit between 5-min candle fetches.
        candles = fetch_5min_candles(client, cand["token"], day)
        if len(candles) < 5:
            rejection_stats["no_candles"] = rejection_stats.get("no_candles", 0) + 1
            continue

        xidx = find_candle_at_time(candles, EXIT_HOUR, EXIT_MIN)
        found_entry = False

        # Try each time slot within the scan window.
        for t_min in entry_times:
            h, m = divmod(t_min, 60)
            eidx = find_candle_at_time(candles, h, m)
            if eidx < 1 or eidx >= xidx:
                continue
            ok, ep, reason = validate_entry_historical(candles, cand["prev_close"], eidx)
            if ok:
                validated.append({**cand, "candles": candles, "entry_idx": eidx,
                                  "entry_price": ep, "exit_idx": xidx})
                logger.info("    ENTRY %s at %02d:%02d: %.2f", cand["symbol"], h, m, ep)
                found_entry = True
                break

        if not found_entry and candles:
            # Log the rejection reason from the last attempted time.
            last_t = entry_times[-1]
            h, m = divmod(last_t, 60)
            eidx = find_candle_at_time(candles, h, m)
            _, _, reason = validate_entry_historical(candles, cand["prev_close"], eidx)
            if "outside 5-10%" in reason:
                key = "gain_outside_range"
            elif "Range pos" in reason:
                key = "range_position"
            elif "breakout" in reason:
                key = "no_breakout"
            elif "Vol" in reason:
                key = "volume_fading"
            else:
                key = "other"
            rejection_stats[key] = rejection_stats.get(key, 0) + 1
            logger.info("    REJECT %s: %s (tried %d time slots)", cand["symbol"], reason, len(entry_times))

    if rejection_stats:
        logger.info("  Rejection breakdown: %s", rejection_stats)
    dr.validated = len(validated)
    if not validated:
        dr.skipped_reason = "No candidates passed validation"
        return dr
    lev_cap = CAPITAL * LEVERAGE
    cap_per = lev_cap * 0.5 if len(validated) == 1 else lev_cap / len(validated)
    for e in validated:
        qty = int(cap_per / e["entry_price"])
        if qty <= 0:
            continue
        xp, xr, xi, hi = simulate_trailing_stop(
            e["candles"], e["entry_price"], e["entry_idx"], e["exit_idx"],
        )
        pnl = (xp - e["entry_price"]) * qty
        pnl_pct = ((xp - e["entry_price"]) / e["entry_price"]) * 100
        ets = e["candles"][e["entry_idx"]][0] if e["entry_idx"] < len(e["candles"]) else ""
        xts = e["candles"][xi][0] if xi < len(e["candles"]) else ""
        ge = ((e["entry_price"] - e["prev_close"]) / e["prev_close"]) * 100
        dr.trades.append(TradeResult(
            date=day.strftime("%Y-%m-%d"), symbol=e["symbol"], token=e["token"],
            entry_price=round(e["entry_price"], 2), exit_price=round(xp, 2),
            quantity=qty, pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2),
            exit_reason=xr, entry_time=ets, exit_time=xts,
            highest_price=round(hi, 2), gain_at_entry=round(ge, 2),
        ))
    return dr


def print_report(results: list[DayResult]) -> None:
    trades = [t for r in results for t in r.trades]
    td = len(results)
    tt = len(trades)
    if tt == 0:
        print("\n=== NO TRADES in %d days ===" % td)
        for r in results:
            print(f"  {r.date}: {r.skipped_reason or 'no candidates'} (found={r.candidates_found})")
        return
    w = [t for t in trades if t.pnl > 0]
    l = [t for t in trades if t.pnl <= 0]
    wr = len(w) / tt * 100
    tp = sum(t.pnl for t in trades)
    aw = sum(t.pnl for t in w) / len(w) if w else 0
    al = sum(t.pnl for t in l) / len(l) if l else 0
    awp = sum(t.pnl_pct for t in w) / len(w) if w else 0
    alp = sum(t.pnl_pct for t in l) / len(l) if l else 0
    bt = max(trades, key=lambda t: t.pnl)
    wt = min(trades, key=lambda t: t.pnl)
    cum = pk = md = 0.0
    for t in trades:
        cum += t.pnl
        if cum > pk: pk = cum
        dd = pk - cum
        if dd > md: md = dd
    gp = sum(t.pnl for t in w)
    gl = abs(sum(t.pnl for t in l))
    pf = gp / gl if gl > 0 else float("inf")
    er: dict[str, int] = {}
    for t in trades:
        er[t.exit_reason] = er.get(t.exit_reason, 0) + 1
    mcl = cl = 0
    for t in trades:
        if t.pnl <= 0:
            cl += 1
            mcl = max(mcl, cl)
        else:
            cl = 0

    print("\n" + "=" * 70)
    print("                    BACKTEST REPORT")
    print("=" * 70)
    print(f"Period:              {results[0].date} to {results[-1].date}")
    print(f"Capital:             Rs.{CAPITAL:,.0f} (leverage {LEVERAGE}x = Rs.{CAPITAL*LEVERAGE:,.0f})")
    print(f"Days analyzed:       {td}")
    print(f"Days traded:         {sum(1 for r in results if r.trades)}")
    print(f"Days skipped:        {sum(1 for r in results if not r.trades)}")
    print()
    print(f"Total trades:        {tt}")
    print(f"Winners:             {len(w)}")
    print(f"Losers:              {len(l)}")
    print(f"Win rate:            {wr:.1f}%")
    print()
    print(f"Total P&L:           Rs.{tp:,.2f} ({tp/CAPITAL*100:+.2f}% on capital)")
    print(f"Avg win:             Rs.{aw:,.2f} ({awp:+.2f}%)")
    print(f"Avg loss:            Rs.{al:,.2f} ({alp:+.2f}%)")
    print(f"Profit factor:       {pf:.2f}")
    print(f"Max drawdown:        Rs.{md:,.2f}")
    print(f"Max consec losses:   {mcl}")
    print()
    print(f"Best trade:          {bt.symbol} {bt.date}: Rs.{bt.pnl:+,.2f} ({bt.pnl_pct:+.2f}%)")
    print(f"Worst trade:         {wt.symbol} {wt.date}: Rs.{wt.pnl:+,.2f} ({wt.pnl_pct:+.2f}%)")
    print()
    print("Exit reasons:")
    for reason, count in sorted(er.items()):
        print(f"  {reason:20s} {count:3d} ({count/tt*100:.0f}%)")
    print()
    print("-" * 80)
    print(f"{'Date':<12} {'Symbol':<16} {'Entry':>8} {'Exit':>8} {'Qty':>5} {'P&L':>10} {'%':>7} {'Reason':<15}")
    print("-" * 80)
    for t in trades:
        print(f"{t.date:<12} {t.symbol:<16} {t.entry_price:>8.2f} {t.exit_price:>8.2f} "
              f"{t.quantity:>5d} {t.pnl:>+10.2f} {t.pnl_pct:>+6.2f}% {t.exit_reason:<15}")
    print()
    print("-" * 80)
    print("DAILY SUMMARY")
    print("-" * 80)
    cum = 0.0
    for r in results:
        dp = sum(t.pnl for t in r.trades)
        cum += dp
        if r.trades:
            syms = ", ".join(t.symbol for t in r.trades)
            print(f"{r.date}  n={len(r.trades)}  P&L=Rs.{dp:>+10,.2f}  cum=Rs.{cum:>+10,.2f}  [{syms}]")
        else:
            print(f"{r.date}  SKIP: {r.skipped_reason or 'no candidates'}")
    print("=" * 70)


def run_backtest():
    client = login_broker()

    logger.info("Loading NSE equity tokens...")
    stocks = load_nse_equity_tokens(client)
    logger.info("Loaded %d NSE stocks.", len(stocks))

    logger.info("Finding trading days (last %d)...", LOOKBACK_DAYS)
    trading_days = get_trading_days(client, LOOKBACK_DAYS)
    logger.info("Found %d trading days.", len(trading_days))
    if not trading_days:
        logger.error("No trading days found.")
        return

    # Prefetch daily candles for all stocks (one-time cost).
    period_start = trading_days[0] - timedelta(days=10)
    period_end = trading_days[-1] + timedelta(days=1)
    logger.info("Prefetching daily candles for %d stocks (%s to %s)...",
                len(stocks), period_start.strftime("%Y-%m-%d"), period_end.strftime("%Y-%m-%d"))
    daily_cache = prefetch_daily_candles(client, stocks, period_start, period_end)
    logger.info("Prefetched candles for %d stocks. Cooling down 30s for rate limit...", len(daily_cache))
    time.sleep(30)  # Let rate limit window reset.

    results: list[DayResult] = []
    for i, day in enumerate(trading_days):
        ds = day.strftime("%Y-%m-%d")
        logger.info("\n--- Day %d/%d: %s ---", i + 1, len(trading_days), ds)

        candidates = scan_candidates_for_day(stocks, daily_cache, day)
        logger.info("Candidates in 5-10%% range: %d", len(candidates))
        for j, c in enumerate(candidates[:5]):
            logger.info("  #%d %s: day=%+.2f%% hi=%+.2f%% score=%.3f vol=%.1fx",
                        j+1, c["symbol"], c["day_pct"], c["high_pct"], c["composite_score"], c["rel_vol"])

        dr = backtest_one_day(client, day, candidates)
        results.append(dr)
        for t in dr.trades:
            logger.info("  TRADE: %s entry=%.2f exit=%.2f P&L=Rs.%+.2f (%+.2f%%) [%s]",
                        t.symbol, t.entry_price, t.exit_price, t.pnl, t.pnl_pct, t.exit_reason)
        if not dr.trades:
            logger.info("  No trades: %s", dr.skipped_reason)
        time.sleep(0.5)

    print_report(results)


if __name__ == "__main__":
    run_backtest()


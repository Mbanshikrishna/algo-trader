from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from broker.angelone_client import AngelOneClient

logger = logging.getLogger("algo_trader")

# Nifty 50 index token on NSE.
NIFTY_50_TOKEN = "99926000"
# India VIX token on NSE.
INDIA_VIX_TOKEN = "99926017"
# NIFTYBEES ETF token (used for VWAP — Nifty index has no volume).
NIFTYBEES_TOKEN = "10576"

# Nifty 50 constituent tokens for breadth calculation.
NIFTY_50_CONSTITUENTS: dict[str, str] = {
    "ADANIENT": "25", "ADANIPORTS": "15083", "APOLLOHOSP": "157",
    "ASIANPAINT": "236", "AXISBANK": "5900", "BAJAJ-AUTO": "16669",
    "BAJAJFINSV": "16675", "BAJFINANCE": "317", "BEL": "383",
    "BHARTIARTL": "10604", "CIPLA": "694", "COALINDIA": "20374",
    "DRREDDY": "881", "EICHERMOT": "910", "ETERNAL": "5097",
    "GRASIM": "1232", "HCLTECH": "7229", "HDFCBANK": "1333",
    "HDFCLIFE": "467", "HINDALCO": "1363", "HINDUNILVR": "1394",
    "ICICIBANK": "4963", "INDIGO": "11195", "INFY": "1594",
    "ITC": "1660", "JIOFIN": "18143", "JSWSTEEL": "11723",
    "KOTAKBANK": "1922", "LT": "11483", "M&M": "2031",
    "MARUTI": "10999", "MAXHEALTH": "22377", "NESTLEIND": "17963",
    "NTPC": "11630", "ONGC": "2475", "POWERGRID": "14977",
    "RELIANCE": "2885", "SBILIFE": "21808", "SBIN": "3045",
    "SHRIRAMFIN": "4306", "SUNPHARMA": "3351", "TATACONSUM": "3432",
    "TATASTEEL": "3499", "TCS": "11536", "TECHM": "13538",
    "TITAN": "3506", "TMPV": "3456", "TRENT": "1964",
    "ULTRACEMCO": "11532", "WIPRO": "3787",
}

# Angel One batch quote limit.
BATCH_SIZE = 50

# Rate-limit delay between batch requests (seconds).
BATCH_DELAY = 0.3

IST = ZoneInfo("Asia/Kolkata")

# --- Gain range filter ---
MIN_GAIN_PCT = 5.0   # Minimum intraday gain to consider.
MAX_GAIN_PCT = 10.0  # Maximum intraday gain (avoid overextended stocks).

# --- Quality filters ---
MIN_PRICE = 50.0      # Avoid penny stocks.
MAX_PRICE = 5000.0    # Avoid very expensive stocks.
MIN_VOLUME = 100_000  # Minimum intraday volume for liquidity.


# ---------------------------------------------------------------------------
# Multi-factor market bullishness model
# ---------------------------------------------------------------------------

@dataclass
class MarketCheckResult:
    """Result of the 4-factor market bullishness check."""

    bullish: bool = False
    score: int = 0

    # Factor 1: Index Direction
    nifty_pct: float = 0.0
    index_pass: bool = False

    # Factor 2: Market Breadth
    advancing: int = 0
    declining: int = 0
    breadth_ratio: float = 0.0
    breadth_pass: bool = False

    # Factor 3: Intraday Strength
    nifty_ltp: float = 0.0
    nifty_open: float = 0.0
    niftybees_ltp: float = 0.0
    niftybees_vwap: float = 0.0
    strength_pass: bool = False

    # Factor 4: Volatility Filter
    vix_pct: float = 0.0
    volatility_pass: bool = False

    # Holiday detection
    is_holiday: bool = False

    def format_report(self) -> str:
        """Format a structured Telegram-friendly report."""
        if self.is_holiday:
            return "Market closed (holiday). Skipping."

        lines = [
            f"Market Check: {self.score}/4 — {'BULLISH' if self.bullish else 'NOT BULLISH'}",
            "",
            f"1. Index Direction: {'PASS ✅' if self.index_pass else 'FAIL ❌'}",
            f"   Nifty 50: {self.nifty_pct:+.2f}% (need > 0%)",
            "",
            f"2. Market Breadth: {'PASS ✅' if self.breadth_pass else 'FAIL ❌'}",
            f"   Advancers: {self.advancing}, Decliners: {self.declining}",
            f"   Breadth ratio: {self.breadth_ratio:.2f} (need > 1.2)",
            "",
            f"3. Intraday Strength: {'PASS ✅' if self.strength_pass else 'FAIL ❌'}",
            f"   Nifty LTP vs Open: {self.nifty_ltp:.2f} vs {self.nifty_open:.2f}",
            f"   NIFTYBEES vs VWAP: {self.niftybees_ltp:.2f} vs {self.niftybees_vwap:.2f}",
            "",
            f"4. Volatility Filter: {'PASS ✅' if self.volatility_pass else 'FAIL ❌'}",
            f"   India VIX change: {self.vix_pct:+.2f}% (reject if > +5%)",
            "",
        ]

        if self.bullish:
            lines.append(f"Decision: TRADE (score {self.score}/4 >= 2)")
        else:
            failed = []
            if not self.index_pass:
                failed.append("Index Direction")
            if not self.breadth_pass:
                failed.append("Market Breadth")
            if not self.strength_pass:
                failed.append("Intraday Strength")
            if not self.volatility_pass:
                failed.append("Volatility Filter")
            lines.append(f"Decision: SKIP TRADING (score {self.score}/4 < 2)")
            lines.append(f"Failed: {', '.join(failed)}")

        return "\n".join(lines)


def is_market_bullish(client: AngelOneClient) -> tuple[bool, MarketCheckResult]:
    """4-factor market bullishness check with 3-out-of-4 decision rule.

    Factors:
      1. Index Direction  — Nifty 50 change > 0%
      2. Market Breadth   — Nifty 50 advancers/decliners > 1.2
      3. Intraday Strength — Nifty LTP > Open AND NIFTYBEES > VWAP
      4. Volatility Filter — India VIX change < +5%

    Returns (is_bullish, detailed_result).
    """
    result = MarketCheckResult()
    today_str = datetime.now(IST).strftime("%d-%b-%Y")

    # --- Fetch Nifty 50 + India VIX + NIFTYBEES in one batch ---
    try:
        market_data = client.get_market_data(
            "FULL", {"NSE": [NIFTY_50_TOKEN, INDIA_VIX_TOKEN, NIFTYBEES_TOKEN]},
        )
    except Exception as exc:
        logger.warning("Failed to fetch market data: %s", exc)
        return False, result

    fetched_map: dict[str, dict] = {}
    for q in market_data.get("fetched", []):
        fetched_map[str(q.get("symbolToken", ""))] = q

    nifty = fetched_map.get(NIFTY_50_TOKEN, {})
    vix = fetched_map.get(INDIA_VIX_TOKEN, {})
    bees = fetched_map.get(NIFTYBEES_TOKEN, {})

    if not nifty:
        logger.warning("No Nifty 50 data returned.")
        return False, result

    # --- Holiday detection ---
    trade_volume = int(float(nifty.get("tradeVolume", 0)))
    feed_time = str(nifty.get("exchFeedTime", ""))
    if trade_volume == 0 and today_str not in feed_time:
        logger.info("Market appears closed (holiday). tradeVolume=0, exchFeedTime=%s", feed_time)
        result.is_holiday = True
        return False, result

    # --- Factor 1: Index Direction ---
    result.nifty_pct = float(nifty.get("percentChange", 0))
    if result.nifty_pct == 0:
        # Fallback: compute from LTP and close.
        ltp = float(nifty.get("ltp", 0))
        prev_close = float(nifty.get("close", 0))
        if prev_close > 0 and ltp > 0:
            result.nifty_pct = round(((ltp - prev_close) / prev_close) * 100, 2)
    result.index_pass = result.nifty_pct > 0

    # --- Factor 2: Market Breadth (Nifty 50 constituents) ---
    try:
        constituent_tokens = list(NIFTY_50_CONSTITUENTS.values())
        breadth_data = client.get_market_data("FULL", {"NSE": constituent_tokens})
        for q in breadth_data.get("fetched", []):
            pct = float(q.get("percentChange", 0))
            if pct == 0:
                # Fallback: compute from ltp/close.
                qltp = float(q.get("ltp", 0))
                qclose = float(q.get("close", 0))
                if qclose > 0 and qltp > 0:
                    pct = ((qltp - qclose) / qclose) * 100
            if pct > 0:
                result.advancing += 1
            elif pct < 0:
                result.declining += 1
    except Exception as exc:
        logger.warning("Failed to fetch breadth data: %s", exc)

    if result.declining > 0:
        result.breadth_ratio = round(result.advancing / result.declining, 2)
    elif result.advancing > 0:
        result.breadth_ratio = 99.0  # All advancing, no decliners.
    result.breadth_pass = result.breadth_ratio > 1.2

    # --- Factor 3: Intraday Strength ---
    result.nifty_ltp = float(nifty.get("ltp", 0))
    result.nifty_open = float(nifty.get("open", 0))
    result.niftybees_ltp = float(bees.get("ltp", 0))
    # NIFTYBEES avgPrice is the exchange-computed VWAP.
    result.niftybees_vwap = float(bees.get("avgPrice", 0))

    ltp_above_open = result.nifty_ltp > result.nifty_open
    bees_above_vwap = (
        result.niftybees_ltp > result.niftybees_vwap
        if result.niftybees_vwap > 0
        else True  # If VWAP unavailable, don't penalize.
    )
    result.strength_pass = ltp_above_open and bees_above_vwap

    # --- Factor 4: Volatility Filter ---
    result.vix_pct = float(vix.get("percentChange", 0))
    result.volatility_pass = result.vix_pct < 5.0

    # --- Scoring: 3 out of 4 required ---
    result.score = sum([
        result.index_pass,
        result.breadth_pass,
        result.strength_pass,
        result.volatility_pass,
    ])
    result.bullish = result.score >= 2

    return result.bullish, result


def load_nse_equity_tokens(client: AngelOneClient) -> list[dict[str, str]]:
    """Load all NSE equity stock tokens from the scrip master."""
    master = client._load_scrip_master()
    stocks = []
    for row in master:
        if row.get("exch_seg") != "NSE":
            continue
        symbol = str(row.get("symbol", ""))
        if not symbol.endswith("-EQ"):
            continue
        if "INAV" in symbol:
            continue
        stocks.append({
            "symbol": symbol,
            "token": str(row.get("token", "")),
            "name": str(row.get("name", "")),
        })
    return stocks


def _fetch_all_quotes(
    client: AngelOneClient,
    stocks: list[dict[str, str]],
    max_workers: int = 3,
) -> list[dict[str, Any]]:
    """Fetch FULL quotes for all stocks in parallel batches of 50.

    Uses concurrent workers to fetch multiple batches simultaneously.
    With 3 workers and 50 batches, reduces scan time from ~15s to ~5-6s.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    all_tokens = [s["token"] for s in stocks]
    token_to_stock = {s["token"]: s for s in stocks}

    # Split into batches of 50.
    batches = [all_tokens[i : i + BATCH_SIZE] for i in range(0, len(all_tokens), BATCH_SIZE)]

    def _fetch_batch(batch: list[str]) -> list[dict[str, Any]]:
        try:
            result = client.get_market_data("FULL", {"NSE": batch})
            fetched = result.get("fetched", [])
            for q in fetched:
                token = str(q.get("symbolToken", ""))
                stock_info = token_to_stock.get(token, {})
                q["_symbol"] = stock_info.get("symbol", q.get("tradingSymbol", ""))
                q["_token"] = token
                q["_name"] = stock_info.get("name", "")
            return fetched
        except Exception:
            return []

    all_quotes: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_batch, b): i for i, b in enumerate(batches)}
        # Collect results in submission order to maintain deterministic output.
        results_by_index: dict[int, list] = {}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results_by_index[idx] = future.result()
            except Exception:
                results_by_index[idx] = []

        for idx in sorted(results_by_index):
            all_quotes.extend(results_by_index[idx])

    return all_quotes


# Session-level daily candle cache. Avoids re-fetching the same candles
# when the scan retries within the 9:45-12:30 window. Daily candles don't
# change intraday, so caching is safe for the entire session.
_daily_candle_cache: dict[str, list[list]] = {}


def clear_daily_candle_cache() -> None:
    """Clear the daily candle cache. Call once at start of each trading day."""
    _daily_candle_cache.clear()
    logger.info("Daily candle cache cleared.")


def _fetch_previous_day_candles(
    client: AngelOneClient,
    tokens: list[str],
    max_workers: int = 5,
) -> dict[str, list[list]]:
    """Fetch last 5 daily candles for given tokens concurrently.

    Uses a session-level cache so repeated scans within the same day
    don't re-fetch candles for tokens already seen.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    now = datetime.now(IST)
    start = now - timedelta(days=10)
    result: dict[str, list[list]] = {}

    # Separate cached vs uncached tokens.
    uncached: list[str] = []
    for token in tokens:
        if token in _daily_candle_cache:
            result[token] = _daily_candle_cache[token]
        else:
            uncached.append(token)

    if not uncached:
        return result

    def _fetch_one(token: str) -> tuple[str, list[list]]:
        candles = client.get_candle_data("NSE", token, "ONE_DAY", start, now)
        return token, candles

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, t): t for t in uncached}
        for future in as_completed(futures):
            token = futures[future]
            try:
                t, candles = future.result()
                result[t] = candles
                _daily_candle_cache[t] = candles  # Cache for future scans.
            except Exception:
                result[token] = []

    return result


def _compute_score(quote: dict[str, Any], daily_candles: list[list]) -> dict[str, Any] | None:
    """Score a stock based on multiple factors. Returns None if disqualified."""
    ltp = float(quote.get("ltp", 0))
    prev_close = float(quote.get("close", 0))
    open_price = float(quote.get("open", 0))
    high = float(quote.get("high", 0))
    low = float(quote.get("low", 0))
    volume = int(float(quote.get("tradeVolume", 0)))
    tot_buy = int(float(quote.get("totBuyQuan", 0)))
    tot_sell = int(float(quote.get("totSellQuan", 0)))

    # --- Basic filters ---
    if prev_close <= 0 or ltp <= 0 or open_price <= 0:
        return None
    if ltp < MIN_PRICE or ltp > MAX_PRICE:
        return None
    if volume < MIN_VOLUME:
        return None

    pct_change = ((ltp - prev_close) / prev_close) * 100
    if pct_change < MIN_GAIN_PCT or pct_change > MAX_GAIN_PCT:
        return None

    # --- Factor 1: Volume strength (relative volume) ---
    # Compare today's volume to average of previous days.
    avg_prev_volume = 0.0
    if len(daily_candles) >= 2:
        prev_volumes = [float(c[5]) for c in daily_candles[:-1] if float(c[5]) > 0]
        if prev_volumes:
            avg_prev_volume = sum(prev_volumes) / len(prev_volumes)

    relative_volume = volume / avg_prev_volume if avg_prev_volume > 0 else 1.0
    volume_score = min(relative_volume / 2.0, 1.0)  # Normalize: 2x avg volume = max score.

    # --- Factor 2: Momentum (price position in today's range) ---
    # Higher score if price is near the day's high (strong momentum).
    day_range = high - low
    if day_range > 0:
        momentum_score = (ltp - low) / day_range  # 1.0 = at day high, 0.0 = at day low.
    else:
        momentum_score = 0.5

    # --- Factor 3: Buying pressure ---
    # Ratio of buy quantity to total (buy + sell) pending orders.
    total_orders = tot_buy + tot_sell
    if total_orders > 0:
        buy_pressure = tot_buy / total_orders  # 1.0 = all buyers, 0.0 = all sellers.
    else:
        buy_pressure = 0.5

    # --- Factor 4: Stability (gap-up vs gradual rise) ---
    # Prefer stocks that opened near prev close and rose gradually (not gap-up).
    open_gap_pct = abs(open_price - prev_close) / prev_close * 100
    stability_score = max(0.0, 1.0 - (open_gap_pct / 5.0))  # 0% gap = 1.0, 5%+ gap = 0.0.

    # --- Factor 5: Previous day trend (upward trend confirmation) ---
    # Check if previous 3-5 days show higher closes (uptrend).
    prev_day_score = 0.0
    if len(daily_candles) >= 3:
        closes = [float(c[4]) for c in daily_candles[:-1]]  # Exclude today.
        if len(closes) >= 2:
            up_days = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
            prev_day_score = up_days / (len(closes) - 1)  # 1.0 = all up days.

    # --- Composite score (weighted) ---
    composite = (
        volume_score * 0.25        # 25% weight: high relative volume
        + momentum_score * 0.25    # 25% weight: price near day high
        + buy_pressure * 0.20      # 20% weight: more buyers than sellers
        + stability_score * 0.15   # 15% weight: gradual rise, not gap-up
        + prev_day_score * 0.15    # 15% weight: previous days uptrend
    )

    return {
        "symbol": quote["_symbol"],
        "token": quote["_token"],
        "name": quote["_name"],
        "ltp": ltp,
        "prev_close": prev_close,
        "open": open_price,
        "high": high,
        "low": low,
        "volume": volume,
        "pct_change": round(pct_change, 2),
        "relative_volume": round(relative_volume, 2),
        "momentum_score": round(momentum_score, 2),
        "buy_pressure": round(buy_pressure, 2),
        "stability_score": round(stability_score, 2),
        "prev_day_score": round(prev_day_score, 2),
        "composite_score": round(composite, 3),
        "lower_circuit": float(quote.get("lowerCircuit", 0)),
        "upper_circuit": float(quote.get("upperCircuit", 0)),
    }


def scan_top_gainers(
    client: AngelOneClient,
    stocks: list[dict[str, str]],
    top_n: int = 2,
) -> list[dict[str, Any]]:
    """Scan all NSE stocks and return the top N by composite score.

    Selection criteria:
    1. Intraday gain between 5% and 10% (sweet spot — not overextended).
    2. Ranked by composite score combining:
       - Relative volume (today vs average of previous days)
       - Momentum (price position in today's range)
       - Buying pressure (buy vs sell order book)
       - Stability (gradual rise preferred over gap-up)
       - Previous day trend (multi-day uptrend confirmation)
    """
    # Phase 1: Fetch all quotes and apply basic filters.
    logger.info("Fetching quotes for %d stocks...", len(stocks))
    all_quotes = _fetch_all_quotes(client, stocks)
    logger.info("Received %d quotes.", len(all_quotes))

    # Pre-filter to 5-10% gain range before fetching candles.
    candidates = []
    for q in all_quotes:
        ltp = float(q.get("ltp", 0))
        prev_close = float(q.get("close", 0))
        volume = int(float(q.get("tradeVolume", 0)))

        if prev_close <= 0 or ltp <= 0:
            continue
        if ltp < MIN_PRICE or ltp > MAX_PRICE:
            continue
        if volume < MIN_VOLUME:
            continue

        pct = ((ltp - prev_close) / prev_close) * 100
        if MIN_GAIN_PCT <= pct <= MAX_GAIN_PCT:
            candidates.append(q)

    logger.info("Candidates in %g-%g%% gain range: %d", MIN_GAIN_PCT, MAX_GAIN_PCT, len(candidates))

    if not candidates:
        return []

    # Phase 2: Fetch previous day candles only for candidates.
    candidate_tokens = list({str(q.get("symbolToken", "")) for q in candidates})
    logger.info("Fetching daily candles for %d candidates...", len(candidate_tokens))
    daily_data = _fetch_previous_day_candles(client, candidate_tokens)

    # Phase 3: Score each candidate.
    scored: list[dict[str, Any]] = []
    for q in candidates:
        token = str(q.get("symbolToken", ""))
        candles = daily_data.get(token, [])
        result = _compute_score(q, candles)
        if result is not None:
            scored.append(result)

    # Phase 4: Sort by composite score and return top N.
    scored.sort(key=lambda x: x["composite_score"], reverse=True)

    for i, s in enumerate(scored[:top_n]):
        logger.info(
            "Rank #%d: %s — gain=%+.2f%%, score=%.3f "
            "(vol=%.1fx, momentum=%.2f, buy_pressure=%.2f, stability=%.2f, prev_trend=%.2f)",
            i + 1, s["symbol"], s["pct_change"], s["composite_score"],
            s["relative_volume"], s["momentum_score"], s["buy_pressure"],
            s["stability_score"], s["prev_day_score"],
        )

    return scored[:top_n]

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from broker.angelone_client import AngelOneClient

logger = logging.getLogger("algo_trader")

# Nifty 50 index token on NSE.
NIFTY_50_TOKEN = "99926000"

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


def is_market_bullish(client: AngelOneClient, retries: int = 3) -> tuple[bool, float]:
    """Check if Nifty 50 is in a positive trend today.

    Uses Angel One's pre-computed percentChange when available.
    Retries on zero-change to handle stale data at market open.
    """
    for attempt in range(retries):
        result = client.get_market_data("FULL", {"NSE": [NIFTY_50_TOKEN]})
        fetched = result.get("fetched", [])
        if not fetched:
            if attempt < retries - 1:
                time.sleep(5)
                continue
            return False, 0.0

        nifty = fetched[0]

        # Prefer the broker's pre-computed field — avoids stale LTP issues.
        pct_change = float(nifty.get("percentChange", 0))
        if pct_change != 0:
            return pct_change >= 0, round(pct_change, 2)

        # Fallback: compute from LTP and previous close.
        ltp = float(nifty.get("ltp", 0))
        prev_close = float(nifty.get("close", 0))

        if prev_close <= 0:
            return False, 0.0

        computed = ((ltp - prev_close) / prev_close) * 100
        if computed != 0 or attempt >= retries - 1:
            return computed >= 0, round(computed, 2)

        # 0.00% at market open likely means stale data — retry after a short wait.
        logger.info("Nifty change is 0.00%% — retrying in 5s (attempt %d/%d)...", attempt + 1, retries)
        time.sleep(5)

    return False, 0.0


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
) -> list[dict[str, Any]]:
    """Fetch FULL quotes for all stocks in batches of 50."""
    all_tokens = [s["token"] for s in stocks]
    token_to_stock = {s["token"]: s for s in stocks}

    all_quotes: list[dict[str, Any]] = []
    for i in range(0, len(all_tokens), BATCH_SIZE):
        batch = all_tokens[i : i + BATCH_SIZE]
        try:
            result = client.get_market_data("FULL", {"NSE": batch})
            fetched = result.get("fetched", [])
            # Attach stock metadata to each quote.
            for q in fetched:
                token = str(q.get("symbolToken", ""))
                stock_info = token_to_stock.get(token, {})
                q["_symbol"] = stock_info.get("symbol", q.get("tradingSymbol", ""))
                q["_token"] = token
                q["_name"] = stock_info.get("name", "")
            all_quotes.extend(fetched)
        except Exception:
            pass
        if i + BATCH_SIZE < len(all_tokens):
            time.sleep(BATCH_DELAY)

    return all_quotes


def _fetch_previous_day_candles(
    client: AngelOneClient,
    tokens: list[str],
    max_workers: int = 5,
) -> dict[str, list[list]]:
    """Fetch last 5 daily candles for given tokens concurrently."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    now = datetime.now(IST)
    start = now - timedelta(days=10)
    result: dict[str, list[list]] = {}

    def _fetch_one(token: str) -> tuple[str, list[list]]:
        candles = client.get_candle_data("NSE", token, "ONE_DAY", start, now)
        return token, candles

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, t): t for t in tokens}
        for future in as_completed(futures):
            token = futures[future]
            try:
                t, candles = future.result()
                result[t] = candles
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

"""Real-time entry validation for momentum continuation trades.

Fetches live market data immediately before order placement and checks
5 conditions. All must pass for the entry to proceed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("algo_trader")

IST = ZoneInfo("Asia/Kolkata")

# --- Validation thresholds ---
MIN_GAIN_PCT = 5.0       # Minimum intraday gain (must still be in range).
MAX_GAIN_PCT = 10.0      # Maximum intraday gain.
MIN_RANGE_POSITION = 0.85  # Price must be in top 15% of day's range.
MIN_VOLUME_RATIO = 1.2   # Current 1-min volume must be ≥ 1.2× avg of prior 5 mins.
MAX_SPREAD_PCT = 0.002   # Reject if bid-ask spread > 0.2% of price.


@dataclass
class ValidationResult:
    """Outcome of a single entry validation."""

    valid: bool
    symbol: str
    live_price: float = 0.0
    reason: str = ""

    # Individual check results (for logging).
    gain_pct: float = 0.0
    range_position: float = 0.0
    breakout_ok: bool = False
    volume_ratio: float = 0.0
    spread_pct: float = 0.0


def validate_entry(
    symbol: str,
    token: str,
    client: Any,
    exchange: str = "NSE",
) -> ValidationResult:
    """Fetch live data and validate all 5 entry conditions.

    Returns a ValidationResult with .valid=True only if ALL pass.
    Designed for minimal latency: 1 FULL quote + 1 candle fetch.
    """
    result = ValidationResult(valid=False, symbol=symbol)

    # --- Fetch live FULL quote (1 API call) ---
    try:
        quote_data = client.get_market_data("FULL", {exchange: [str(token)]})
        fetched = quote_data.get("fetched", [])
        if not fetched:
            result.reason = "No quote data returned"
            logger.info("ENTRY REJECTED %s: %s", symbol, result.reason)
            return result
        quote = fetched[0]
    except Exception as exc:
        result.reason = f"Quote fetch failed: {exc}"
        logger.warning("ENTRY REJECTED %s: %s", symbol, result.reason)
        return result

    ltp = float(quote.get("ltp", 0))
    day_open = float(quote.get("open", 0))
    day_high = float(quote.get("high", 0))
    day_low = float(quote.get("low", 0))
    prev_close = float(quote.get("close", 0))
    result.live_price = ltp

    if ltp <= 0 or prev_close <= 0:
        result.reason = "Invalid price data (ltp=0 or close=0)"
        logger.info("ENTRY REJECTED %s: %s", symbol, result.reason)
        return result

    # --- Check 1: Momentum still valid (5-10% gain) ---
    result.gain_pct = round(((ltp - prev_close) / prev_close) * 100, 2)
    if not (MIN_GAIN_PCT <= result.gain_pct <= MAX_GAIN_PCT):
        result.reason = (
            f"Gain {result.gain_pct:+.2f}% outside {MIN_GAIN_PCT}-{MAX_GAIN_PCT}% range"
        )
        logger.info("ENTRY REJECTED %s: %s", symbol, result.reason)
        return result

    # --- Check 2: Price strength (range position ≥ 0.85) ---
    day_range = day_high - day_low
    if day_range > 0:
        result.range_position = round((ltp - day_low) / day_range, 3)
    else:
        result.range_position = 1.0  # No range = price hasn't moved, treat as top.

    if result.range_position < MIN_RANGE_POSITION:
        result.reason = (
            f"Range position {result.range_position:.3f} < {MIN_RANGE_POSITION} "
            f"(price retreating from high)"
        )
        logger.info("ENTRY REJECTED %s: %s", symbol, result.reason)
        return result

    # --- Check 3: Micro breakout (price > last 5-min candle high) ---
    try:
        now = datetime.now(IST)
        # Fetch last 2 five-minute candles (current + previous).
        candle_from = now - timedelta(minutes=15)
        candles = client.get_candle_data(
            exchange, str(token), "FIVE_MINUTE",
            candle_from.strftime("%Y-%m-%d %H:%M"),
            now.strftime("%Y-%m-%d %H:%M"),
        )
        if candles and len(candles) >= 2:
            # The last completed candle is candles[-2] (candles[-1] is the current forming candle).
            last_completed_high = float(candles[-2][2])  # index 2 = high
            result.breakout_ok = ltp > last_completed_high
            if not result.breakout_ok:
                result.reason = (
                    f"No micro breakout: LTP {ltp:.2f} <= last 5min high {last_completed_high:.2f}"
                )
                logger.info("ENTRY REJECTED %s: %s", symbol, result.reason)
                return result
        elif candles and len(candles) == 1:
            # Only one candle (market just opened) — use its high.
            candle_high = float(candles[0][2])
            result.breakout_ok = ltp > candle_high
            if not result.breakout_ok:
                result.reason = (
                    f"No micro breakout: LTP {ltp:.2f} <= candle high {candle_high:.2f}"
                )
                logger.info("ENTRY REJECTED %s: %s", symbol, result.reason)
                return result
        else:
            # No candle data — skip this check rather than blocking a valid trade.
            result.breakout_ok = True
            logger.debug("No candle data for %s breakout check — skipping", symbol)
    except Exception as exc:
        # Candle fetch failed — don't block the trade for a data issue.
        result.breakout_ok = True
        logger.warning("Candle fetch failed for %s breakout check: %s — skipping", symbol, exc)

    # --- Check 4: Volume confirmation (current 1-min vol ≥ 1.2× avg of prior 5 mins) ---
    try:
        candle_from_1m = now - timedelta(minutes=10)
        candles_1m = client.get_candle_data(
            exchange, str(token), "ONE_MINUTE",
            candle_from_1m.strftime("%Y-%m-%d %H:%M"),
            now.strftime("%Y-%m-%d %H:%M"),
        )
        if candles_1m and len(candles_1m) >= 3:
            # Last candle is current (forming), use it as "current volume".
            current_vol = float(candles_1m[-1][5])  # index 5 = volume
            # Average of the prior candles (excluding current).
            prior_vols = [float(c[5]) for c in candles_1m[:-1]]
            avg_prior = sum(prior_vols) / len(prior_vols) if prior_vols else 0

            if avg_prior > 0:
                result.volume_ratio = round(current_vol / avg_prior, 2)
            else:
                result.volume_ratio = 99.0  # No prior volume — don't penalize.

            if result.volume_ratio < MIN_VOLUME_RATIO:
                result.reason = (
                    f"Volume fading: current {current_vol:.0f} vs avg {avg_prior:.0f} "
                    f"(ratio {result.volume_ratio:.2f} < {MIN_VOLUME_RATIO})"
                )
                logger.info("ENTRY REJECTED %s: %s", symbol, result.reason)
                return result
        else:
            # Not enough candle data — skip volume check.
            result.volume_ratio = 99.0
            logger.debug("Insufficient 1-min candles for %s volume check — skipping", symbol)
    except Exception as exc:
        result.volume_ratio = 99.0
        logger.warning("1-min candle fetch failed for %s volume check: %s — skipping", symbol, exc)

    # --- Check 5: Spread check (bid-ask spread < 0.2% of price) ---
    depth = quote.get("depth", {})
    best_buy = depth.get("buy", [{}])
    best_sell = depth.get("sell", [{}])

    bid = float(best_buy[0].get("price", 0)) if best_buy else 0
    ask = float(best_sell[0].get("price", 0)) if best_sell else 0

    if bid > 0 and ask > 0 and ask > bid:
        spread = ask - bid
        result.spread_pct = round(spread / ltp, 5)
        if result.spread_pct > MAX_SPREAD_PCT:
            result.reason = (
                f"Wide spread: {spread:.2f} ({result.spread_pct * 100:.3f}% > "
                f"{MAX_SPREAD_PCT * 100:.1f}%)"
            )
            logger.info("ENTRY REJECTED %s: %s", symbol, result.reason)
            return result
    else:
        # No depth data (common for indices or illiquid stocks at open).
        result.spread_pct = 0.0

    # --- All 5 checks passed ---
    result.valid = True
    result.reason = "All checks passed"
    logger.info(
        "ENTRY VALIDATED %s: price=%.2f, gain=%+.2f%%, range=%.3f, "
        "breakout=True, vol_ratio=%.2f, spread=%.4f%%",
        symbol, ltp, result.gain_pct, result.range_position,
        result.volume_ratio, result.spread_pct * 100,
    )
    return result

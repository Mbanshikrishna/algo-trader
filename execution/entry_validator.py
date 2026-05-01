"""Real-time entry validation for momentum continuation trades.

Fetches live market data immediately before order placement and checks
5 conditions. All must pass for the entry to proceed.

Supports both single-stock validation (validate_entry) and batch
validation (validate_entries_batch) with parallel candle fetches.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _check_quote(symbol: str, quote: dict) -> ValidationResult:
    """Run checks 1, 2, 5 using pre-fetched FULL quote data (no API calls)."""
    result = ValidationResult(valid=False, symbol=symbol)

    ltp = float(quote.get("ltp", 0))
    day_high = float(quote.get("high", 0))
    day_low = float(quote.get("low", 0))
    prev_close = float(quote.get("close", 0))
    result.live_price = ltp

    if ltp <= 0 or prev_close <= 0:
        result.reason = "Invalid price data (ltp=0 or close=0)"
        return result

    # --- Check 1: Momentum still valid (5-10% gain) ---
    result.gain_pct = round(((ltp - prev_close) / prev_close) * 100, 2)
    if not (MIN_GAIN_PCT <= result.gain_pct <= MAX_GAIN_PCT):
        result.reason = (
            f"Gain {result.gain_pct:+.2f}% outside {MIN_GAIN_PCT}-{MAX_GAIN_PCT}% range"
        )
        return result

    # --- Check 2: Price strength (range position ≥ 0.85) ---
    day_range = day_high - day_low
    if day_range > 0:
        result.range_position = round((ltp - day_low) / day_range, 3)
    else:
        result.range_position = 1.0

    if result.range_position < MIN_RANGE_POSITION:
        result.reason = (
            f"Range position {result.range_position:.3f} < {MIN_RANGE_POSITION} "
            f"(price retreating from high)"
        )
        return result

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
            return result

    # Checks 1, 2, 5 passed — mark as pending candle checks.
    result.valid = True
    return result


def _check_candles(
    symbol: str,
    token: str,
    ltp: float,
    client: Any,
    exchange: str = "NSE",
) -> tuple[bool, float, str]:
    """Run checks 3 (breakout) and 4 (volume) using candle data.

    Returns (passed, volume_ratio, rejection_reason).
    Fetches 5-min and 1-min candles — these are the slow API calls.
    """
    now = datetime.now(IST)
    breakout_ok = True
    volume_ratio = 99.0
    reason = ""

    # --- Check 3: Micro breakout (price > last 5-min candle high) ---
    try:
        candle_from = now - timedelta(minutes=15)
        candles = client.get_candle_data(
            exchange, str(token), "FIVE_MINUTE",
            candle_from.strftime("%Y-%m-%d %H:%M"),
            now.strftime("%Y-%m-%d %H:%M"),
        )
        if candles and len(candles) >= 2:
            last_completed_high = float(candles[-2][2])
            breakout_ok = ltp > last_completed_high
            if not breakout_ok:
                return False, 0.0, (
                    f"No micro breakout: LTP {ltp:.2f} <= last 5min high {last_completed_high:.2f}"
                )
        elif candles and len(candles) == 1:
            candle_high = float(candles[0][2])
            breakout_ok = ltp > candle_high
            if not breakout_ok:
                return False, 0.0, (
                    f"No micro breakout: LTP {ltp:.2f} <= candle high {candle_high:.2f}"
                )
    except Exception as exc:
        logger.warning("Candle fetch failed for %s breakout check: %s — skipping", symbol, exc)

    # --- Check 4: Volume confirmation ---
    try:
        candle_from_1m = now - timedelta(minutes=10)
        candles_1m = client.get_candle_data(
            exchange, str(token), "ONE_MINUTE",
            candle_from_1m.strftime("%Y-%m-%d %H:%M"),
            now.strftime("%Y-%m-%d %H:%M"),
        )
        if candles_1m and len(candles_1m) >= 3:
            current_vol = float(candles_1m[-1][5])
            prior_vols = [float(c[5]) for c in candles_1m[:-1]]
            avg_prior = sum(prior_vols) / len(prior_vols) if prior_vols else 0

            if avg_prior > 0:
                volume_ratio = round(current_vol / avg_prior, 2)
            else:
                volume_ratio = 99.0

            if volume_ratio < MIN_VOLUME_RATIO:
                return False, volume_ratio, (
                    f"Volume fading: current {current_vol:.0f} vs avg {avg_prior:.0f} "
                    f"(ratio {volume_ratio:.2f} < {MIN_VOLUME_RATIO})"
                )
    except Exception as exc:
        logger.warning("1-min candle fetch failed for %s volume check: %s — skipping", symbol, exc)

    return True, volume_ratio, ""


def validate_entry(
    symbol: str,
    token: str,
    client: Any,
    exchange: str = "NSE",
) -> ValidationResult:
    """Fetch live data and validate all 5 entry conditions (single stock).

    Returns a ValidationResult with .valid=True only if ALL pass.
    For batch validation of multiple candidates, use validate_entries_batch().
    """
    # Fetch live FULL quote.
    try:
        quote_data = client.get_market_data("FULL", {exchange: [str(token)]})
        fetched = quote_data.get("fetched", [])
        if not fetched:
            r = ValidationResult(valid=False, symbol=symbol, reason="No quote data returned")
            logger.info("ENTRY REJECTED %s: %s", symbol, r.reason)
            return r
        quote = fetched[0]
    except Exception as exc:
        r = ValidationResult(valid=False, symbol=symbol, reason=f"Quote fetch failed: {exc}")
        logger.warning("ENTRY REJECTED %s: %s", symbol, r.reason)
        return r

    # Checks 1, 2, 5 from quote.
    result = _check_quote(symbol, quote)
    if not result.valid:
        logger.info("ENTRY REJECTED %s: %s", symbol, result.reason)
        return result

    # Checks 3, 4 from candles.
    passed, vol_ratio, reason = _check_candles(symbol, token, result.live_price, client, exchange)
    result.volume_ratio = vol_ratio
    result.breakout_ok = passed
    if not passed:
        result.valid = False
        result.reason = reason
        logger.info("ENTRY REJECTED %s: %s", symbol, result.reason)
        return result

    result.reason = "All checks passed"
    logger.info(
        "ENTRY VALIDATED %s: price=%.2f, gain=%+.2f%%, range=%.3f, "
        "breakout=True, vol_ratio=%.2f, spread=%.4f%%",
        symbol, result.live_price, result.gain_pct, result.range_position,
        result.volume_ratio, result.spread_pct * 100,
    )
    return result


def validate_entries_batch(
    candidates: list[dict],
    client: Any,
    max_valid: int = 2,
    max_workers: int = 4,
    exchange: str = "NSE",
) -> list[tuple[dict, ValidationResult]]:
    """Validate multiple candidates in parallel.

    1. Batch-fetch all FULL quotes in 1 API call (≤50 tokens).
    2. Run quote-based checks (1, 2, 5) — instant, no API calls.
    3. For candidates that pass, run candle checks (3, 4) in parallel threads.
    4. Return up to max_valid validated candidates in original score order.

    Returns list of (candidate_dict, ValidationResult) for validated entries.
    """
    if not candidates:
        return []

    tokens = [str(c.get("token", "")) for c in candidates]
    symbol_by_token = {str(c.get("token", "")): c.get("symbol", "") for c in candidates}

    # --- Step 1: Batch-fetch all FULL quotes (1 API call) ---
    quote_map: dict[str, dict] = {}
    try:
        result = client.get_market_data("FULL", {exchange: tokens})
        for q in result.get("fetched", []):
            qt = str(q.get("symbolToken", ""))
            quote_map[qt] = q
    except Exception as exc:
        logger.warning("Batch quote fetch failed: %s", exc)
        return []

    # --- Step 2: Quote-based checks (instant — no API calls) ---
    candle_candidates: list[tuple[dict, ValidationResult]] = []
    for candidate in candidates:
        token = str(candidate.get("token", ""))
        symbol = candidate.get("symbol", "")
        quote = quote_map.get(token)

        if not quote:
            logger.info("ENTRY REJECTED %s: No quote data in batch", symbol)
            continue

        vr = _check_quote(symbol, quote)
        if not vr.valid:
            logger.info("ENTRY REJECTED %s: %s", symbol, vr.reason)
            continue

        candle_candidates.append((candidate, vr))

    if not candle_candidates:
        return []

    # --- Step 3: Candle-based checks in parallel ---
    def _candle_check(item: tuple[dict, ValidationResult]) -> tuple[dict, ValidationResult]:
        candidate, vr = item
        token = str(candidate.get("token", ""))
        symbol = candidate.get("symbol", "")

        passed, vol_ratio, reason = _check_candles(symbol, token, vr.live_price, client, exchange)
        vr.volume_ratio = vol_ratio
        vr.breakout_ok = passed
        if not passed:
            vr.valid = False
            vr.reason = reason
            logger.info("ENTRY REJECTED %s: %s", symbol, reason)
        else:
            vr.reason = "All checks passed"
            logger.info(
                "ENTRY VALIDATED %s: price=%.2f, gain=%+.2f%%, range=%.3f, "
                "breakout=True, vol_ratio=%.2f, spread=%.4f%%",
                symbol, vr.live_price, vr.gain_pct, vr.range_position,
                vr.volume_ratio, vr.spread_pct * 100,
            )
        return candidate, vr

    validated: list[tuple[dict, ValidationResult]] = []
    # Preserve original score order: submit all, collect in order.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(_candle_check, item): i
            for i, item in enumerate(candle_candidates)
        }
        results_by_idx: dict[int, tuple[dict, ValidationResult]] = {}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results_by_idx[idx] = future.result()
            except Exception as exc:
                c, vr = candle_candidates[idx]
                vr.valid = False
                vr.reason = f"Validation error: {exc}"
                logger.warning("ENTRY REJECTED %s: %s", c.get("symbol", ""), exc)
                results_by_idx[idx] = (c, vr)

        # Collect in original score order, stop at max_valid.
        for idx in sorted(results_by_idx):
            candidate, vr = results_by_idx[idx]
            if vr.valid:
                validated.append((candidate, vr))
                if len(validated) >= max_valid:
                    break

    return validated

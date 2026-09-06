"""Real-time entry validation for momentum continuation trades.

Fetches live market data immediately before order placement and checks
5 conditions. All must pass for the entry to proceed.

Supports both single-stock validation (validate_entry) and batch
validation (validate_entries_batch) with parallel candle fetches.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
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

# Observation-derived policy is recorded alongside the active decision.  It is
# deliberately shadow-only until it has enough out-of-sample winners and
# losers to justify changing order flow.
SHADOW_MIN_RANGE_POSITION = 0.95
SHADOW_PERSISTENCE_BARS = 2
SHADOW_RETEST_TOLERANCE_PCT = 0.002


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


def _candle_start(candle: list) -> datetime:
    value = str(candle[0]).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)


def _completed_candles(
    candles: list[list], now: datetime, duration_minutes: int
) -> list[list]:
    """Return only candles whose full duration elapsed before ``now``."""
    return [
        candle
        for candle in candles
        if _candle_start(candle) + timedelta(minutes=duration_minutes) <= now
    ]


def _shadow_breakout_confirmation(
    completed_minutes: list[list],
) -> tuple[bool, float, str]:
    """Evaluate persistence/retest using complete one-minute bars only."""
    buckets: dict[datetime, list[list]] = {}
    for candle in completed_minutes:
        started = _candle_start(candle)
        bucket = started.replace(minute=(started.minute // 5) * 5, second=0, microsecond=0)
        buckets.setdefault(bucket, []).append(candle)
    full_buckets = [
        (started, sorted(rows, key=_candle_start))
        for started, rows in buckets.items()
        if len({_candle_start(row).minute for row in rows}) == 5
    ]
    if not full_buckets:
        return False, 0.0, "no completed five-minute breakout bar"

    bucket_start, breakout_rows = max(full_buckets, key=lambda item: item[0])
    breakout_level = max(float(candle[2]) for candle in breakout_rows)
    available_at = bucket_start + timedelta(minutes=5)
    confirmation = [
        candle for candle in completed_minutes if _candle_start(candle) >= available_at
    ]
    if len(confirmation) >= SHADOW_PERSISTENCE_BARS and all(
        float(candle[4]) >= breakout_level
        for candle in confirmation[-SHADOW_PERSISTENCE_BARS:]
    ):
        return True, breakout_level, "persistent"

    lower = breakout_level * (1 - SHADOW_RETEST_TOLERANCE_PCT)
    upper = breakout_level * (1 + SHADOW_RETEST_TOLERANCE_PCT)
    breakout_seen = False
    for candle in confirmation:
        low = float(candle[3])
        close = float(candle[4])
        if breakout_seen and lower <= low <= upper and close >= breakout_level:
            return True, breakout_level, "retest"
        breakout_seen = breakout_seen or close > breakout_level
    return False, breakout_level, "neither persistence nor successful retest"


def evaluate_shadow_entry_policy(
    result: ValidationResult,
    captured_candles: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate stricter observations without affecting the active decision."""
    evaluated_at = now or datetime.now(IST)
    range_pass = result.range_position >= SHADOW_MIN_RANGE_POSITION
    complete_minutes = _completed_candles(
        captured_candles.get("volume_candles", []), evaluated_at, 1
    )
    prior = complete_minutes[-6:-1]
    prior_average = (
        sum(float(candle[5]) for candle in prior) / len(prior) if prior else 0.0
    )
    volume_ratio = (
        float(complete_minutes[-1][5]) / prior_average
        if complete_minutes and prior_average > 0
        else 0.0
    )
    volume_pass = len(prior) >= 2 and volume_ratio >= MIN_VOLUME_RATIO
    breakout_pass, breakout_level, breakout_path = _shadow_breakout_confirmation(
        complete_minutes
    )
    enough_data = len(prior) >= 2 and breakout_level > 0
    accepted = range_pass and volume_pass and breakout_pass and enough_data
    return {
        "policy": "range95_completed_volume_persistence_or_retest_v1",
        "decision": (
            "accepted"
            if accepted
            else "rejected"
            if not range_pass or enough_data
            else "insufficient_data"
        ),
        "range_position": result.range_position,
        "range_pass": range_pass,
        "completed_volume_ratio": round(volume_ratio, 4),
        "completed_volume_pass": volume_pass,
        "completed_minute_count": len(complete_minutes),
        "breakout_level": round(breakout_level, 4),
        "breakout_pass": breakout_pass,
        "breakout_path": breakout_path,
    }


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
    *,
    gain_pct: float = 0.0,
    range_position: float = 0.0,
    capture: dict[str, Any] | None = None,
) -> tuple[bool, float, str]:
    """Run checks 3 (breakout) and 4 (volume) using candle data.

    Returns (passed, volume_ratio, rejection_reason).
    Fetches 5-min and 1-min candles — these are the slow API calls.
    """
    now = datetime.now(IST)
    breakout_ok = True
    volume_ratio = 0.0

    # Near-breakout: skip micro breakout check when stock is at the top of
    # its range (>= 0.85) with moderate gain (5-7%).  These stocks are in
    # strong position but pulled back one tick from the candle high.
    near_breakout = range_position >= MIN_RANGE_POSITION and 5.0 <= gain_pct < 7.0

    # --- Check 3: Micro breakout (price > last 5-min candle high) ---
    if near_breakout:
        logger.info("Near-breakout skip for %s: range=%.3f, gain=%.1f%% — skipping breakout check",
                     symbol, range_position, gain_pct)
    else:
        try:
            candle_from = now - timedelta(minutes=15)
            candles = client.get_candle_data(
                exchange, str(token), "FIVE_MINUTE",
                candle_from.strftime("%Y-%m-%d %H:%M"),
                now.strftime("%Y-%m-%d %H:%M"),
            )
            if capture is not None:
                capture["breakout_candles"] = candles
            if candles and len(candles) >= 2:
                last_completed_high = float(candles[-2][2])
                breakout_ok = ltp >= last_completed_high
                if not breakout_ok:
                    return False, 0.0, (
                        f"No micro breakout: LTP {ltp:.2f} < last 5min high {last_completed_high:.2f}"
                    )
            elif candles and len(candles) == 1:
                candle_high = float(candles[0][2])
                breakout_ok = ltp >= candle_high
                if not breakout_ok:
                    return False, 0.0, (
                        f"No micro breakout: LTP {ltp:.2f} < candle high {candle_high:.2f}"
                    )
        except Exception as exc:
            if capture is not None:
                capture["breakout_error"] = str(exc)
            logger.warning("Candle fetch failed for %s breakout check: %s — rejecting", symbol, exc)
            return False, 0.0, f"Breakout check failed: candle fetch error ({exc})"

    # --- Check 4: Volume confirmation ---
    try:
        candle_from_1m = now - timedelta(minutes=10)
        candles_1m = client.get_candle_data(
            exchange, str(token), "ONE_MINUTE",
            candle_from_1m.strftime("%Y-%m-%d %H:%M"),
            now.strftime("%Y-%m-%d %H:%M"),
        )
        if capture is not None:
            capture["volume_candles"] = candles_1m
        if candles_1m and len(candles_1m) >= 3:
            current_vol = float(candles_1m[-1][5])
            prior_vols = [float(c[5]) for c in candles_1m[:-1]]
            avg_prior = sum(prior_vols) / len(prior_vols) if prior_vols else 0

            if avg_prior > 0:
                volume_ratio = round(current_vol / avg_prior, 2)
            else:
                # No prior volume to compare — can't confirm momentum.
                return False, 0.0, "Volume check inconclusive: no prior volume data"

            if volume_ratio < MIN_VOLUME_RATIO:
                return False, volume_ratio, (
                    f"Volume fading: current {current_vol:.0f} vs avg {avg_prior:.0f} "
                    f"(ratio {volume_ratio:.2f} < {MIN_VOLUME_RATIO})"
                )
        else:
            # Not enough candles to evaluate volume.
            return False, 0.0, (
                f"Volume check failed: need >=3 candles, got {len(candles_1m) if candles_1m else 0}"
            )
    except Exception as exc:
        if capture is not None:
            capture["volume_error"] = str(exc)
        logger.warning("1-min candle fetch failed for %s volume check: %s — rejecting", symbol, exc)
        return False, 0.0, f"Volume check failed: candle fetch error ({exc})"

    return True, volume_ratio, ""


def validate_entry(
    symbol: str,
    token: str,
    client: Any,
    exchange: str = "NSE",
    event_sink: Callable[..., Any] | None = None,
    strict_policy_enabled: bool = False,
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
        if event_sink is not None:
            event_sink(
                "candidate_validation",
                symbol=symbol,
                token=token,
                decision="rejected",
                reason=r.reason,
                payload={"quote": {}, "candles": {}, "result": asdict(r)},
            )
        return r

    # Checks 1, 2, 5 from quote.
    result = _check_quote(symbol, quote)
    if not result.valid:
        logger.info("ENTRY REJECTED %s: %s", symbol, result.reason)
        if event_sink is not None:
            event_sink(
                "candidate_validation",
                symbol=symbol,
                token=token,
                decision="rejected",
                reason=result.reason,
                exchange_timestamp=str(quote.get("exchFeedTime", "")),
                payload={"quote": quote, "candles": {}, "result": asdict(result)},
            )
        return result

    # Checks 3, 4 from candles.
    captured_candles: dict[str, Any] = {}
    passed, vol_ratio, reason = _check_candles(
        symbol, token, result.live_price, client, exchange,
        gain_pct=result.gain_pct, range_position=result.range_position,
        capture=captured_candles,
    )
    shadow_policy = evaluate_shadow_entry_policy(
        result, captured_candles
    )
    captured_candles["shadow_entry_policy"] = shadow_policy
    captured_candles["strict_policy_enabled"] = strict_policy_enabled
    result.volume_ratio = vol_ratio
    result.breakout_ok = passed
    if not passed:
        result.valid = False
        result.reason = reason
    elif strict_policy_enabled and shadow_policy["decision"] != "accepted":
        result.valid = False
        result.reason = (
            "Strict entry policy rejected: "
            f"range_pass={shadow_policy['range_pass']}, "
            f"completed_volume_pass={shadow_policy['completed_volume_pass']}, "
            f"breakout_pass={shadow_policy['breakout_pass']} "
            f"({shadow_policy['breakout_path']})"
        )
    if not result.valid:
        logger.info("ENTRY REJECTED %s: %s", symbol, result.reason)
        if event_sink is not None:
            event_sink(
                "candidate_validation",
                symbol=symbol,
                token=token,
                decision="rejected",
                reason=result.reason,
                exchange_timestamp=str(quote.get("exchFeedTime", "")),
                payload={
                    "quote": quote,
                    "candles": captured_candles,
                    "result": asdict(result),
                },
            )
        return result

    result.reason = "All checks passed"
    logger.info(
        "ENTRY VALIDATED %s: price=%.2f, gain=%+.2f%%, range=%.3f, "
        "breakout=True, vol_ratio=%.2f, spread=%.4f%%",
        symbol, result.live_price, result.gain_pct, result.range_position,
        result.volume_ratio, result.spread_pct * 100,
    )
    if event_sink is not None:
        event_sink(
            "candidate_validation",
            symbol=symbol,
            token=token,
            decision="accepted",
            reason=result.reason,
            exchange_timestamp=str(quote.get("exchFeedTime", "")),
            payload={
                "quote": quote,
                "candles": captured_candles,
                "result": asdict(result),
            },
        )
    return result


def validate_entries_batch(
    candidates: list[dict],
    client: Any,
    max_valid: int = 2,
    max_workers: int = 4,
    exchange: str = "NSE",
    event_sink: Callable[..., Any] | None = None,
    strict_policy_enabled: bool = False,
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

    # --- Step 1: Batch-fetch all FULL quotes (1 API call) ---
    quote_map: dict[str, dict] = {}
    try:
        result = client.get_market_data("FULL", {exchange: tokens})
        for q in result.get("fetched", []):
            qt = str(q.get("symbolToken", ""))
            quote_map[qt] = q
    except Exception as exc:
        logger.warning("Batch quote fetch failed: %s", exc)
        if event_sink is not None:
            event_sink(
                "validation_batch_failed",
                decision="reject_all",
                reason=str(exc),
                payload={"candidates": candidates, "tokens": tokens},
            )
        return []

    # --- Step 2: Quote-based checks (instant — no API calls) ---
    candle_candidates: list[tuple[dict, ValidationResult, dict]] = []
    for candidate in candidates:
        token = str(candidate.get("token", ""))
        symbol = candidate.get("symbol", "")
        quote = quote_map.get(token)

        if not quote:
            logger.info("ENTRY REJECTED %s: No quote data in batch", symbol)
            if event_sink is not None:
                event_sink(
                    "candidate_validation",
                    symbol=symbol,
                    token=token,
                    decision="rejected",
                    reason="No quote data in batch",
                    payload={"candidate": candidate, "quote": {}, "candles": {}},
                )
            continue

        vr = _check_quote(symbol, quote)
        if not vr.valid:
            logger.info("ENTRY REJECTED %s: %s", symbol, vr.reason)
            if event_sink is not None:
                event_sink(
                    "candidate_validation",
                    symbol=symbol,
                    token=token,
                    decision="rejected",
                    reason=vr.reason,
                    exchange_timestamp=str(quote.get("exchFeedTime", "")),
                    payload={
                        "candidate": candidate,
                        "quote": quote,
                        "candles": {},
                        "result": asdict(vr),
                    },
                )
            continue

        candle_candidates.append((candidate, vr, quote))

    if not candle_candidates:
        return []

    # --- Step 3: Candle-based checks in parallel ---
    def _candle_check(
        item: tuple[dict, ValidationResult, dict],
    ) -> tuple[dict, ValidationResult]:
        candidate, vr, quote = item
        token = str(candidate.get("token", ""))
        symbol = candidate.get("symbol", "")
        captured_candles: dict[str, Any] = {}

        passed, vol_ratio, reason = _check_candles(
            symbol, token, vr.live_price, client, exchange,
            gain_pct=vr.gain_pct, range_position=vr.range_position,
            capture=captured_candles,
        )
        shadow_policy = evaluate_shadow_entry_policy(
            vr, captured_candles
        )
        captured_candles["shadow_entry_policy"] = shadow_policy
        captured_candles["strict_policy_enabled"] = strict_policy_enabled
        vr.volume_ratio = vol_ratio
        vr.breakout_ok = passed
        if not passed:
            vr.valid = False
            vr.reason = reason
            logger.info("ENTRY REJECTED %s: %s", symbol, reason)
        elif strict_policy_enabled and shadow_policy["decision"] != "accepted":
            vr.valid = False
            vr.reason = (
                "Strict entry policy rejected: "
                f"range_pass={shadow_policy['range_pass']}, "
                f"completed_volume_pass={shadow_policy['completed_volume_pass']}, "
                f"breakout_pass={shadow_policy['breakout_pass']} "
                f"({shadow_policy['breakout_path']})"
            )
            logger.info("ENTRY REJECTED %s: %s", symbol, vr.reason)
        else:
            vr.reason = "All checks passed"
            logger.info(
                "ENTRY VALIDATED %s: price=%.2f, gain=%+.2f%%, range=%.3f, "
                "breakout=True, vol_ratio=%.2f, spread=%.4f%%",
                symbol, vr.live_price, vr.gain_pct, vr.range_position,
                vr.volume_ratio, vr.spread_pct * 100,
            )
        if event_sink is not None:
            event_sink(
                "candidate_validation",
                symbol=symbol,
                token=token,
                decision="accepted" if vr.valid else "rejected",
                reason=vr.reason,
                exchange_timestamp=str(quote.get("exchFeedTime", "")),
                payload={
                    "candidate": candidate,
                    "quote": quote,
                    "candles": captured_candles,
                    "result": asdict(vr),
                },
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
                c, vr, quote = candle_candidates[idx]
                vr.valid = False
                vr.reason = f"Validation error: {exc}"
                logger.warning("ENTRY REJECTED %s: %s", c.get("symbol", ""), exc)
                if event_sink is not None:
                    event_sink(
                        "candidate_validation",
                        symbol=str(c.get("symbol", "")),
                        token=str(c.get("token", "")),
                        decision="rejected",
                        reason=vr.reason,
                        exchange_timestamp=str(quote.get("exchFeedTime", "")),
                        payload={
                            "candidate": c,
                            "quote": quote,
                            "candles": {},
                            "result": asdict(vr),
                        },
                    )
                results_by_idx[idx] = (c, vr)

        # Collect in original score order, stop at max_valid.
        for idx in sorted(results_by_idx):
            candidate, vr = results_by_idx[idx]
            if vr.valid:
                validated.append((candidate, vr))
                if len(validated) >= max_valid:
                    break

    return validated

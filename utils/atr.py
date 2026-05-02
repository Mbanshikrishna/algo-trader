"""Compute Average True Range (ATR) from candle data.

Used at entry time to set ATR-adaptive stop distances.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("algo_trader")

IST = ZoneInfo("Asia/Kolkata")

# Default ATR parameters.
ATR_CANDLE_COUNT = 12   # Number of 5-min candles to use for ATR.
ATR_INTERVAL = "FIVE_MINUTE"
# Fallback ATR as percentage of price when candle fetch fails.
FALLBACK_ATR_PCT = 0.005  # 0.5% of entry price.


def compute_atr_from_candles(candles: list[list]) -> float:
    """Compute ATR from Angel One candle data.

    Each candle is [timestamp, open, high, low, close, volume].
    ATR = average of True Range over the candle set.
    True Range = max(high-low, |high-prev_close|, |low-prev_close|).
    """
    if not candles or len(candles) < 2:
        return 0.0

    true_ranges: list[float] = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i - 1][4])

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        true_ranges.append(tr)

    if not true_ranges:
        return 0.0

    return sum(true_ranges) / len(true_ranges)


def fetch_entry_atr(
    client: Any,
    token: str,
    entry_price: float,
    exchange: str = "NSE",
) -> float:
    """Fetch recent 5-min candles and compute ATR for stop-loss sizing.

    Falls back to a percentage of entry price if the API call fails.
    """
    now = datetime.now(IST)
    candle_from = now - timedelta(minutes=ATR_CANDLE_COUNT * 5 + 10)  # Extra buffer.

    try:
        candles = client.get_candle_data(
            exchange, str(token), ATR_INTERVAL,
            candle_from.strftime("%Y-%m-%d %H:%M"),
            now.strftime("%Y-%m-%d %H:%M"),
        )
        if candles and len(candles) >= 3:
            atr = compute_atr_from_candles(candles)
            if atr > 0:
                logger.info(
                    "ATR computed for token %s: %.4f (from %d candles, price=%.2f, ATR%%=%.2f%%)",
                    token, atr, len(candles), entry_price, (atr / entry_price) * 100,
                )
                return atr
    except Exception as exc:
        logger.warning("ATR candle fetch failed for token %s: %s", token, exc)

    # Fallback: use percentage of entry price.
    fallback = entry_price * FALLBACK_ATR_PCT
    logger.warning(
        "Using fallback ATR for token %s: %.4f (%.2f%% of %.2f)",
        token, fallback, FALLBACK_ATR_PCT * 100, entry_price,
    )
    return fallback

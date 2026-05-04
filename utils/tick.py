"""NSE tick-size rounding utility."""
from __future__ import annotations

import math

# NSE tick size — all prices must be multiples of this value.
NSE_TICK_SIZE = 0.05


def tick_round(price: float, direction: str = "down") -> float:
    """Round a price to the nearest NSE tick (₹0.05).

    direction='down' floors the price (conservative for SL triggers / sell limits).
    direction='up' ceils the price (conservative for buy limits).
    direction='nearest' rounds to the closest tick.
    """
    if direction == "up":
        return round(math.ceil(price / NSE_TICK_SIZE) * NSE_TICK_SIZE, 2)
    elif direction == "down":
        return round(math.floor(price / NSE_TICK_SIZE) * NSE_TICK_SIZE, 2)
    return round(round(price / NSE_TICK_SIZE) * NSE_TICK_SIZE, 2)

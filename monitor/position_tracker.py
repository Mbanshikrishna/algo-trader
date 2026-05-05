"""ATR-adaptive trailing stop with intraday gain-based profit lock.

Stop distances scale with each stock's 5-minute ATR. When the stock's
intraday gain (from previous close) reaches 15%, the stop tightens
sharply so the position exits in the 17-19% intraday gain range.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# --- ATR-based stop configuration ---
INITIAL_ATR_MULT = 3.0       # Initial stop distance: 3x ATR below entry.
HARD_MAX_LOSS_PCT = 0.02     # Absolute max loss per stock: 2% below entry.
MIN_STOP_DISTANCE_PCT = 0.005  # Stop never tighter than 0.5% (avoid instant stop-out).

# Profit-tiered trail multipliers: as profit grows, trail tightens.
# (min_profit_pct, atr_multiplier)
TRAIL_TIERS = [
    (0.10, 1.0),   # >10% profit: very tight, 1x ATR trail.
    (0.06, 1.5),   # 6-10% profit: tight, 1.5x ATR trail.
    (0.03, 2.0),   # 3-6% profit: moderate, 2x ATR trail.
    (0.00, 2.5),   # <3% profit: default, 2.5x ATR trail.
]

# Time-based tightening: after this hour (IST), reduce all multipliers.
LATE_SESSION_HOUR = 14
LATE_SESSION_MIN = 30
LATE_SESSION_MULT_REDUCTION = 0.5

# --- Intraday gain-based profit lock ---
# Triggered by the stock's gain from previous close, NOT profit from entry.
# Example: enter at +5%, stock hits +15% for the day → lock activates.
INTRADAY_LOCK_THRESHOLD = 0.12   # Lock when stock's intraday gain >= 12%.
INTRADAY_LOCK_FLOOR_PCT = 0.12   # Stop never below prev_close * 1.12 once locked.
INTRADAY_LOCK_TRAIL_PCT = 0.01   # Trail at 1% of highest price inside lock zone.


@dataclass
class Position:
    """One tracked position with ATR-adaptive stop."""

    symbol: str
    quantity: int
    average_price: float       # Our entry price.
    atr: float                 # 5-minute ATR at entry time (fixed for the trade).
    prev_close: float          # Stock's previous day close — used for intraday gain calc.
    product_type: str = "INTRADAY"  # "INTRADAY" (MIS, 5x) or "DELIVERY" (CNC, 1x).
    highest_price: float = 0.0
    stop_loss: float = 0.0
    hard_stop: float = 0.0
    profit_locked: bool = False  # True once intraday gain crosses INTRADAY_LOCK_THRESHOLD.

    def __post_init__(self) -> None:
        if self.highest_price == 0.0:
            self.highest_price = self.average_price
        if self.stop_loss == 0.0:
            self._set_initial_stop()
        if self.hard_stop == 0.0:
            self._set_hard_stop()

    def _set_initial_stop(self) -> None:
        """Set initial stop at entry - INITIAL_ATR_MULT x ATR, clamped."""
        atr_distance = self.atr * INITIAL_ATR_MULT
        pct_distance = self.average_price * HARD_MAX_LOSS_PCT
        min_distance = self.average_price * MIN_STOP_DISTANCE_PCT

        distance = max(min(atr_distance, pct_distance), min_distance)
        self.stop_loss = round(self.average_price - distance, 2)

    def _set_hard_stop(self) -> None:
        """Hard stop: tighter of ATR-based and percentage-based."""
        atr_hard = self.average_price - (self.atr * INITIAL_ATR_MULT)
        pct_hard = self.average_price * (1 - HARD_MAX_LOSS_PCT)
        self.hard_stop = round(max(atr_hard, pct_hard), 2)

    def intraday_gain_pct(self, current_price: float) -> float:
        """Stock's intraday gain from previous close."""
        if self.prev_close <= 0:
            return 0.0
        return (current_price - self.prev_close) / self.prev_close


class PositionTracker:
    """In-memory position tracker with ATR-adaptive trailing stops."""

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}

    def update_buy(
        self, symbol: str, quantity: int, price: float,
        atr: float, prev_close: float, product_type: str = "INTRADAY",
    ) -> Position:
        """Record a buy. ATR and prev_close are computed at entry time."""
        existing = self._positions.get(symbol)
        if not existing:
            pos = Position(
                symbol=symbol, quantity=quantity,
                average_price=price, atr=atr, prev_close=prev_close,
                product_type=product_type,
            )
            self._positions[symbol] = pos
            return pos

        # Scale-in: recalculate weighted average, keep original ATR and prev_close.
        new_qty = existing.quantity + quantity
        weighted_avg = (
            (existing.average_price * existing.quantity) + (price * quantity)
        ) / new_qty
        existing.quantity = new_qty
        existing.average_price = weighted_avg
        existing.highest_price = max(existing.highest_price, price)

        new_stop = self._compute_trail_stop(existing)
        if new_stop > existing.stop_loss:
            existing.stop_loss = new_stop
        existing._set_hard_stop()
        return existing

    def update_sell(self, symbol: str, quantity: int) -> Position | None:
        """Record a sell (full exit)."""
        existing = self._positions.get(symbol)
        if not existing:
            return None
        if quantity > existing.quantity:
            raise ValueError(
                f"Cannot sell {quantity} shares of {symbol}: only {existing.quantity} held"
            )
        existing.quantity -= quantity
        if existing.quantity == 0:
            del self._positions[symbol]
            return None
        return existing

    def update_trailing_stop(self, symbol: str, current_price: float) -> Position | None:
        """Update trailing stop based on current price, ATR tiers, and intraday lock."""
        existing = self._positions.get(symbol)
        if not existing:
            return None

        if current_price > existing.highest_price:
            existing.highest_price = current_price

        new_stop = self._compute_trail_stop(existing)
        if new_stop > existing.stop_loss:
            existing.stop_loss = new_stop

        return existing

    def should_exit(self, symbol: str, current_price: float) -> bool:
        """Check if price has breached trailing stop or hard stop."""
        existing = self._positions.get(symbol)
        if not existing:
            return False
        return current_price <= existing.stop_loss or current_price <= existing.hard_stop

    def get_exit_reason(self, symbol: str, current_price: float) -> str:
        """Return a descriptive exit reason for logging."""
        existing = self._positions.get(symbol)
        if not existing:
            return "NO_POSITION"
        if current_price <= existing.hard_stop:
            return "HARD STOP (ATR-based max loss)"
        if current_price <= existing.stop_loss:
            if existing.profit_locked:
                intraday = existing.intraday_gain_pct(current_price) * 100
                return f"PROFIT LOCK EXIT (stock at +{intraday:.1f}% intraday)"
            profit_pct = (existing.highest_price - existing.average_price) / existing.average_price
            tier_label = self._get_tier_label(profit_pct)
            return f"TRAILING STOP ({tier_label})"
        return "NO_EXIT"

    def snapshot(self) -> dict[str, Position]:
        return dict(self._positions)

    # --- Internal helpers ---

    def _compute_trail_stop(self, pos: Position) -> float:
        """Compute trailing stop. Switches to lock mode when intraday gain hits 15%."""
        intraday_gain = pos.intraday_gain_pct(pos.highest_price)

        if intraday_gain >= INTRADAY_LOCK_THRESHOLD:
            pos.profit_locked = True
            # Floor: stock's price at +15% from prev close.
            lock_floor = pos.prev_close * (1 + INTRADAY_LOCK_FLOOR_PCT)
            # Trail: 1% below highest price — tight enough to exit in 17-19% range.
            lock_trail = pos.highest_price * (1 - INTRADAY_LOCK_TRAIL_PCT)
            return round(max(lock_floor, lock_trail), 2)

        # Normal ATR-tiered trailing based on profit from entry.
        profit_pct = (pos.highest_price - pos.average_price) / pos.average_price
        mult = self._get_trail_multiplier(profit_pct)
        trail_distance = pos.atr * mult
        min_distance = pos.highest_price * MIN_STOP_DISTANCE_PCT
        trail_distance = max(trail_distance, min_distance)

        return round(pos.highest_price - trail_distance, 2)

    @staticmethod
    def _get_trail_multiplier(profit_pct: float) -> float:
        """Look up the ATR multiplier for the current profit level."""
        now = datetime.now(IST)
        late_session = (
            now.hour > LATE_SESSION_HOUR
            or (now.hour == LATE_SESSION_HOUR and now.minute >= LATE_SESSION_MIN)
        )

        for threshold, mult in TRAIL_TIERS:
            if profit_pct >= threshold:
                if late_session:
                    mult = max(mult - LATE_SESSION_MULT_REDUCTION, 0.5)
                return mult

        base = TRAIL_TIERS[-1][1]
        if late_session:
            base = max(base - LATE_SESSION_MULT_REDUCTION, 0.5)
        return base

    @staticmethod
    def _get_tier_label(profit_pct: float) -> str:
        """Human-readable label for the current trail tier."""
        if profit_pct >= 0.10:
            return "very tight, >10% profit"
        if profit_pct >= 0.06:
            return "tight, 6-10% profit"
        if profit_pct >= 0.03:
            return "moderate, 3-6% profit"
        return "default, <3% profit"

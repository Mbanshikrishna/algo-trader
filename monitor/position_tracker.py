from __future__ import annotations  # Lets Python postpone evaluation of type hints.

from dataclasses import dataclass  # Imports the dataclass decorator for compact position objects.


# Trailing stop configuration.
DEFAULT_TRAIL_PCT = 0.02  # Default trailing stop: 2% below highest price.
TIGHT_TRAIL_PCT = 0.015  # Tighter trailing stop: 1.5% after profit exceeds threshold.
TIGHT_TRAIL_PROFIT_THRESHOLD = 0.05  # Tighten the trail once profit exceeds 5%.
LOCK_PROFIT_THRESHOLD = 0.15  # Lock stop-loss at +15% once gain crosses 15%.
MAX_LOSS_PCT = 0.02  # Hard max loss per stock: 2% below entry price.


@dataclass  # Creates a simple mutable container for one tracked position.
class Position:  # Represents one symbol's current position state.
    symbol: str  # Stores the stock symbol.
    quantity: int  # Stores the total number of shares currently held.
    average_price: float  # Stores the weighted average entry price.
    highest_price: float = 0.0  # Tracks the highest price reached since entry.
    stop_loss: float = 0.0  # Stores the current trailing stop-loss level.
    hard_stop: float = 0.0  # Absolute floor: max 2% loss from entry, never changes.

    def __post_init__(self) -> None:  # Sets initial highest_price, stop_loss, and hard_stop from entry price.
        if self.highest_price == 0.0:
            self.highest_price = self.average_price
        if self.stop_loss == 0.0:
            self.stop_loss = round(self.highest_price * (1 - DEFAULT_TRAIL_PCT), 2)
        if self.hard_stop == 0.0:
            self.hard_stop = round(self.average_price * (1 - MAX_LOSS_PCT), 2)


class PositionTracker:  # Defines an in-memory tracker for open intraday positions.
    """In-memory position tracker for intraday session state."""

    def __init__(self) -> None:  # Initializes the tracker with no open positions.
        self._positions: dict[str, Position] = {}  # Stores positions keyed by symbol.

    def update_buy(self, symbol: str, quantity: int, price: float) -> Position:  # Updates the tracked position after a buy.
        existing = self._positions.get(symbol)  # Looks up any existing position for the symbol.
        if not existing:  # Handles the case where this is the first buy for the symbol.
            pos = Position(symbol=symbol, quantity=quantity, average_price=price)  # Creates a new position with trailing stop initialized.
            self._positions[symbol] = pos  # Saves the new position in the tracker.
            return pos  # Returns the newly created position.

        new_qty = existing.quantity + quantity  # Calculates the new total quantity after the buy.
        weighted_avg = ((existing.average_price * existing.quantity) + (price * quantity)) / new_qty  # Recalculates the weighted average entry price.
        existing.quantity = new_qty  # Updates the stored quantity.
        existing.average_price = weighted_avg  # Updates the stored average price.
        existing.highest_price = max(existing.highest_price, price)  # Keeps the tracked high aligned with the best price seen since the position was opened.

        profit_pct = (existing.highest_price - existing.average_price) / existing.average_price  # Recomputes profit using the new blended entry price.
        if profit_pct >= LOCK_PROFIT_THRESHOLD:
            new_stop = round(existing.average_price * (1 + LOCK_PROFIT_THRESHOLD), 2)
        else:
            trail_pct = TIGHT_TRAIL_PCT if profit_pct >= TIGHT_TRAIL_PROFIT_THRESHOLD else DEFAULT_TRAIL_PCT
            new_stop = round(existing.highest_price * (1 - trail_pct), 2)
        if new_stop > existing.stop_loss:
            existing.stop_loss = new_stop  # Never lets a scale-in lower the current stop-loss.
        return existing  # Returns the updated position.

    def update_sell(self, symbol: str, quantity: int) -> Position | None:  # Updates the tracked position after a sell.
        existing = self._positions.get(symbol)  # Looks up the current position for the symbol.
        if not existing:  # Handles the case where there is no position to reduce.
            return None  # Returns None because nothing can be updated.

        if quantity > existing.quantity:  # Prevents selling more shares than currently held.
            raise ValueError(
                f"Cannot sell {quantity} shares of {symbol}: only {existing.quantity} held"
            )

        existing.quantity -= quantity  # Reduces the held quantity by the sold amount.
        if existing.quantity == 0:  # Checks whether the position has been fully closed.
            del self._positions[symbol]  # Removes the position from the tracker once it is closed.
            return None  # Returns None because no open position remains.
        return existing  # Returns the updated remaining position.

    def update_trailing_stop(self, symbol: str, current_price: float) -> Position | None:  # Updates the trailing stop for an open position based on the current price.
        existing = self._positions.get(symbol)
        if not existing:
            return None

        # Update highest price (only moves up).
        if current_price > existing.highest_price:
            existing.highest_price = current_price

        # Choose trail percentage based on profit level.
        profit_pct = (existing.highest_price - existing.average_price) / existing.average_price

        if profit_pct >= LOCK_PROFIT_THRESHOLD:
            # Lock stop-loss at exactly +15% gain — guarantees at least 15% profit.
            new_stop = round(existing.average_price * (1 + LOCK_PROFIT_THRESHOLD), 2)
        elif profit_pct >= TIGHT_TRAIL_PROFIT_THRESHOLD:
            trail_pct = TIGHT_TRAIL_PCT
            new_stop = round(existing.highest_price * (1 - trail_pct), 2)
        else:
            trail_pct = DEFAULT_TRAIL_PCT
            new_stop = round(existing.highest_price * (1 - trail_pct), 2)

        # Only allow upward movement.
        if new_stop > existing.stop_loss:
            existing.stop_loss = new_stop

        return existing

    def should_exit(self, symbol: str, current_price: float) -> bool:  # Checks whether the current price has breached the trailing stop or the hard max-loss stop.
        existing = self._positions.get(symbol)
        if not existing:
            return False
        return current_price <= existing.stop_loss or current_price <= existing.hard_stop

    def snapshot(self) -> dict[str, Position]:  # Returns a copy of the current positions dictionary.
        return dict(self._positions)  # Creates a shallow copy so callers cannot replace the internal mapping directly.

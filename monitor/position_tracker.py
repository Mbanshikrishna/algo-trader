from __future__ import annotations  # Lets Python postpone evaluation of type hints.

from dataclasses import dataclass  # Imports the dataclass decorator for compact position objects.


@dataclass  # Creates a simple mutable container for one tracked position.
class Position:  # Represents one symbol's current position state.
    symbol: str  # Stores the stock symbol.
    quantity: int  # Stores the total number of shares currently held.
    average_price: float  # Stores the weighted average entry price.


class PositionTracker:  # Defines an in-memory tracker for open intraday positions.
    """In-memory position tracker for intraday session state."""

    def __init__(self) -> None:  # Initializes the tracker with no open positions.
        self._positions: dict[str, Position] = {}  # Stores positions keyed by symbol.

    def update_buy(self, symbol: str, quantity: int, price: float) -> Position:  # Updates the tracked position after a buy.
        existing = self._positions.get(symbol)  # Looks up any existing position for the symbol.
        if not existing:  # Handles the case where this is the first buy for the symbol.
            pos = Position(symbol=symbol, quantity=quantity, average_price=price)  # Creates a new position record.
            self._positions[symbol] = pos  # Saves the new position in the tracker.
            return pos  # Returns the newly created position.

        new_qty = existing.quantity + quantity  # Calculates the new total quantity after the buy.
        weighted_avg = ((existing.average_price * existing.quantity) + (price * quantity)) / new_qty  # Recalculates the weighted average entry price.
        existing.quantity = new_qty  # Updates the stored quantity.
        existing.average_price = weighted_avg  # Updates the stored average price.
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

    def snapshot(self) -> dict[str, Position]:  # Returns a copy of the current positions dictionary.
        return dict(self._positions)  # Creates a shallow copy so callers cannot replace the internal mapping directly.

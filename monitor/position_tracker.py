from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Position:
    symbol: str
    quantity: int
    average_price: float


class PositionTracker:
    """In-memory position tracker for intraday session state."""

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}

    def update_buy(self, symbol: str, quantity: int, price: float) -> Position:
        existing = self._positions.get(symbol)
        if not existing:
            pos = Position(symbol=symbol, quantity=quantity, average_price=price)
            self._positions[symbol] = pos
            return pos

        new_qty = existing.quantity + quantity
        weighted_avg = ((existing.average_price * existing.quantity) + (price * quantity)) / new_qty
        existing.quantity = new_qty
        existing.average_price = weighted_avg
        return existing

    def update_sell(self, symbol: str, quantity: int) -> Position | None:
        existing = self._positions.get(symbol)
        if not existing:
            return None

        existing.quantity -= quantity
        if existing.quantity <= 0:
            del self._positions[symbol]
            return None
        return existing

    def snapshot(self) -> dict[str, Position]:
        return dict(self._positions)

from __future__ import annotations


class OrderManager:
    """Creates order payloads and optionally sends them through a broker client."""

    def __init__(self, broker_client: object | None = None, paper_trade: bool = True) -> None:
        self.broker_client = broker_client
        self.paper_trade = paper_trade

    def place_market_order(self, symbol: str, side: str, quantity: int) -> dict:
        order = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "order_type": "MARKET",
            "status": "PAPER_FILLED" if self.paper_trade else "PLACED",
        }

        if not self.paper_trade and self.broker_client:
            return self.broker_client.place_order(order)

        return order

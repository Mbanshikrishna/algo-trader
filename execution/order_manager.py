from __future__ import annotations  # Lets Python postpone evaluation of type hints.


class OrderManager:  # Defines a helper responsible for constructing and submitting orders.
    """Creates order payloads and optionally sends them through a broker client."""

    def __init__(self, broker_client: object | None = None, paper_trade: bool = True) -> None:  # Initializes the order manager with broker and execution mode.
        self.broker_client = broker_client  # Stores the optional broker client used for live execution.
        self.paper_trade = paper_trade  # Stores whether orders should be simulated instead of sent live.

    def place_market_order(self, symbol: str, side: str, quantity: int) -> dict:  # Creates and optionally sends a market order.
        order = {  # Builds the order payload in a broker-friendly dictionary format.
            "symbol": symbol,  # Stores the instrument symbol being traded.
            "side": side,  # Stores whether this is a buy or sell order.
            "quantity": quantity,  # Stores the number of shares to trade.
            "order_type": "MARKET",  # Marks the order as a market order.
            "status": "PAPER_FILLED" if self.paper_trade else "PLACED",  # Uses a simulated fill status in paper mode or a placed status in live mode.
        }  # Finishes building the order payload.

        if not self.paper_trade and self.broker_client:  # Checks whether this should be sent to a live broker client.
            return self.broker_client.place_order(order)  # Delegates live order placement to the broker client.

        return order  # Returns the simulated order payload in paper mode.

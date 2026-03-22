from __future__ import annotations  # Lets Python postpone evaluation of type hints.

from config.instruments import Instrument  # Imports broker instrument metadata used to build live Angel One orders.


class OrderManager:  # Defines a helper responsible for constructing and submitting orders.
    """Creates order payloads and optionally sends them through a broker client."""

    def __init__(
        self,
        broker_client: object | None = None,
        paper_trade: bool = True,
        product_type: str = "INTRADAY",
        variety: str = "NORMAL",
    ) -> None:  # Initializes the order manager with broker and execution defaults.
        self.broker_client = broker_client  # Stores the optional broker client used for live execution.
        self.paper_trade = paper_trade  # Stores whether orders should be simulated instead of sent live.
        self.product_type = product_type  # Stores the Angel One product type for live orders.
        self.variety = variety  # Stores the Angel One order variety for live orders.

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        instrument: Instrument | None = None,
    ) -> dict:  # Creates and optionally sends a market order.
        order = {  # Builds the order payload in a broker-friendly dictionary format.
            "symbol": symbol,  # Stores the user-facing symbol being traded.
            "side": side,  # Stores whether this is a buy or sell order.
            "quantity": quantity,  # Stores the number of shares to trade.
            "order_type": "MARKET",  # Marks the order as a market order.
            "status": "PAPER_FILLED" if self.paper_trade else "PLACED",  # Uses a simulated fill status in paper mode or a placed status in live mode.
        }  # Finishes building the app-level order payload.

        if instrument is not None:  # Adds Angel One-specific broker fields when a resolved instrument is available.
            order.update(
                {
                    "exchange": instrument.exchange,
                    "tradingsymbol": instrument.tradingsymbol,
                    "symboltoken": instrument.symboltoken,
                    "variety": self.variety,
                    "transactiontype": side.upper(),
                    "ordertype": "MARKET",
                    "producttype": self.product_type,
                    "duration": "DAY",
                    "quantity": str(quantity),
                    "squareoff": "0",
                    "stoploss": "0",
                }
            )

        if not self.paper_trade and self.broker_client:  # Checks whether this should be sent to a live broker client.
            return self.broker_client.place_order(order)  # Delegates live order placement to the broker client.

        return order  # Returns the simulated order payload in paper mode.

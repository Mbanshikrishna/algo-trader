from __future__ import annotations  # Lets Python postpone evaluation of type hints.

from config.instruments import Instrument  # Imports broker instrument metadata used to build live Angel One orders.


class OrderManager:  # Defines a helper responsible for constructing and submitting orders.
    """Creates live order payloads and sends them through a broker client."""

    def __init__(
        self,
        broker_client: object,
        product_type: str = "INTRADAY",
        variety: str = "NORMAL",
    ) -> None:  # Initializes the order manager with broker and execution defaults.
        self.broker_client = broker_client  # Stores the optional broker client used for live execution.
        self.product_type = product_type  # Stores the Angel One product type for live orders.
        self.variety = variety  # Stores the Angel One order variety for live orders.

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        instrument: Instrument | None = None,
    ) -> dict:  # Creates and submits a live market order.
        if instrument is None:  # Rejects live orders that do not include the broker identifiers required by Angel One.
            raise ValueError("Live order placement requires a resolved broker instrument")

        order = {  # Builds the order payload in a broker-friendly dictionary format.
            "symbol": symbol,  # Stores the user-facing symbol being traded.
            "side": side,  # Stores whether this is a buy or sell order.
            "quantity": quantity,  # Stores the number of shares to trade.
            "order_type": "MARKET",  # Marks the order as a market order.
        }  # Finishes building the app-level order payload.

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
        return self.broker_client.place_order(order)  # Delegates live order placement to the broker client.

    def place_exit_order(
        self,
        symbol: str,
        quantity: int,
        instrument: Instrument | None = None,
    ) -> dict:  # Places a SELL market order to exit an open position.
        return self.place_market_order(symbol, "SELL", quantity, instrument=instrument)

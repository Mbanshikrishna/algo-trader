from __future__ import annotations

import logging
import time
from typing import Any

from config.instruments import Instrument
from execution.tradability_filter import TradabilityFilter

logger = logging.getLogger("algo_trader")

# Max retries for order placement.
MAX_RETRIES = 2
RETRY_DELAY = 0.5


class OrderManager:
    """Creates live order payloads and sends them through a broker client.

    Includes execution safety:
    - Checks tradability before placing orders
    - Retries with LIMIT order if MARKET is rejected
    - Records broker rejections in the tradability filter blacklist
    """

    def __init__(
        self,
        broker_client: object,
        product_type: str = "INTRADAY",
        variety: str = "NORMAL",
        tradability_filter: TradabilityFilter | None = None,
    ) -> None:
        self.broker_client = broker_client
        self.product_type = product_type
        self.variety = variety
        self.filter = tradability_filter

    def _build_order_payload(
        self,
        symbol: str,
        side: str,
        quantity: int,
        instrument: Instrument,
        order_type: str = "MARKET",
        price: float = 0.0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
            "exchange": instrument.exchange,
            "tradingsymbol": instrument.tradingsymbol,
            "symboltoken": instrument.symboltoken,
            "variety": self.variety,
            "transactiontype": side.upper(),
            "ordertype": order_type,
            "producttype": self.product_type,
            "duration": "DAY",
            "quantity": str(quantity),
            "squareoff": "0",
            "stoploss": "0",
        }
        if order_type == "LIMIT" and price > 0:
            payload["price"] = str(round(price, 2))
        return payload

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        instrument: Instrument | None = None,
        current_price: float = 0.0,
    ) -> dict:
        """Place a market order with execution safety.

        If the market order is rejected due to exchange restrictions,
        the stock is blacklisted and the error is raised.
        If rejected for other reasons, retries once with a LIMIT order.
        """
        if instrument is None:
            raise ValueError("Live order placement requires a resolved broker instrument")

        # Pre-check tradability.
        if self.filter is not None:
            restricted, reason = self.filter.is_restricted(symbol)
            if restricted:
                raise OrderRejectedError(symbol, reason)

        # Attempt 1: MARKET order.
        order = self._build_order_payload(symbol, side, quantity, instrument, "MARKET")
        try:
            result = self.broker_client.place_order(order)
            logger.info("MARKET order placed: %s %s %d %s", side, symbol, quantity, result)
            return result
        except Exception as exc:
            error_msg = str(exc)
            logger.warning("MARKET order failed for %s: %s", symbol, error_msg)

            # Check if this is a tradability rejection.
            if self.filter is not None and self.filter.record_broker_rejection(symbol, error_msg):
                raise OrderRejectedError(symbol, f"Broker rejected (untradable): {error_msg}") from exc

            # Attempt 2: Retry with LIMIT order at current price.
            if current_price > 0:
                logger.info("Retrying %s with LIMIT order at %.2f", symbol, current_price)
                time.sleep(RETRY_DELAY)
                limit_order = self._build_order_payload(
                    symbol, side, quantity, instrument, "LIMIT", price=current_price,
                )
                try:
                    result = self.broker_client.place_order(limit_order)
                    logger.info("LIMIT order placed: %s %s %d @ %.2f %s", side, symbol, quantity, current_price, result)
                    return result
                except Exception as limit_exc:
                    limit_msg = str(limit_exc)
                    logger.warning("LIMIT order also failed for %s: %s", symbol, limit_msg)
                    if self.filter is not None:
                        self.filter.record_broker_rejection(symbol, limit_msg)
                    raise OrderRejectedError(symbol, f"Both MARKET and LIMIT failed: {limit_msg}") from limit_exc

            raise

    def place_exit_order(
        self,
        symbol: str,
        quantity: int,
        instrument: Instrument | None = None,
        current_price: float = 0.0,
    ) -> dict:
        """Place a SELL order to exit a position. Skips tradability check for exits."""
        if instrument is None:
            raise ValueError("Live order placement requires a resolved broker instrument")

        # Exits skip the tradability pre-check — we must close the position regardless.
        order = self._build_order_payload(symbol, "SELL", quantity, instrument, "MARKET")
        try:
            return self.broker_client.place_order(order)
        except Exception as exc:
            # Retry exit with LIMIT if MARKET fails.
            if current_price > 0:
                logger.warning("MARKET exit failed for %s, retrying with LIMIT at %.2f", symbol, current_price)
                time.sleep(RETRY_DELAY)
                limit_order = self._build_order_payload(
                    symbol, "SELL", quantity, instrument, "LIMIT", price=current_price,
                )
                return self.broker_client.place_order(limit_order)
            raise


class OrderRejectedError(Exception):
    """Raised when an order is rejected due to exchange restrictions."""

    def __init__(self, symbol: str, reason: str) -> None:
        self.symbol = symbol
        self.reason = reason
        super().__init__(f"Order rejected for {symbol}: {reason}")

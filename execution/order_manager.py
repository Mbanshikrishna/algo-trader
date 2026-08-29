from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from config.instruments import Instrument
from execution.tradability_filter import TradabilityFilter
from utils.tick import tick_round

logger = logging.getLogger("algo_trader")

# Max retries for order placement.
MAX_RETRIES = 2
RETRY_DELAY = 0.5


class OrderState(str, Enum):
    SUBMITTED = "submitted"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OrderExecution:
    order_id: str
    state: OrderState
    requested_quantity: int
    filled_quantity: int = 0
    average_price: float = 0.0
    reason: str = ""
    raw_order: dict[str, Any] | None = None

    @property
    def is_filled(self) -> bool:
        return self.state is OrderState.FILLED and self.filled_quantity == self.requested_quantity


class OrderUnconfirmedError(RuntimeError):
    """Raised when an order cannot be proven filled or rejected."""


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
        event_sink: Callable[..., Any] | None = None,
    ) -> None:
        self.broker_client = broker_client
        self.product_type = product_type
        self.variety = variety
        self.filter = tradability_filter
        self.event_sink = event_sink
        self._order_context: dict[str, dict[str, Any]] = {}

    def _emit(self, event_type: str, **kwargs: Any) -> None:
        if self.event_sink is not None:
            self.event_sink(event_type, **kwargs)

    def _remember_order(
        self,
        result: dict[str, Any],
        *,
        symbol: str,
        token: str,
        side: str,
        quantity: int,
        reference_price: float,
        order_type: str,
        payload: dict[str, Any],
    ) -> None:
        order_id = self.extract_order_id(result)
        context = {
            "symbol": symbol,
            "token": token,
            "side": side.upper(),
            "quantity": quantity,
            "reference_price": reference_price,
            "order_type": order_type,
            "request": payload,
            "response": result,
        }
        if order_id:
            self._order_context[order_id] = context
        self._emit(
            "order_submitted",
            symbol=symbol,
            token=token,
            decision="submitted",
            payload={"order_id": order_id, **context},
        )

    def _finalize_execution(self, execution: OrderExecution, symbol: str) -> OrderExecution:
        context = self._order_context.get(execution.order_id, {})
        payload = {
            **context,
            "order_id": execution.order_id,
            "state": execution.state.value,
            "requested_quantity": execution.requested_quantity,
            "filled_quantity": execution.filled_quantity,
            "fill_price": execution.average_price,
            "broker_order": execution.raw_order,
        }
        self._emit(
            "order_execution",
            symbol=symbol,
            token=str(context.get("token", "")),
            decision=execution.state.value,
            reason=execution.reason,
            payload=payload,
        )
        if execution.filled_quantity > 0 and execution.average_price > 0:
            self._emit(
                "confirmed_fill",
                symbol=symbol,
                token=str(context.get("token", "")),
                decision=execution.state.value,
                reason="broker-confirmed fill",
                payload=payload,
            )
        return execution

    def _build_order_payload(
        self,
        symbol: str,
        side: str,
        quantity: int,
        instrument: Instrument,
        order_type: str = "MARKET",
        price: float = 0.0,
        product_type: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "exchange": instrument.exchange,
            "tradingsymbol": instrument.tradingsymbol,
            "symboltoken": instrument.symboltoken,
            "variety": self.variety,
            "transactiontype": side.upper(),
            "ordertype": order_type,
            "producttype": product_type or self.product_type,
            "duration": "DAY",
            "quantity": str(quantity),
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
        }
        if order_type == "LIMIT" and price > 0:
            payload["price"] = str(tick_round(price, "up" if side.upper() == "BUY" else "down"))
        return payload

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        instrument: Instrument | None = None,
        current_price: float = 0.0,
        product_type: str | None = None,
    ) -> dict:
        """Place a market order with execution safety.

        If the market order is rejected due to exchange restrictions,
        the stock is blacklisted and the error is raised.
        If rejected for other reasons, retries once with a LIMIT order.
        ``product_type`` overrides the default ("INTRADAY" or "DELIVERY").
        """
        if instrument is None:
            raise ValueError("Live order placement requires a resolved broker instrument")

        # Pre-check tradability.
        if self.filter is not None:
            restricted, reason = self.filter.is_restricted(symbol)
            if restricted:
                raise OrderRejectedError(symbol, reason)

        # Attempt 1: MARKET order.
        order = self._build_order_payload(
            symbol, side, quantity, instrument, "MARKET", product_type=product_type,
        )
        self._emit(
            "order_requested",
            symbol=symbol,
            token=instrument.symboltoken,
            decision="submit",
            payload={
                "side": side.upper(),
                "quantity": quantity,
                "reference_price": current_price,
                "order_type": "MARKET",
                "request": order,
            },
        )
        try:
            result = self.broker_client.place_order(order)
            self._remember_order(
                result,
                symbol=symbol,
                token=instrument.symboltoken,
                side=side,
                quantity=quantity,
                reference_price=current_price,
                order_type="MARKET",
                payload=order,
            )
            logger.info("MARKET order placed: %s %s %d [%s] %s", side, symbol, quantity, product_type or self.product_type, result)
            return result
        except Exception as exc:
            error_msg = str(exc)
            self._emit(
                "order_attempt_failed",
                symbol=symbol,
                token=instrument.symboltoken,
                decision="retry_or_reject",
                reason=error_msg,
                payload={"side": side.upper(), "order_type": "MARKET", "request": order},
            )
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
                    product_type=product_type,
                )
                try:
                    result = self.broker_client.place_order(limit_order)
                    self._remember_order(
                        result,
                        symbol=symbol,
                        token=instrument.symboltoken,
                        side=side,
                        quantity=quantity,
                        reference_price=current_price,
                        order_type="LIMIT",
                        payload=limit_order,
                    )
                    logger.info("LIMIT order placed: %s %s %d @ %.2f %s", side, symbol, quantity, current_price, result)
                    return result
                except Exception as limit_exc:
                    limit_msg = str(limit_exc)
                    logger.warning("LIMIT order also failed for %s: %s", symbol, limit_msg)
                    if self.filter is not None:
                        self.filter.record_broker_rejection(symbol, limit_msg)
                    self._emit(
                        "order_rejected",
                        symbol=symbol,
                        token=instrument.symboltoken,
                        decision="rejected",
                        reason=limit_msg,
                        payload={"side": side.upper(), "request": limit_order},
                    )
                    raise OrderRejectedError(symbol, f"Both MARKET and LIMIT failed: {limit_msg}") from limit_exc

            raise

    def place_exit_order(
        self,
        symbol: str,
        quantity: int,
        instrument: Instrument | None = None,
        current_price: float = 0.0,
        product_type: str | None = None,
    ) -> dict:
        """Place a SELL order to exit a position. Skips tradability check for exits.

        ``product_type`` must match the product type used for the entry order
        (INTRADAY or DELIVERY).
        """
        if instrument is None:
            raise ValueError("Live order placement requires a resolved broker instrument")

        # Exits skip the tradability pre-check — we must close the position regardless.
        order = self._build_order_payload(
            symbol, "SELL", quantity, instrument, "MARKET", product_type=product_type,
        )
        self._emit(
            "order_requested",
            symbol=symbol,
            token=instrument.symboltoken,
            decision="submit",
            payload={
                "side": "SELL",
                "quantity": quantity,
                "reference_price": current_price,
                "order_type": "MARKET",
                "request": order,
            },
        )
        try:
            result = self.broker_client.place_order(order)
            self._remember_order(
                result,
                symbol=symbol,
                token=instrument.symboltoken,
                side="SELL",
                quantity=quantity,
                reference_price=current_price,
                order_type="MARKET",
                payload=order,
            )
            return result
        except Exception as exc:
            self._emit(
                "order_attempt_failed",
                symbol=symbol,
                token=instrument.symboltoken,
                decision="retry_or_fail",
                reason=str(exc),
                payload={"side": "SELL", "order_type": "MARKET", "request": order},
            )
            # Retry exit with LIMIT if MARKET fails.
            if current_price > 0:
                logger.warning("MARKET exit failed for %s, retrying with LIMIT at %.2f", symbol, current_price)
                time.sleep(RETRY_DELAY)
                limit_order = self._build_order_payload(
                    symbol, "SELL", quantity, instrument, "LIMIT", price=current_price,
                    product_type=product_type,
                )
                result = self.broker_client.place_order(limit_order)
                self._remember_order(
                    result,
                    symbol=symbol,
                    token=instrument.symboltoken,
                    side="SELL",
                    quantity=quantity,
                    reference_price=current_price,
                    order_type="LIMIT",
                    payload=limit_order,
                )
                return result
            raise


    @staticmethod
    def extract_order_id(order_result: dict[str, Any]) -> str:
        resp = order_result.get("response", {})
        data = resp.get("data", {})
        order_id = data.get("orderid") if isinstance(data, dict) else str(data) if data else None
        return str(order_id or order_result.get("orderid") or "")

    @staticmethod
    def _number(entry: dict[str, Any], *keys: str) -> float:
        for key in keys:
            value = entry.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return 0.0

    def wait_for_fill(
        self,
        order_result: dict[str, Any],
        symbol: str,
        expected_quantity: int,
        timeout: float = 15.0,
    ) -> OrderExecution:
        """Return broker-confirmed fill details; uncertainty always fails closed."""
        order_id = self.extract_order_id(order_result)
        if not order_id:
            return self._finalize_execution(OrderExecution(
                order_id="", state=OrderState.UNKNOWN,
                requested_quantity=expected_quantity,
                reason="broker response contained no order ID",
            ), symbol)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                book = self.broker_client.get_order_book()
                orders = book.get("data") or []
                for entry in orders:
                    if str(entry.get("orderid", "")) == str(order_id):
                        status = str(entry.get("orderstatus") or entry.get("status") or "").lower()
                        filled = int(self._number(
                            entry, "filledshares", "filledqty", "filledQty", "tradedQuantity",
                        ))
                        if status in ("complete", "filled", "traded", "executed"):
                            if filled <= 0:
                                filled = int(self._number(entry, "quantity")) or expected_quantity
                            average = self._number(
                                entry, "averageprice", "avgprice", "averagePrice",
                                "averageTradedPrice", "price",
                            )
                            state = OrderState.FILLED if filled == expected_quantity else OrderState.PARTIALLY_FILLED
                            return self._finalize_execution(OrderExecution(
                                order_id=order_id, state=state,
                                requested_quantity=expected_quantity,
                                filled_quantity=filled, average_price=average,
                                raw_order=entry,
                            ), symbol)
                        if status in ("rejected", "cancelled", "canceled", "expired"):
                            reason = entry.get("text", "unknown reason")
                            terminal = OrderState.REJECTED if status == "rejected" else OrderState.CANCELLED
                            return self._finalize_execution(OrderExecution(
                                order_id=order_id, state=terminal,
                                requested_quantity=expected_quantity,
                                filled_quantity=filled, reason=str(reason), raw_order=entry,
                            ), symbol)
                        break
            except Exception as exc:
                logger.warning("Order book check failed for %s: %s", symbol, exc)
            time.sleep(0.5)

        logger.error("Order %s for %s: timed out waiting for broker confirmation", order_id, symbol)
        return self._finalize_execution(OrderExecution(
            order_id=order_id, state=OrderState.UNKNOWN,
            requested_quantity=expected_quantity,
            reason="timed out waiting for broker confirmation",
        ), symbol)

    def verify_order_filled(
        self, order_result: dict, symbol: str, timeout: float = 15.0,
        expected_quantity: int | None = None,
    ) -> bool:
        """Compatibility wrapper; returns true only for a complete confirmed fill."""
        quantity = expected_quantity or int(order_result.get("quantity") or 0)
        if quantity <= 0:
            logger.error("Cannot verify %s without an expected quantity", symbol)
            return False
        return self.wait_for_fill(order_result, symbol, quantity, timeout).is_filled


class OrderRejectedError(Exception):
    """Raised when an order is rejected due to exchange restrictions."""

    def __init__(self, symbol: str, reason: str) -> None:
        self.symbol = symbol
        self.reason = reason
        super().__init__(f"Order rejected for {symbol}: {reason}")

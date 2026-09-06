"""Paper trading broker client — simulates orders using live market data.

Wraps a real broker client (Angel One or Dhan) for market data but
intercepts all order operations. Tracks simulated positions, P&L,
and order history in memory and logs every action.

Usage: set PAPER_TRADE=true in .env to activate.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("algo_trader")

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class PaperOrder:
    """A simulated order."""

    order_id: str
    symbol: str
    token: str
    side: str  # BUY or SELL
    quantity: int
    price: float
    order_type: str  # MARKET, LIMIT, STOPLOSS_MARKET
    product_type: str
    status: str  # COMPLETE, CANCELLED, PENDING
    trigger_price: float = 0.0
    reference_price: float = 0.0
    fees: float = 0.0
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class PaperPosition:
    """A simulated position."""

    symbol: str
    token: str
    quantity: int
    average_price: float
    product_type: str
    entry_fees: float = 0.0


class PaperBrokerClient:
    """Drop-in replacement for AngelOneClient/DhanClient that simulates orders.

    All market data calls are forwarded to the real data broker.
    All order/position calls are simulated in memory.
    """

    def __init__(
        self,
        data_broker: Any,
        capital: float = 100_000,
        *,
        slippage_bps: float = 5.0,
        fees_bps_per_side: float = 10.0,
    ) -> None:
        if slippage_bps < 0 or fees_bps_per_side < 0:
            raise ValueError("paper execution costs cannot be negative")
        self._data = data_broker
        self._capital = capital
        self._slippage_bps = slippage_bps
        self._fees_bps_per_side = fees_bps_per_side
        self._available = capital
        self._orders: dict[str, PaperOrder] = {}
        self._positions: dict[str, PaperPosition] = {}
        self._trade_log: list[dict] = []
        self._daily_pnl = 0.0
        logger.info(
            "[PAPER] Paper trading mode active. Capital: %.2f, slippage=%.1f "
            "bps/side, fees=%.1f bps/side",
            capital, slippage_bps, fees_bps_per_side,
        )

    # --- Market data: forwarded to real broker ---

    def get_market_data(self, mode: str, exchange_tokens: dict) -> dict:
        return self._data.get_market_data(mode, exchange_tokens)

    def get_candle_data(self, exchange: str, token: str, interval: str,
                        from_date: Any, to_date: Any) -> list:
        return self._data.get_candle_data(exchange, token, interval, from_date, to_date)

    def get_ltp(self, exchange: str, symbol: str, token: str) -> dict:
        return self._data.get_ltp(exchange, symbol, token)

    def _load_scrip_master(self) -> list:
        return self._data._load_scrip_master()

    def refresh_if_stale(self, max_age_seconds: int = 7200) -> None:
        """No-op — main.py refreshes data_broker separately."""
        pass

    # --- Order operations: simulated ---

    def place_order(self, payload: dict) -> dict:
        """Simulate placing an order. Market orders fill immediately at LTP."""
        order_id = str(uuid.uuid4())[:8]
        symbol = payload.get("tradingsymbol", "")
        token = payload.get("symboltoken", "")
        side = payload.get("transactiontype", "BUY")
        quantity = int(payload.get("quantity", 0))
        order_type = payload.get("ordertype", "MARKET")
        product_type = payload.get("producttype", "INTRADAY")
        trigger_price = float(payload.get("triggerprice", 0))

        # For SL-M orders, keep them pending until triggered
        if order_type == "STOPLOSS_MARKET":
            order = PaperOrder(
                order_id=order_id, symbol=symbol, token=token,
                side=side, quantity=quantity, price=0,
                order_type=order_type, product_type=product_type,
                status="PENDING", trigger_price=trigger_price,
            )
            self._orders[order_id] = order
            logger.info(
                "[PAPER] SL-M order placed: %s %s %d trigger=%.2f id=%s",
                side, symbol, quantity, trigger_price, order_id,
            )
            return {"response": {"data": {"orderid": order_id}}}

        # Market/Limit orders: fetch LTP and fill immediately
        reference_price = self._get_ltp_for(symbol, token)
        if reference_price <= 0:
            reference_price = float(payload.get("price", 0))
        fill_price = self._adverse_fill(reference_price, side)
        fee = self._fee(fill_price, quantity)

        order = PaperOrder(
            order_id=order_id, symbol=symbol, token=token,
            side=side, quantity=quantity, price=fill_price,
            order_type=order_type, product_type=product_type,
            status="COMPLETE",
            reference_price=reference_price,
            fees=fee,
        )
        self._orders[order_id] = order

        # Update positions
        if side == "BUY":
            self._apply_buy(
                symbol, token, quantity, fill_price, product_type, fee
            )
        else:
            pnl = self._apply_sell(symbol, quantity, fill_price, fee)
            self._daily_pnl += pnl

        logger.info(
            "[PAPER] %s order filled: %s %s %d @ %.2f "
            "(reference=%.2f, fee=%.2f) id=%s",
            order_type, side, symbol, quantity, fill_price,
            reference_price, fee, order_id,
        )
        return {"response": {"data": {"orderid": order_id}}}

    def modify_order(self, payload: dict) -> dict:
        """Modify a pending SL-M order's trigger price."""
        order_id = payload.get("orderid", "")
        order = self._orders.get(order_id)
        if order and order.status == "PENDING":
            old_trigger = order.trigger_price
            order.trigger_price = float(payload.get("triggerprice", order.trigger_price))
            logger.info(
                "[PAPER] SL-M modified: %s trigger %.2f → %.2f id=%s",
                order.symbol, old_trigger, order.trigger_price, order_id,
            )
        return {"response": {"data": {"orderid": order_id}}}

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> dict:
        """Cancel a pending order."""
        order = self._orders.get(order_id)
        if order and order.status == "PENDING":
            order.status = "CANCELLED"
            logger.info("[PAPER] Order cancelled: %s id=%s", order.symbol, order_id)
        return {"response": {"data": {"orderid": order_id}}}

    def get_order_book(self) -> dict:
        """Return all orders in Angel One format."""
        orders = []
        for o in self._orders.values():
            orders.append({
                "orderid": o.order_id,
                "tradingsymbol": o.symbol,
                "transactiontype": o.side,
                "quantity": str(o.quantity),
                "price": str(o.price),
                "filledshares": str(o.quantity if o.status == "COMPLETE" else 0),
                "averageprice": str(o.price),
                "triggerprice": str(o.trigger_price),
                "orderstatus": o.status,
                "status": o.status,
                "ordertype": o.order_type,
                "producttype": o.product_type,
                "referenceprice": str(o.reference_price),
                "fees": str(o.fees),
            })
        return {"data": orders}

    def get_positions(self) -> dict:
        """Return current positions in Angel One format."""
        positions = []
        for p in self._positions.values():
            positions.append({
                "tradingsymbol": p.symbol,
                "symboltoken": p.token,
                "netqty": str(p.quantity),
                "avgnetprice": str(p.average_price),
                "producttype": p.product_type,
            })
        return {"data": positions}

    def get_available_capital(self) -> float:
        return self._available

    # --- Dhan-specific methods (no-ops for paper) ---

    def load_scrip_master(self) -> None:
        """No-op — paper client uses data broker's scrip master."""
        pass

    def _resolve_security_id(self, symbol: str) -> int:
        """Forward to data broker if it has this method."""
        if hasattr(self._data, "_resolve_security_id"):
            return self._data._resolve_security_id(symbol)
        return 0

    # --- Internal helpers ---

    def _get_ltp_for(self, symbol: str, token: str) -> float:
        """Fetch current LTP from the data broker."""
        try:
            data = self._data.get_market_data("LTP", {"NSE": [str(token)]})
            fetched = data.get("fetched", [])
            if fetched:
                return float(fetched[0].get("ltp", 0))
        except Exception as exc:
            logger.warning("[PAPER] LTP fetch failed for %s: %s", symbol, exc)
        return 0.0

    def _adverse_fill(self, reference_price: float, side: str) -> float:
        if reference_price <= 0:
            return 0.0
        rate = self._slippage_bps / 10_000
        multiplier = 1 + rate if side == "BUY" else 1 - rate
        return round(reference_price * multiplier, 2)

    def _fee(self, price: float, quantity: int) -> float:
        return round(
            price * quantity * self._fees_bps_per_side / 10_000, 2
        )

    def _apply_buy(
        self, symbol: str, token: str, qty: int, price: float,
        product_type: str, fee: float,
    ) -> None:
        """Add to simulated position."""
        existing = self._positions.get(symbol)
        if existing:
            total_qty = existing.quantity + qty
            existing.average_price = (
                (existing.average_price * existing.quantity) + (price * qty)
            ) / total_qty
            existing.quantity = total_qty
            existing.entry_fees += fee
        else:
            self._positions[symbol] = PaperPosition(
                symbol=symbol, token=token, quantity=qty,
                average_price=price, product_type=product_type,
                entry_fees=fee,
            )
        cost = price * qty
        self._available -= cost + fee
        self._trade_log.append({
            "time": datetime.now(IST).strftime("%H:%M:%S"),
            "side": "BUY", "symbol": symbol, "qty": qty,
            "price": price, "cost": cost, "fees": fee,
        })

    def _apply_sell(self, symbol: str, qty: int, price: float, fee: float) -> float:
        """Remove from simulated position and return net realized P&L."""
        existing = self._positions.get(symbol)
        net_pnl = 0.0
        if existing:
            if qty > existing.quantity:
                raise ValueError(
                    f"Cannot paper-sell {qty} {symbol}; held={existing.quantity}"
                )
            held_before = existing.quantity
            allocated_entry_fees = round(
                existing.entry_fees * qty / held_before, 2
            )
            gross_pnl = (price - existing.average_price) * qty
            net_pnl = gross_pnl - allocated_entry_fees - fee
            existing.quantity -= qty
            existing.entry_fees = max(
                existing.entry_fees - allocated_entry_fees, 0.0
            )
            if existing.quantity <= 0:
                del self._positions[symbol]
            revenue = price * qty
            self._available += revenue - fee
            self._trade_log.append({
                "time": datetime.now(IST).strftime("%H:%M:%S"),
                "side": "SELL", "symbol": symbol, "qty": qty,
                "price": price,
                "gross_pnl": round(gross_pnl, 2),
                "fees": round(allocated_entry_fees + fee, 2),
                "pnl": round(net_pnl, 2),
            })
        return net_pnl

    def print_summary(self) -> str:
        """Generate end-of-day paper trading summary."""
        lines = [
            "",
            "=" * 60,
            "PAPER TRADING SUMMARY",
            "=" * 60,
            "",
        ]

        if self._trade_log:
            lines.append(f"Total trades: {len(self._trade_log)}")
            buys = [t for t in self._trade_log if t["side"] == "BUY"]
            sells = [t for t in self._trade_log if t["side"] == "SELL"]
            lines.append(f"Buys: {len(buys)}, Sells: {len(sells)}")
            lines.append("")

            for t in self._trade_log:
                if t["side"] == "BUY":
                    lines.append(
                        f"  {t['time']} BUY  {t['symbol']} x{t['qty']} @ {t['price']:.2f} "
                        f"fees: Rs.{t['fees']:.2f}"
                    )
                else:
                    lines.append(
                        f"  {t['time']} SELL {t['symbol']} x{t['qty']} @ {t['price']:.2f} "
                        f"gross: Rs.{t['gross_pnl']:+,.2f}, fees: Rs.{t['fees']:.2f}, "
                        f"net P&L: Rs.{t['pnl']:+,.2f}"
                    )
        else:
            lines.append("No trades executed today.")

        lines.append("")
        lines.append(f"Daily net P&L: Rs.{self._daily_pnl:+,.2f}")
        lines.append(f"Capital: Rs.{self._capital:,.2f} → Rs.{self._available:,.2f}")
        lines.append("")

        if self._positions:
            lines.append("Open positions (not closed):")
            for p in self._positions.values():
                lines.append(f"  {p.symbol}: {p.quantity} @ {p.average_price:.2f}")
        else:
            lines.append("All positions closed.")

        lines.append("=" * 60)
        summary = "\n".join(lines)
        logger.info(summary)
        return summary

    def reset_daily(self) -> None:
        """Reset daily state for a new trading day."""
        self._orders.clear()
        self._positions.clear()
        self._trade_log.clear()
        self._daily_pnl = 0.0
        self._available = self._capital

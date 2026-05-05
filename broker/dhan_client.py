"""Dhan broker client — drop-in alternative to AngelOneClient.

Implements the same public interface so the rest of the codebase
(OrderManager, TradabilityFilter, main.py) can use either broker
without changes.

Key differences from Angel One:
- No hidden cautionary list — all stocks tradable via API.
- Uses security_id (int) instead of symboltoken (str).
- Trading symbols don't have -EQ suffix.
- Market data requires separate subscription — use Angel One for data.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger("algo_trader")

IST = ZoneInfo("Asia/Kolkata")

# Scrip master cache
_DHAN_SCRIP_CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / ".dhan_scrip_master.json"
_DHAN_SCRIP_CACHE_MAX_AGE = 86400  # 24 hours


class DhanRateLimiter:
    """Simple rate limiter — Dhan allows 10 order requests/second."""

    def __init__(self, max_per_second: float = 9.0) -> None:
        self._min_interval = 1.0 / max_per_second
        self._last_call = 0.0

    def acquire(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()


class DhanClient:
    """Dhan broker client with the same interface as AngelOneClient."""

    API_BASE = "https://api.dhan.co/v2"
    SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

    def __init__(self, client_id: str, access_token: str) -> None:
        self.client_id = client_id
        self.access_token = access_token
        self._session = requests.Session()
        self._session.headers.update({
            "access-token": access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._rate_limiter = DhanRateLimiter()
        self._login_time = time.monotonic()
        self._scrip_master: list[dict[str, str]] = []
        # Map Angel One symbol (e.g. "SBIN-EQ") → Dhan security_id
        self._symbol_to_security_id: dict[str, int] = {}
        # Map Angel One token → Dhan security_id
        self._angel_token_to_dhan_id: dict[str, int] = {}

    @classmethod
    def login(
        cls,
        client_id: str | None = None,
        access_token: str | None = None,
        **kwargs: Any,
    ) -> DhanClient:
        """Create a DhanClient. Accepts extra kwargs for compatibility with AngelOneClient.login()."""
        cid = client_id or os.getenv("DHAN_CLIENT_ID", "")
        token = access_token or os.getenv("DHAN_ACCESS_TOKEN", "")
        if not cid or not token:
            raise ValueError("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN must be set")
        client = cls(cid, token)
        # Verify connection
        try:
            result = client._get("/fundlimit")
            logger.info("Dhan login successful. Client ID: %s", result.get("dhanClientId", cid))
        except Exception as exc:
            raise ConnectionError(f"Dhan API connection failed: {exc}") from exc
        return client

    # ── HTTP helpers ──────────────────────────────────────────────

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._rate_limiter.acquire()
        resp = self._session.get(f"{self.API_BASE}{path}", params=params, timeout=15)
        return self._handle_response(resp, f"GET {path}")

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self._rate_limiter.acquire()
        resp = self._session.post(f"{self.API_BASE}{path}", json=body, timeout=15)
        return self._handle_response(resp, f"POST {path}")

    def _put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self._rate_limiter.acquire()
        resp = self._session.put(f"{self.API_BASE}{path}", json=body, timeout=15)
        return self._handle_response(resp, f"PUT {path}")

    def _delete(self, path: str) -> dict[str, Any]:
        self._rate_limiter.acquire()
        resp = self._session.delete(f"{self.API_BASE}{path}", timeout=15)
        return self._handle_response(resp, f"DELETE {path}")

    @staticmethod
    def _handle_response(resp: requests.Response, context: str) -> dict[str, Any]:
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                return {"data": resp.text}
            # Dhan returns {"status": "failure", ...} on some errors
            if isinstance(data, dict) and data.get("status") == "failure":
                remarks = data.get("remarks", {})
                raise RuntimeError(
                    f"Dhan {context} failed: {remarks.get('error_message', data)}"
                )
            return data
        # Non-200
        try:
            err = resp.json()
            msg = err.get("errorMessage") or err.get("remarks", {}).get("error_message") or str(err)
        except Exception:
            msg = resp.text[:200]
        raise RuntimeError(f"Dhan {context} HTTP {resp.status_code}: {msg}")

    # ── Session management (compatibility) ────────────────────────

    def refresh_if_stale(self, max_age_seconds: int = 7200) -> None:
        """No-op for Dhan — token is valid for 30 days."""
        pass

    # ── Scrip master ──────────────────────────────────────────────

    def load_scrip_master(self) -> list[dict[str, str]]:
        """Load Dhan scrip master and build symbol mappings."""
        if self._scrip_master:
            return self._scrip_master

        # Try disk cache first
        if _DHAN_SCRIP_CACHE_FILE.exists():
            age = time.time() - _DHAN_SCRIP_CACHE_FILE.stat().st_mtime
            if age < _DHAN_SCRIP_CACHE_MAX_AGE:
                try:
                    data = json.loads(_DHAN_SCRIP_CACHE_FILE.read_text())
                    self._scrip_master = data
                    self._build_symbol_maps()
                    logger.info("Dhan scrip master loaded from cache (%d entries)", len(data))
                    return data
                except Exception:
                    pass

        # Fetch from Dhan
        logger.info("Fetching Dhan scrip master...")
        resp = requests.get(self.SCRIP_MASTER_URL, timeout=60)
        resp.raise_for_status()

        reader = csv.DictReader(io.StringIO(resp.text))
        nse_equities = []
        for row in reader:
            if (
                row.get("SEM_EXM_EXCH_ID") == "NSE"
                and row.get("SEM_SEGMENT") == "E"
                and row.get("SEM_INSTRUMENT_NAME") == "EQUITY"
                and row.get("SEM_SERIES") == "EQ"
            ):
                nse_equities.append({
                    "security_id": row["SEM_SMST_SECURITY_ID"],
                    "symbol": row["SEM_TRADING_SYMBOL"],
                    "name": row.get("SM_SYMBOL_NAME", ""),
                    "tick_size": row.get("SEM_TICK_SIZE", "5"),
                })

        # Cache to disk
        _DHAN_SCRIP_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DHAN_SCRIP_CACHE_FILE.write_text(json.dumps(nse_equities))

        self._scrip_master = nse_equities
        self._build_symbol_maps()
        logger.info("Dhan scrip master: %d NSE equities", len(nse_equities))
        return nse_equities

    def _build_symbol_maps(self) -> None:
        """Build mappings from Angel One symbols/tokens to Dhan security IDs."""
        for entry in self._scrip_master:
            symbol = entry["symbol"]
            sec_id = int(entry["security_id"])
            # Angel One uses "SBIN-EQ", Dhan uses "SBIN"
            self._symbol_to_security_id[f"{symbol}-EQ"] = sec_id
            self._symbol_to_security_id[symbol] = sec_id

    def resolve_security_id(self, angel_symbol: str) -> int:
        """Convert an Angel One symbol (e.g. 'SBIN-EQ') to a Dhan security ID."""
        if not self._symbol_to_security_id:
            self.load_scrip_master()
        sec_id = self._symbol_to_security_id.get(angel_symbol)
        if sec_id is None:
            # Try without -EQ
            clean = angel_symbol.replace("-EQ", "")
            sec_id = self._symbol_to_security_id.get(clean)
        if sec_id is None:
            raise ValueError(f"Cannot resolve Dhan security ID for {angel_symbol}")
        return sec_id

    # ── Angel One compatible interface ────────────────────────────
    # These methods match AngelOneClient's signatures so the rest of
    # the codebase works without changes.

    def get_scrip_master(self) -> list[dict[str, str]]:
        """Return scrip master in Angel One format."""
        return self._load_scrip_master()

    def _load_scrip_master(self) -> list[dict[str, str]]:
        """Return scrip master in Angel One format (compatible with AngelOneClient)."""
        if not self._scrip_master:
            self.load_scrip_master()
        return [
            {
                "exch_seg": "NSE",
                "token": entry["security_id"],
                "symbol": f"{entry['symbol']}-EQ",
                "name": entry["name"],
            }
            for entry in self._scrip_master
        ]

    def get_market_data(self, mode: str, exchange_tokens: dict[str, list[str]]) -> dict[str, Any]:
        """Fetch market quotes — converts Angel One format to Dhan format.

        Angel One format: {"NSE": ["3045", "1333"]}
        Dhan format: POST /v2/marketfeed/ltp with {"NSE_EQ": [3045, 1333]}
        """
        nse_tokens = exchange_tokens.get("NSE", [])
        if not nse_tokens:
            return {"fetched": []}

        security_ids = [int(t) for t in nse_tokens]

        try:
            resp = self._post("/marketfeed/ltp", {"NSE_EQ": security_ids})
        except Exception as exc:
            logger.warning("Dhan market data failed: %s", exc)
            return {"fetched": []}

        # Convert Dhan response to Angel One format
        fetched = []
        data = resp.get("data", {}).get("NSE_EQ", {}) if isinstance(resp, dict) else {}
        for sec_id_str, quote in data.items():
            fetched.append({
                "symbolToken": sec_id_str,
                "ltp": quote.get("last_price", 0),
                "open": quote.get("open", 0),
                "high": quote.get("high", 0),
                "low": quote.get("low", 0),
                "close": quote.get("close", 0),
                "volume": quote.get("volume", 0),
                "tradingSymbol": "",  # Filled by caller
            })
        return {"fetched": fetched}

    def get_candle_data(
        self,
        exchange: str,
        token: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> list[list] | None:
        """Fetch historical candle data.

        Converts Angel One interval names to Dhan format.
        Returns data in Angel One candle format: [[timestamp, O, H, L, C, V], ...]
        """
        # Map Angel One intervals to Dhan
        interval_map = {
            "ONE_DAY": "DAY",
            "FIVE_MINUTE": "5",
            "FIFTEEN_MINUTE": "15",
            "ONE_HOUR": "60",
        }
        dhan_interval = interval_map.get(interval, interval)

        try:
            sec_id = int(token)
        except ValueError:
            sec_id = self.resolve_security_id(token)

        body = {
            "securityId": str(sec_id),
            "exchangeSegment": "NSE_EQ",
            "instrument": "EQUITY",
            "expiryCode": 0,
            "fromDate": from_date.split(" ")[0] if " " in from_date else from_date,
            "toDate": to_date.split(" ")[0] if " " in to_date else to_date,
        }

        try:
            if dhan_interval == "DAY":
                resp = self._post("/charts/historical", body)
            else:
                body["interval"] = dhan_interval
                resp = self._post("/charts/intraday", body)
        except Exception as exc:
            logger.warning("Dhan candle data failed for %s: %s", token, exc)
            return None

        # Convert to Angel One format
        candles = []
        if isinstance(resp, dict):
            opens = resp.get("open", [])
            highs = resp.get("high", [])
            lows = resp.get("low", [])
            closes = resp.get("close", [])
            volumes = resp.get("volume", [])
            timestamps = resp.get("timestamp", [])
            for i in range(len(opens)):
                ts = timestamps[i] if i < len(timestamps) else ""
                candles.append([ts, opens[i], highs[i], lows[i], closes[i], volumes[i]])

        return candles if candles else None

    # ── Order management ──────────────────────────────────────────

    def _to_dhan_product(self, angel_product: str) -> str:
        """Convert Angel One product type to Dhan."""
        mapping = {
            "INTRADAY": "INTRADAY",
            "DELIVERY": "CNC",
            "CNC": "CNC",
            "MARGIN": "MARGIN",
        }
        return mapping.get(angel_product, "INTRADAY")

    def _to_dhan_order_type(self, angel_type: str) -> str:
        """Convert Angel One order type to Dhan."""
        mapping = {
            "MARKET": "MARKET",
            "LIMIT": "LIMIT",
            "STOPLOSS_MARKET": "STOP_LOSS_MARKET",
            "STOPLOSS_LIMIT": "STOP_LOSS",
            "SL-M": "STOP_LOSS_MARKET",
            "SL": "STOP_LOSS",
        }
        return mapping.get(angel_type, angel_type)

    def place_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        """Place an order — accepts Angel One format, converts to Dhan."""
        symbol = order_payload.get("tradingsymbol", "")
        try:
            sec_id = self.resolve_security_id(symbol)
        except ValueError:
            sec_id = int(order_payload.get("symboltoken", 0))

        product = self._to_dhan_product(order_payload.get("producttype", "INTRADAY"))
        order_type = self._to_dhan_order_type(order_payload.get("ordertype", "MARKET"))
        transaction = order_payload.get("transactiontype", "BUY")
        quantity = int(order_payload.get("quantity", 0))
        price = float(order_payload.get("price", 0))
        trigger_price = float(order_payload.get("triggerprice", 0))

        # Determine variety — Dhan uses "REGULAR" for normal, "STOP_LOSS" for SL orders
        variety = order_payload.get("variety", "NORMAL")
        if variety == "STOPLOSS" or "STOP_LOSS" in order_type:
            dhan_variety = "STOP_LOSS"
        else:
            dhan_variety = "REGULAR"

        dhan_order = {
            "dhanClientId": self.client_id,
            "transactionType": transaction.upper(),
            "exchangeSegment": "NSE_EQ",
            "productType": product,
            "orderType": order_type,
            "validity": "DAY",
            "securityId": str(sec_id),
            "quantity": quantity,
            "price": price if order_type == "LIMIT" else 0,
            "triggerPrice": trigger_price if "STOP_LOSS" in order_type else 0,
            "disclosedQuantity": 0,
            "afterMarketOrder": False,
            "correlationId": "",
        }

        result = self._post("/orders", dhan_order)

        # Convert to Angel One response format
        order_id = result.get("orderId", "")
        return {
            "broker": "dhan",
            "status": "PLACED",
            "response": {"data": {"orderid": str(order_id)}},
            **order_payload,
        }

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> dict[str, Any]:
        """Cancel an order by ID."""
        return self._delete(f"/orders/{order_id}")

    def modify_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        """Modify an existing order."""
        order_id = order_payload.get("orderid", "")
        order_type = self._to_dhan_order_type(order_payload.get("ordertype", "MARKET"))
        quantity = int(order_payload.get("quantity", 0))
        price = float(order_payload.get("price", 0))
        trigger_price = float(order_payload.get("triggerprice", 0))

        dhan_modify = {
            "dhanClientId": self.client_id,
            "orderId": order_id,
            "orderType": order_type,
            "quantity": quantity,
            "price": price,
            "triggerPrice": trigger_price,
            "disclosedQuantity": 0,
            "validity": "DAY",
        }

        return self._put(f"/orders/{order_id}", dhan_modify)

    def get_order_book(self) -> dict[str, Any]:
        """Fetch order book — returns in Angel One format."""
        orders = self._get("/orders")
        if isinstance(orders, list):
            # Convert Dhan order format to Angel One format
            converted = []
            for o in orders:
                status = o.get("orderStatus", "").upper()
                # Map Dhan statuses to Angel One statuses
                status_map = {
                    "TRADED": "complete",
                    "TRANSIT": "open",
                    "PENDING": "open",
                    "REJECTED": "rejected",
                    "CANCELLED": "cancelled",
                    "EXPIRED": "cancelled",
                }
                raw_sym = o.get("tradingSymbol", "")
                sym = f"{raw_sym}-EQ" if raw_sym and not raw_sym.endswith("-EQ") else raw_sym
                converted.append({
                    "orderid": str(o.get("orderId", "")),
                    "orderstatus": status_map.get(status, status.lower()),
                    "text": o.get("omsErrorDescription", ""),
                    "tradingsymbol": sym,
                    "transactiontype": o.get("transactionType", ""),
                    "quantity": o.get("quantity", 0),
                    "price": o.get("price", 0),
                })
            return {"data": converted}
        return {"data": []}

    def get_positions(self) -> dict[str, Any]:
        """Fetch positions — returns in Angel One format."""
        positions = self._get("/positions")
        if isinstance(positions, list):
            converted = []
            for p in positions:
                # Dhan uses "SBIN", Angel One uses "SBIN-EQ".
                raw_symbol = p.get("tradingSymbol", "")
                symbol = f"{raw_symbol}-EQ" if raw_symbol and not raw_symbol.endswith("-EQ") else raw_symbol
                converted.append({
                    "tradingsymbol": symbol,
                    "symboltoken": str(p.get("securityId", "")),
                    "netqty": str(p.get("netQty", 0)),
                    "buyqty": str(p.get("buyQty", 0)),
                    "sellqty": str(p.get("sellQty", 0)),
                    "buyavgprice": str(p.get("buyAvg", 0)),
                    "sellavgprice": str(p.get("sellAvg", 0)),
                    "ltp": str(p.get("lastTradedPrice", 0)),
                    "pnl": str(p.get("realizedProfit", 0)),
                    "exchange": "NSE",
                    "producttype": p.get("productType", ""),
                })
            return {"data": converted}
        return {"data": []}

    def get_available_capital(self) -> float:
        """Return available cash (unleveraged)."""
        result = self._get("/fundlimit")
        return float(result.get("availabelBalance", 0))

    # ── Compatibility stubs ───────────────────────────────────────

    def get_ltp(self, exchange: str, symbol: str, token: str) -> dict[str, Any]:
        """Get last traded price for a single stock."""
        try:
            sec_id = int(token)
        except ValueError:
            sec_id = self.resolve_security_id(symbol)

        try:
            resp = self._post("/marketfeed/ltp", {"NSE_EQ": [sec_id]})
            data = resp.get("data", {}).get("NSE_EQ", {})
            ltp = data.get(str(sec_id), {}).get("last_price", 0)
            return {"data": {"ltp": ltp, "open": 0, "high": 0, "low": 0, "close": 0}}
        except Exception as exc:
            logger.warning("Dhan LTP failed for %s: %s", symbol, exc)
            return {"data": {"ltp": 0}}

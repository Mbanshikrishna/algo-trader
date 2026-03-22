from __future__ import annotations  # Lets Python postpone evaluation of type annotations.

from datetime import datetime  # Imports datetime for historical candle request windows.
from typing import Any  # Imports Any for JSON-like response typing.

import requests  # Imports requests for SmartAPI HTTP calls.

from config.instruments import Instrument, angel_tradingsymbol_for  # Imports broker instrument helpers used by market data and order placement.


class AngelOneClient:  # Defines a lightweight wrapper around Angel One SmartAPI endpoints used by the bot.
    """Angel One SmartAPI integration for market data and order placement."""

    ORDER_BASE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/order/v1"  # Stores the SmartAPI order endpoint base URL.
    PORTFOLIO_BASE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/portfolio/v1"  # Stores the SmartAPI portfolio endpoint base URL.
    TRADE_BASE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/trade/v1"  # Stores the SmartAPI trade endpoint base URL.
    HISTORICAL_BASE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/historical/v1"  # Stores the SmartAPI historical-candle endpoint base URL.
    MARKET_BASE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/market/v1"  # Stores the SmartAPI quote/search endpoint base URL.

    def __init__(
        self,
        api_key: str,
        client_id: str,
        access_token: str,
        paper_trade: bool = True,
        timeout_seconds: int = 10,
    ) -> None:  # Stores the broker credentials needed for API access.
        self.api_key = api_key  # Stores the configured API key for SmartAPI headers.
        self.client_id = client_id  # Stores the client code used by SmartAPI.
        self.access_token = access_token  # Stores the JWT access token used for authenticated requests.
        self.paper_trade = paper_trade  # Stores whether paper trade mode is active.
        self.timeout_seconds = timeout_seconds  # Stores a default timeout for SmartAPI requests.
        self.session = requests.Session()  # Reuses one HTTP session across requests.
        self.session.headers.update(
            {
                "Accept": "application/json",  # Requests JSON responses from SmartAPI.
                "Content-Type": "application/json",  # Sends JSON payloads for POST requests.
                "X-Client-Code": self.client_id,  # Provides the client code in the common SmartAPI header form.
                "X-API-Key": self.api_key,  # Provides the API key in the header used by this project's existing implementation.
                "X-PrivateKey": self.api_key,  # Provides the API key in the header used by SmartAPI SDK examples and forum traces.
                "X-UserType": "USER",  # Marks the request as coming from a user session.
                "X-SourceID": "WEB",  # Uses the default SmartAPI web source identifier.
                "Authorization": f"Bearer {self.access_token}",  # Authenticates requests with the JWT access token.
            }
        )

    def _get(self, base_url: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:  # Performs a GET request against one SmartAPI service group.
        url = f"{base_url}{path}"  # Builds the full request URL.
        response = self.session.get(url, params=params or {}, timeout=self.timeout_seconds)  # Sends the GET request using the shared session.
        response.raise_for_status()  # Raises a requests exception for non-success HTTP status codes.
        return response.json()  # Returns the parsed JSON body to the caller.

    def _post(self, base_url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:  # Performs a POST request against one SmartAPI service group.
        url = f"{base_url}{path}"  # Builds the full request URL.
        response = self.session.post(url, json=body, timeout=self.timeout_seconds)  # Sends the JSON POST request using the shared session.
        response.raise_for_status()  # Raises a requests exception for non-success HTTP status codes.
        return response.json()  # Returns the parsed JSON body to the caller.

    @staticmethod
    def _require_success(payload: dict[str, Any], action: str) -> dict[str, Any]:  # Normalizes SmartAPI responses by rejecting explicit API-level failures.
        if payload.get("status") is False:  # Detects SmartAPI responses that succeeded at HTTP level but failed at API level.
            message = payload.get("message") or payload.get("errorMessage") or f"{action} failed"  # Extracts the most useful error message from the payload.
            raise ValueError(message)  # Raises a standard error so callers can handle SmartAPI failures consistently.
        return payload  # Returns the payload unchanged when SmartAPI reports success.

    def place_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:  # Places an order or simulates one in paper mode.
        if self.paper_trade:  # Short-circuits live broker traffic when paper mode is enabled.
            return {"broker": "angelone", "status": "PAPER_FILLED", **order_payload}

        payload = self._require_success(self._post(self.ORDER_BASE_URL, "/placeOrder", order_payload), "place order")  # Sends the live order payload to SmartAPI.
        return {"broker": "angelone", "status": "PLACED", "response": payload, **order_payload}  # Returns the broker payload alongside the local order fields.

    def get_order_book(self) -> dict[str, Any]:  # Fetches current active orders for the account.
        if self.paper_trade:
            return {"broker": "angelone", "orders": []}
        return self._require_success(self._get(self.ORDER_BASE_URL, "/getOrderBook"), "fetch order book")

    def get_trade_book(self) -> dict[str, Any]:  # Fetches executed trade history for the account.
        if self.paper_trade:
            return {"broker": "angelone", "trades": []}
        return self._require_success(self._get(self.TRADE_BASE_URL, "/getTradeBook"), "fetch trade book")

    def get_holdings(self) -> dict[str, Any]:  # Fetches current holdings from the account.
        if self.paper_trade:
            return {"broker": "angelone", "holdings": []}
        return self._require_success(self._get(self.PORTFOLIO_BASE_URL, "/getHolding"), "fetch holdings")

    def get_historical_orders(self, from_date: str = "", to_date: str = "") -> dict[str, Any]:  # Fetches historical orders between dates.
        if self.paper_trade:
            return {"broker": "angelone", "historical_orders": []}

        params: dict[str, str] = {}  # Builds the optional from/to query parameters.
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date

        return self._require_success(self._get(self.ORDER_BASE_URL, "/getHistory", params=params), "fetch historical orders")

    def get_historical_trades(self, from_date: str = "", to_date: str = "") -> dict[str, Any]:  # Fetches historical trades between dates.
        if self.paper_trade:
            return {"broker": "angelone", "historical_trades": []}

        params: dict[str, str] = {}
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date

        return self._require_success(self._get(self.TRADE_BASE_URL, "/getHistory", params=params), "fetch historical trades")

    def search_scrip(self, exchange: str, search_text: str) -> list[dict[str, Any]]:  # Searches Angel One's instrument master for a tradingsymbol/token pair.
        if self.paper_trade:
            return []
        payload = self._require_success(
            self._post(
                self.MARKET_BASE_URL,
                "/searchScrip",
                {"exchange": exchange, "searchscrip": search_text},
            ),
            "search scrip",
        )
        return payload.get("data") or []  # Returns the broker's search results or an empty list when none are found.

    def resolve_instrument(self, symbol: str, exchange: str = "NSE") -> Instrument:  # Resolves a project symbol into broker tradingsymbol and token details.
        tradingsymbol = angel_tradingsymbol_for(symbol)  # Converts the app symbol into the expected Angel One equity tradingsymbol.
        matches = self.search_scrip(exchange, tradingsymbol)  # Searches SmartAPI for the exact tradingsymbol first.
        exact = next((row for row in matches if str(row.get("tradingsymbol", "")).upper() == tradingsymbol.upper()), None)  # Prefers an exact tradingsymbol match when available.
        if exact is None and not matches:  # Falls back to the bare base symbol when the exact lookup returns nothing.
            base_symbol = tradingsymbol.split("-", 1)[0]
            matches = self.search_scrip(exchange, base_symbol)
            exact = next((row for row in matches if str(row.get("tradingsymbol", "")).upper() == tradingsymbol.upper()), None)

        candidate = exact or (matches[0] if matches else None)  # Uses the exact match when possible and otherwise the first returned row.
        if candidate is None:  # Fails fast when SmartAPI cannot resolve the symbol.
            raise ValueError(f"Could not resolve Angel One instrument for {symbol}")

        return Instrument(
            symbol=symbol,
            exchange=str(candidate.get("exchange") or exchange),
            tradingsymbol=str(candidate["tradingsymbol"]),
            symboltoken=str(candidate["symboltoken"]),
        )

    def get_candle_data(
        self,
        exchange: str,
        symboltoken: str,
        interval: str,
        from_datetime: datetime | str,
        to_datetime: datetime | str,
    ) -> list[list[Any]]:  # Fetches historical candles in the SmartAPI array format.
        if self.paper_trade:
            return []

        payload = self._require_success(
            self._post(
                self.HISTORICAL_BASE_URL,
                "/getCandleData",
                {
                    "exchange": exchange,
                    "symboltoken": str(symboltoken),
                    "interval": interval,
                    "fromdate": self._format_datetime(from_datetime),
                    "todate": self._format_datetime(to_datetime),
                },
            ),
            "fetch candle data",
        )
        return payload.get("data") or []

    def get_ltp_data(self, exchange: str, tradingsymbol: str, symboltoken: str) -> dict[str, Any]:  # Fetches the latest traded price snapshot for one symbol.
        if self.paper_trade:
            return {"ltp": None}
        payload = self._require_success(
            self._post(
                self.MARKET_BASE_URL,
                "/getLtpData",
                {
                    "exchange": exchange,
                    "tradingsymbol": tradingsymbol,
                    "symboltoken": str(symboltoken),
                },
            ),
            "fetch ltp",
        )
        return payload.get("data") or {}

    def get_market_data(self, mode: str, exchange_tokens: dict[str, list[str]]) -> dict[str, Any]:  # Fetches snapshot quote data for one or more exchange/token groups.
        if self.paper_trade:
            return {"fetched": []}
        payload = self._require_success(
            self._post(
                self.MARKET_BASE_URL,
                "/quote",
                {
                    "mode": mode,
                    "exchangeTokens": exchange_tokens,
                },
            ),
            "fetch market data",
        )
        return payload.get("data") or {}

    @staticmethod
    def _format_datetime(value: datetime | str) -> str:  # Formats SmartAPI candle timestamps as yyyy-mm-dd HH:MM.
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M")
        return value

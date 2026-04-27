from __future__ import annotations  # Lets Python postpone evaluation of type annotations.

from datetime import datetime  # Imports datetime for historical candle request windows.
import socket  # Imports socket for deriving the client-local IP used by SmartAPI headers.
from typing import Any  # Imports Any for JSON-like response typing.
import uuid  # Imports uuid for deriving a stable MAC-address style identifier used by SmartAPI headers.

import pyotp  # Imports pyotp for generating TOTP codes during auto-login.
import requests  # Imports requests for SmartAPI HTTP calls.
from requests.exceptions import JSONDecodeError as RequestsJSONDecodeError  # Imports requests' JSON decode error so non-JSON API responses can be reported clearly.

from config.instruments import Instrument, angel_tradingsymbol_for  # Imports broker instrument helpers used by market data and order placement.


class AngelOneClient:  # Defines a lightweight wrapper around Angel One SmartAPI endpoints used by the bot.
    """Angel One SmartAPI integration for market data and order placement."""

    AUTH_BASE_URL = "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1"  # Stores the SmartAPI authentication endpoint base URL.
    ORDER_BASE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/order/v1"  # Stores the SmartAPI order endpoint base URL.
    PORTFOLIO_BASE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/portfolio/v1"  # Stores the SmartAPI portfolio endpoint base URL.
    TRADE_BASE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/trade/v1"  # Stores the SmartAPI trade endpoint base URL.
    HISTORICAL_BASE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/historical/v1"  # Stores the SmartAPI historical-candle endpoint base URL.
    MARKET_BASE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/market/v1"  # Stores the SmartAPI quote/search endpoint base URL.
    SCRIP_MASTER_URLS = (  # Stores public Angel One scrip-master locations used as a fallback when searchScrip yields no match.
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    )

    def __init__(
        self,
        api_key: str,
        client_id: str,
        access_token: str,
        timeout_seconds: int = 10,
    ) -> None:  # Stores the broker credentials needed for API access.
        self.api_key = api_key  # Stores the configured API key for SmartAPI headers.
        self.client_id = client_id  # Stores the client code used by SmartAPI.
        self.access_token = access_token  # Stores the JWT access token used for authenticated requests.
        self.timeout_seconds = timeout_seconds  # Stores a default timeout for SmartAPI requests.
        self.session = requests.Session()  # Reuses one HTTP session across requests.
        adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=10)  # Increases connection pool for concurrent requests.
        self.session.mount("https://", adapter)
        self.client_local_ip = self._detect_local_ip()  # Stores the client-local IP in the header format used by the SmartAPI SDK.
        self.client_public_ip = "106.193.147.98"  # Uses the same fallback public IP placeholder the official SDK currently ships with.
        self.client_mac_address = ":".join(f"{uuid.getnode():012x}"[i : i + 2] for i in range(0, 12, 2))  # Formats the machine identifier like the official SDK's MAC header.
        self._scrip_master_cache: list[dict[str, Any]] | None = None  # Caches the downloaded scrip master so symbol fallback resolution happens only once per process.
        self.session.headers.update(
            {
                "Accept": "application/json",  # Requests JSON responses from SmartAPI.
                "Content-Type": "application/json",  # Sends JSON payloads for POST requests.
                "X-ClientLocalIP": self.client_local_ip,  # Provides the local IP header expected by the SmartAPI SDK.
                "X-ClientPublicIP": self.client_public_ip,  # Provides the public IP header expected by the SmartAPI SDK.
                "X-MACAddress": self.client_mac_address,  # Provides the MAC-address style header expected by the SmartAPI SDK.
                "X-Client-Code": self.client_id,  # Provides the client code in the common SmartAPI header form.
                "X-API-Key": self.api_key,  # Provides the API key in the header used by this project's existing implementation.
                "X-PrivateKey": self.api_key,  # Provides the API key in the header used by SmartAPI SDK examples and forum traces.
                "X-UserType": "USER",  # Marks the request as coming from a user session.
                "X-SourceID": "WEB",  # Uses the default SmartAPI web source identifier.
                "Authorization": f"Bearer {self.access_token}",  # Authenticates requests with the JWT access token.
            }
        )

    @classmethod
    def login(
        cls,
        api_key: str,
        client_id: str,
        pin: str,
        totp_secret: str,
        timeout_seconds: int = 10,
    ) -> "AngelOneClient":  # Authenticates with Angel One and returns a client with a fresh access token.
        """Generate a session token via SmartAPI login and return an initialized client."""
        totp = pyotp.TOTP(totp_secret).now()  # Generates the current 6-digit TOTP code from the secret.

        local_ip = cls._detect_local_ip()
        mac_address = ":".join(f"{uuid.getnode():012x}"[i : i + 2] for i in range(0, 12, 2))

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-ClientLocalIP": local_ip,
            "X-ClientPublicIP": "106.193.147.98",
            "X-MACAddress": mac_address,
            "X-PrivateKey": api_key,
            "X-UserType": "USER",
            "X-SourceID": "WEB",
        }

        response = requests.post(
            f"{cls.AUTH_BASE_URL}/loginByPassword",
            json={"clientcode": client_id, "password": pin, "totp": totp},
            headers=headers,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = cls._parse_json_response(response, "POST", f"{cls.AUTH_BASE_URL}/loginByPassword")

        if not payload.get("status"):
            raise ValueError(payload.get("message") or "Angel One login failed")

        data = payload.get("data") or {}
        access_token = data.get("jwtToken")
        if not access_token:
            raise ValueError("Angel One login succeeded but no jwtToken was returned")

        return cls(
            api_key=api_key,
            client_id=client_id,
            access_token=access_token,
            timeout_seconds=timeout_seconds,
        )

    def _get(self, base_url: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:  # Performs a GET request against one SmartAPI service group.
        url = f"{base_url}{path}"  # Builds the full request URL.
        response = self.session.get(url, params=params or {}, timeout=self.timeout_seconds)  # Sends the GET request using the shared session.
        response.raise_for_status()  # Raises a requests exception for non-success HTTP status codes.
        return self._parse_json_response(response, "GET", url)  # Returns the parsed JSON body to the caller.

    def _post(self, base_url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:  # Performs a POST request against one SmartAPI service group.
        url = f"{base_url}{path}"  # Builds the full request URL.
        response = self.session.post(url, json=body, timeout=self.timeout_seconds)  # Sends the JSON POST request using the shared session.
        response.raise_for_status()  # Raises a requests exception for non-success HTTP status codes.
        return self._parse_json_response(response, "POST", url)  # Returns the parsed JSON body to the caller.

    @staticmethod
    def _require_success(payload: dict[str, Any], action: str) -> dict[str, Any]:  # Normalizes SmartAPI responses by rejecting explicit API-level failures.
        if payload.get("status") is False:  # Detects SmartAPI responses that succeeded at HTTP level but failed at API level.
            message = payload.get("message") or payload.get("errorMessage") or f"{action} failed"  # Extracts the most useful error message from the payload.
            raise ValueError(message)  # Raises a standard error so callers can handle SmartAPI failures consistently.
        return payload  # Returns the payload unchanged when SmartAPI reports success.

    def place_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:  # Places a live order through SmartAPI.
        payload = self._require_success(self._post(self.ORDER_BASE_URL, "/placeOrder", order_payload), "place order")  # Sends the live order payload to SmartAPI.
        return {"broker": "angelone", "status": "PLACED", "response": payload, **order_payload}  # Returns the broker payload alongside the local order fields.

    USER_BASE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/user/v1"  # Stores the SmartAPI user/RMS endpoint base URL.

    def get_available_capital(self) -> float:  # Fetches available intraday capital from the account.
        payload = self._require_success(self._get(self.USER_BASE_URL, "/getRMS"), "fetch RMS")
        data = payload.get("data") or {}
        # Use available cash + intraday payin as total available capital.
        cash = float(data.get("availablecash", 0))
        intraday = float(data.get("availableintradaypayin", 0))
        margin = float(data.get("availablelimitmargin", 0))
        return cash + intraday + margin

    def get_order_book(self) -> dict[str, Any]:  # Fetches current active orders for the account.
        return self._require_success(self._get(self.ORDER_BASE_URL, "/getOrderBook"), "fetch order book")

    def get_trade_book(self) -> dict[str, Any]:  # Fetches executed trade history for the account.
        return self._require_success(self._get(self.TRADE_BASE_URL, "/getTradeBook"), "fetch trade book")

    def get_holdings(self) -> dict[str, Any]:  # Fetches current holdings from the account.
        return self._require_success(self._get(self.PORTFOLIO_BASE_URL, "/getHolding"), "fetch holdings")

    def get_historical_orders(self, from_date: str = "", to_date: str = "") -> dict[str, Any]:  # Fetches historical orders between dates.
        params: dict[str, str] = {}  # Builds the optional from/to query parameters.
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date

        return self._require_success(self._get(self.ORDER_BASE_URL, "/getHistory", params=params), "fetch historical orders")

    def get_historical_trades(self, from_date: str = "", to_date: str = "") -> dict[str, Any]:  # Fetches historical trades between dates.
        params: dict[str, str] = {}
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date

        return self._require_success(self._get(self.TRADE_BASE_URL, "/getHistory", params=params), "fetch historical trades")

    def search_scrip(self, exchange: str, search_text: str) -> list[dict[str, Any]]:  # Searches Angel One's instrument master for a tradingsymbol/token pair.
        payload = self._require_success(
            self._post(
                self.ORDER_BASE_URL,
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
        if candidate is None:  # Falls back to the public scrip master when the search endpoint returns no usable result.
            candidate = self._resolve_from_scrip_master(symbol=symbol, exchange=exchange, tradingsymbol=tradingsymbol)
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
        payload = self._require_success(
            self._post(
                self.ORDER_BASE_URL,
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

    @staticmethod
    def _detect_local_ip() -> str:  # Detects the machine's local IP address for SmartAPI headers and falls back safely when unavailable.
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"

    @staticmethod
    def _parse_json_response(response: requests.Response, method: str, url: str) -> dict[str, Any]:  # Converts an HTTP response to JSON and reports non-JSON bodies with enough detail to debug SmartAPI routing/auth issues.
        try:
            return response.json()
        except (RequestsJSONDecodeError, ValueError) as exc:
            content_type = response.headers.get("Content-Type", "unknown")
            snippet = response.text.strip().replace("\n", " ")[:200]
            hint = ""
            if "Request Rejected" in response.text:
                hint = (
                    " This usually means Angel One's gateway blocked the request."
                    " Verify the SmartAPI app's Primary Static IP matches the machine's public IP,"
                    " and confirm the request is using the documented endpoint for this API."
                )
            raise ValueError(
                f"{method} {url} returned non-JSON response (status={response.status_code}, content_type={content_type}): {snippet or '<empty body>'}{hint}"
            ) from exc

    def _resolve_from_scrip_master(self, symbol: str, exchange: str, tradingsymbol: str) -> dict[str, Any] | None:  # Resolves a symbol from Angel One's public instrument master as a fallback to searchScrip.
        base_symbol = tradingsymbol.split("-", 1)[0]  # Extracts the root equity symbol used by the public master data.
        for row in self._load_scrip_master():  # Scans the cached master data for an exact match on exchange and tradingsymbol.
            row_exchange = str(row.get("exch_seg", "")).upper()
            row_symbol = str(row.get("symbol", "")).upper()
            row_name = str(row.get("name", "")).upper()
            if row_exchange != exchange.upper():
                continue
            if row_symbol == tradingsymbol.upper() or (row_name == base_symbol.upper() and row_symbol.endswith("-EQ")):
                return {
                    "exchange": row_exchange,
                    "tradingsymbol": row.get("symbol"),
                    "symboltoken": row.get("token"),
                }
        return None

    def _load_scrip_master(self) -> list[dict[str, Any]]:  # Loads and caches the public Angel One scrip master JSON for token-resolution fallback.
        if self._scrip_master_cache is not None:
            return self._scrip_master_cache

        last_error: Exception | None = None
        for url in self.SCRIP_MASTER_URLS:
            try:
                response = requests.get(url, timeout=self.timeout_seconds)
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, list):
                    self._scrip_master_cache = payload
                    return payload
            except Exception as exc:  # Tries the next known scrip-master URL if one host is unavailable.
                last_error = exc

        if last_error is not None:
            raise ValueError(f"Unable to load Angel One scrip master: {last_error}") from last_error
        raise ValueError("Unable to load Angel One scrip master")

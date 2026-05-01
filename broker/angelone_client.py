from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pyotp
import requests
from requests import HTTPError
from requests.exceptions import JSONDecodeError as RequestsJSONDecodeError

import uuid

from config.instruments import Instrument, angel_tradingsymbol_for

logger = logging.getLogger("algo_trader")

# ---------------------------------------------------------------------------
# Rate limiter — enforces max N requests per second across all threads.
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Token-bucket rate limiter. Thread-safe."""

    def __init__(self, max_per_second: float = 8.0) -> None:
        self._interval = 1.0 / max_per_second
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


# Module-level rate limiter shared by all client instances.
_rate_limiter = _RateLimiter(max_per_second=8.0)

# Retry configuration.
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds; doubles each retry.
_AUTH_FAIL_CODES = {401, 403}
_RETRYABLE_CODES = {429, 500, 502, 503, 504}

# Scrip master disk cache.
_SCRIP_CACHE_DIR = Path(__file__).resolve().parent.parent / "data"
_SCRIP_CACHE_FILE = _SCRIP_CACHE_DIR / ".scrip_master_cache.json"
_SCRIP_CACHE_MAX_AGE = 24 * 3600  # 24 hours.


class AngelOneClient:
    """Angel One SmartAPI client with retry, rate limiting, and auto re-login."""

    API_ROOT_URL = "https://apiconnect.angelone.in"
    AUTH_BASE_URL = "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1"
    ORDER_BASE_URL = f"{API_ROOT_URL}/rest/secure/angelbroking/order/v1"
    PORTFOLIO_BASE_URL = f"{API_ROOT_URL}/rest/secure/angelbroking/portfolio/v1"
    TRADE_BASE_URL = f"{API_ROOT_URL}/rest/secure/angelbroking/trade/v1"
    HISTORICAL_BASE_URL = f"{API_ROOT_URL}/rest/secure/angelbroking/historical/v1"
    MARKET_BASE_URL = f"{API_ROOT_URL}/rest/secure/angelbroking/market/v1"
    USER_BASE_URL = f"{API_ROOT_URL}/rest/secure/angelbroking/user/v1"
    SCRIP_MASTER_URLS = (
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
        "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
    )

    def __init__(
        self,
        api_key: str,
        client_id: str,
        access_token: str,
        timeout_seconds: int = 10,
        *,
        _pin: str = "",
        _totp_secret: str = "",
    ) -> None:
        self.api_key = api_key
        self.client_id = client_id
        self.access_token = access_token
        self.timeout_seconds = timeout_seconds

        # Stored for auto re-login on auth failure.
        self._pin = _pin
        self._totp_secret = _totp_secret
        self._login_lock = threading.Lock()
        self._last_login_time = time.monotonic()

        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=10)
        self.session.mount("https://", adapter)

        self.client_local_ip = self._resolve_local_ip()
        self.client_public_ip = self._resolve_public_ip(timeout_seconds=self.timeout_seconds)
        self.client_mac_address = self._resolve_mac_address()
        self._scrip_master_cache: list[dict[str, Any]] | None = None

        self._update_session_headers()

    def _update_session_headers(self) -> None:
        """Set/refresh session headers with current access token."""
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-ClientLocalIP": self.client_local_ip,
                "X-ClientPublicIP": self.client_public_ip,
                "X-MACAddress": self.client_mac_address,
                "X-Client-Code": self.client_id,
                "X-API-Key": self.api_key,
                "X-PrivateKey": self.api_key,
                "X-UserType": "USER",
                "X-SourceID": "WEB",
                "Authorization": f"Bearer {self.access_token}",
            }
        )

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @classmethod
    def login(
        cls,
        api_key: str,
        client_id: str,
        pin: str,
        totp_secret: str,
        timeout_seconds: int = 10,
    ) -> "AngelOneClient":
        """Authenticate and return a client with a fresh JWT."""
        totp = pyotp.TOTP(totp_secret).now()

        local_ip = cls._resolve_local_ip()
        public_ip = cls._resolve_public_ip(timeout_seconds=timeout_seconds)
        mac_address = cls._resolve_mac_address()

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-ClientLocalIP": local_ip,
            "X-ClientPublicIP": public_ip,
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
            _pin=pin,
            _totp_secret=totp_secret,
        )

    def refresh_session(self) -> None:
        """Re-login and update the access token. Thread-safe."""
        with self._login_lock:
            if not self._pin or not self._totp_secret:
                raise RuntimeError("Cannot refresh session: login credentials not stored")
            logger.info("[auth_refresh] Re-authenticating with Angel One...")
            totp = pyotp.TOTP(self._totp_secret).now()
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-ClientLocalIP": self.client_local_ip,
                "X-ClientPublicIP": self.client_public_ip,
                "X-MACAddress": self.client_mac_address,
                "X-PrivateKey": self.api_key,
                "X-UserType": "USER",
                "X-SourceID": "WEB",
            }
            response = requests.post(
                f"{self.AUTH_BASE_URL}/loginByPassword",
                json={"clientcode": self.client_id, "password": self._pin, "totp": totp},
                headers=headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = self._parse_json_response(response, "POST", f"{self.AUTH_BASE_URL}/loginByPassword")
            if not payload.get("status"):
                raise ValueError(payload.get("message") or "Re-login failed")
            data = payload.get("data") or {}
            new_token = data.get("jwtToken")
            if not new_token:
                raise ValueError("Re-login succeeded but no jwtToken returned")
            self.access_token = new_token
            self._update_session_headers()
            self._last_login_time = time.monotonic()
            logger.info("[auth_refresh] Session refreshed successfully.")

    def refresh_if_stale(self, max_age_seconds: float = 7200) -> None:
        """Re-login if the current session is older than max_age_seconds (default 2h)."""
        elapsed = time.monotonic() - self._last_login_time
        if elapsed >= max_age_seconds:
            logger.info("[auth_refresh] Session age %.0fs >= %.0fs, refreshing...", elapsed, max_age_seconds)
            self.refresh_session()

    # ------------------------------------------------------------------
    # Core HTTP with retry, rate limiting, and auto re-login
    # ------------------------------------------------------------------

    def _get(self, base_url: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_with_retry("GET", base_url, path, params=params)

    def _post(self, base_url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request_with_retry("POST", base_url, path, body=body)

    def _request_with_retry(
        self,
        method: str,
        base_url: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an HTTP request with rate limiting, retry, and auto re-login."""
        url = f"{base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            _rate_limiter.acquire()

            try:
                if method == "GET":
                    response = self.session.get(url, params=params or {}, timeout=self.timeout_seconds)
                else:
                    response = self.session.post(url, json=body, timeout=self.timeout_seconds)

                # Auth failure — try re-login once, then retry.
                if response.status_code in _AUTH_FAIL_CODES:
                    if attempt < _MAX_RETRIES - 1 and self._pin and self._totp_secret:
                        logger.warning(
                            "[auth_refresh] %s %s returned %d, re-authenticating (attempt %d/%d)...",
                            method, path, response.status_code, attempt + 1, _MAX_RETRIES,
                        )
                        try:
                            self.refresh_session()
                        except Exception as login_exc:
                            logger.error("[auth_refresh] Re-login failed: %s", login_exc)
                        backoff = _BACKOFF_BASE * (2 ** attempt)
                        time.sleep(backoff)
                        continue
                    self._raise_for_status(response, method, url)

                # Retryable server/rate-limit errors.
                if response.status_code in _RETRYABLE_CODES:
                    if attempt < _MAX_RETRIES - 1:
                        backoff = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "[rate_limit_hit] %s %s returned %d, retrying in %.1fs (attempt %d/%d)...",
                            method, path, response.status_code, backoff, attempt + 1, _MAX_RETRIES,
                        )
                        time.sleep(backoff)
                        continue
                    self._raise_for_status(response, method, url)

                # Any other HTTP error.
                self._raise_for_status(response, method, url)
                return self._parse_json_response(response, method, url)

            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    backoff = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "[retry] %s %s network error: %s, retrying in %.1fs (attempt %d/%d)...",
                        method, path, exc, backoff, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(backoff)
                    continue
                raise

        if last_exc:
            raise last_exc
        raise RuntimeError(f"Request failed after {_MAX_RETRIES} attempts: {method} {url}")

    # ------------------------------------------------------------------
    # Business methods (unchanged signatures)
    # ------------------------------------------------------------------

    @staticmethod
    def _require_success(payload: dict[str, Any], action: str) -> dict[str, Any]:
        if payload.get("status") is False:
            message = payload.get("message") or payload.get("errorMessage") or f"{action} failed"
            raise ValueError(message)
        return payload

    def place_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._require_success(self._post(self.ORDER_BASE_URL, "/placeOrder", order_payload), "place order")
        return {"broker": "angelone", "status": "PLACED", "response": payload, **order_payload}

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> dict[str, Any]:
        payload = self._require_success(
            self._post(self.ORDER_BASE_URL, "/cancelOrder", {"variety": variety, "orderid": order_id}),
            "cancel order",
        )
        return payload

    def modify_order(self, order_payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._require_success(
            self._post(self.ORDER_BASE_URL, "/modifyOrder", order_payload),
            "modify order",
        )
        return payload

    def get_available_capital(self) -> float:
        payload = self._require_success(self._get(self.USER_BASE_URL, "/getRMS"), "fetch RMS")
        data = payload.get("data") or {}
        cash = float(data.get("availablecash", 0))
        intraday = float(data.get("availableintradaypayin", 0))
        margin = float(data.get("availablelimitmargin", 0))
        return cash + intraday + margin

    def get_order_book(self) -> dict[str, Any]:
        return self._require_success(self._get(self.ORDER_BASE_URL, "/getOrderBook"), "fetch order book")

    def get_trade_book(self) -> dict[str, Any]:
        return self._require_success(self._get(self.TRADE_BASE_URL, "/getTradeBook"), "fetch trade book")

    def get_holdings(self) -> dict[str, Any]:
        return self._require_success(self._get(self.PORTFOLIO_BASE_URL, "/getHolding"), "fetch holdings")

    def get_historical_orders(self, from_date: str = "", to_date: str = "") -> dict[str, Any]:
        params: dict[str, str] = {}
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date
        return self._require_success(self._get(self.ORDER_BASE_URL, "/getHistory", params=params), "fetch historical orders")

    def get_historical_trades(self, from_date: str = "", to_date: str = "") -> dict[str, Any]:
        params: dict[str, str] = {}
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date
        return self._require_success(self._get(self.TRADE_BASE_URL, "/getHistory", params=params), "fetch historical trades")

    def search_scrip(self, exchange: str, search_text: str) -> list[dict[str, Any]]:
        payload = self._require_success(
            self._post(self.ORDER_BASE_URL, "/searchScrip", {"exchange": exchange, "searchscrip": search_text}),
            "search scrip",
        )
        return payload.get("data") or []

    def resolve_instrument(self, symbol: str, exchange: str = "NSE") -> Instrument:
        tradingsymbol = angel_tradingsymbol_for(symbol)
        matches = self._safe_search_scrip(exchange, tradingsymbol)
        exact = next((row for row in matches if str(row.get("tradingsymbol", "")).upper() == tradingsymbol.upper()), None)
        if exact is None and not matches:
            base_symbol = tradingsymbol.split("-", 1)[0]
            matches = self._safe_search_scrip(exchange, base_symbol)
            exact = next((row for row in matches if str(row.get("tradingsymbol", "")).upper() == tradingsymbol.upper()), None)

        candidate = exact or (matches[0] if matches else None)
        if candidate is None:
            candidate = self._resolve_from_scrip_master(symbol=symbol, exchange=exchange, tradingsymbol=tradingsymbol)
        if candidate is None:
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
    ) -> list[list[Any]]:
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

    def get_ltp_data(self, exchange: str, tradingsymbol: str, symboltoken: str) -> dict[str, Any]:
        payload = self._require_success(
            self._post(
                self.ORDER_BASE_URL,
                "/getLtpData",
                {"exchange": exchange, "tradingsymbol": tradingsymbol, "symboltoken": str(symboltoken)},
            ),
            "fetch ltp",
        )
        return payload.get("data") or {}

    def get_market_data(self, mode: str, exchange_tokens: dict[str, list[str]]) -> dict[str, Any]:
        payload = self._require_success(
            self._post(
                self.MARKET_BASE_URL,
                "/quote",
                {"mode": mode, "exchangeTokens": exchange_tokens},
            ),
            "fetch market data",
        )
        return payload.get("data") or {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _safe_search_scrip(self, exchange: str, search_text: str) -> list[dict[str, Any]]:
        try:
            return self.search_scrip(exchange, search_text)
        except Exception:
            return []

    @staticmethod
    def _format_datetime(value: datetime | str) -> str:
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M")
        return value

    @staticmethod
    def _detect_local_ip() -> str:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"

    @classmethod
    def _resolve_local_ip(cls) -> str:
        configured = os.getenv("ANGELONE_CLIENT_LOCAL_IP", "").strip()
        if configured:
            return configured
        return cls._detect_local_ip()

    @staticmethod
    def _resolve_mac_address() -> str:
        configured = os.getenv("ANGELONE_CLIENT_MAC_ADDRESS", "").strip()
        if configured:
            return configured
        return ":".join(f"{uuid.getnode():012x}"[i : i + 2] for i in range(0, 12, 2))

    @staticmethod
    def _detect_public_ip(timeout_seconds: int = 10) -> str | None:
        sources = ("https://api.ipify.org", "https://checkip.amazonaws.com")
        for source in sources:
            try:
                response = requests.get(source, timeout=min(timeout_seconds, 5))
                response.raise_for_status()
                value = response.text.strip()
                if value:
                    return value
            except Exception:
                continue
        return None

    @classmethod
    def _resolve_public_ip(cls, timeout_seconds: int = 10) -> str:
        configured = os.getenv("ANGELONE_CLIENT_PUBLIC_IP", "").strip()
        if configured:
            return configured
        detected = cls._detect_public_ip(timeout_seconds=timeout_seconds)
        if detected:
            return detected
        return "106.193.147.98"

    @staticmethod
    def _parse_json_response(response: requests.Response, method: str, url: str) -> dict[str, Any]:
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

    def _raise_for_status(self, response: requests.Response, method: str, url: str) -> None:
        try:
            response.raise_for_status()
        except HTTPError as exc:
            content_type = response.headers.get("Content-Type", "unknown")
            snippet = response.text.strip().replace("\n", " ")[:200]
            raise HTTPError(
                f"{method} {url} failed with status={response.status_code}, content_type={content_type}, "
                f"client_public_ip={self.client_public_ip}, client_local_ip={self.client_local_ip}: "
                f"{snippet or '<empty body>'}"
            ) from exc

    def _resolve_from_scrip_master(self, symbol: str, exchange: str, tradingsymbol: str) -> dict[str, Any] | None:
        base_symbol = tradingsymbol.split("-", 1)[0]
        for row in self._load_scrip_master():
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

    def _load_scrip_master(self) -> list[dict[str, Any]]:
        """Load scrip master with disk caching (24h TTL)."""
        if self._scrip_master_cache is not None:
            return self._scrip_master_cache

        # Try disk cache first.
        if _SCRIP_CACHE_FILE.exists():
            age = time.time() - _SCRIP_CACHE_FILE.stat().st_mtime
            if age < _SCRIP_CACHE_MAX_AGE:
                try:
                    data = json.loads(_SCRIP_CACHE_FILE.read_text(encoding="utf-8"))
                    if isinstance(data, list) and data:
                        logger.info("Loaded scrip master from disk cache (age=%.0fs).", age)
                        self._scrip_master_cache = data
                        return data
                except Exception:
                    pass  # Corrupted cache — re-download.

        # Download from Angel One.
        last_error: Exception | None = None
        for url in self.SCRIP_MASTER_URLS:
            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, list):
                    self._scrip_master_cache = payload
                    # Write to disk cache.
                    try:
                        _SCRIP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                        _SCRIP_CACHE_FILE.write_text(json.dumps(payload), encoding="utf-8")
                        logger.info("Scrip master cached to disk (%d entries).", len(payload))
                    except Exception as write_exc:
                        logger.warning("Failed to write scrip master cache: %s", write_exc)
                    return payload
            except Exception as exc:
                last_error = exc

        if last_error is not None:
            raise ValueError(f"Unable to load Angel One scrip master: {last_error}") from last_error
        raise ValueError("Unable to load Angel One scrip master")

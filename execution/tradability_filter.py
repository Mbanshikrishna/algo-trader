from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger("algo_trader")

# Broker rejection messages that indicate untradable stocks.
REJECTION_KEYWORDS = [
    "cautionary",
    "asm",
    "gsm",
    "t2t",
    "trade to trade",
    "surveillance",
    "restricted",
    "not allowed",
    "suspended",
    "circuit",
    "be category",
    "block deal",
]

# NSE API endpoints for restricted stock lists.
NSE_ASM_URL = "https://www.nseindia.com/api/reportASM"
NSE_GSM_URL = "https://www.nseindia.com/api/reportGSM"
NSE_FNO_URL = "https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O"


class TradabilityFilter:
    """Filters out stocks that cannot be traded intraday due to exchange restrictions.

    Three layers of detection:
    1. NSE ASM/GSM lists (fetched once at startup)
    2. Circuit-limit heuristic (T2T/BE stocks have <=5% circuits)
    3. Runtime blacklist (caches broker rejections during the session)
    """

    def __init__(self, safe_mode: bool = True) -> None:
        self.safe_mode = safe_mode
        self._asm_symbols: set[str] = set()
        self._gsm_symbols: set[str] = set()
        self._fno_symbols: set[str] = set()
        self._session_blacklist: dict[str, str] = {}  # symbol -> reason
        self._loaded = False

    def load_restricted_lists(self) -> None:
        """Fetch ASM, GSM, and F&O lists from NSE. Safe to call multiple times."""
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })

        # NSE requires a cookie from the homepage first.
        try:
            session.get("https://www.nseindia.com", timeout=10)
        except Exception:
            pass

        # Fetch ASM list.
        try:
            resp = session.get(NSE_ASM_URL, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                lt_data = data.get("longterm", {})
                st_data = data.get("shortterm", {})
                lt_list = lt_data.get("data", []) if isinstance(lt_data, dict) else []
                st_list = st_data.get("data", []) if isinstance(st_data, dict) else []
                self._asm_symbols = {
                    s["symbol"] for s in lt_list if "symbol" in s
                } | {
                    s["symbol"] for s in st_list if "symbol" in s
                }
                logger.info("Loaded %d ASM stocks from NSE.", len(self._asm_symbols))
        except Exception as exc:
            logger.warning("Failed to fetch ASM list: %s", exc)

        # Fetch GSM list.
        try:
            resp = session.get(NSE_GSM_URL, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    self._gsm_symbols = {s["symbol"] for s in data if "symbol" in s}
                logger.info("Loaded %d GSM stocks from NSE.", len(self._gsm_symbols))
        except Exception as exc:
            logger.warning("Failed to fetch GSM list: %s", exc)

        # Fetch F&O universe (used in safe mode to restrict to liquid stocks).
        try:
            resp = session.get(NSE_FNO_URL, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                fno_data = data.get("data", []) if isinstance(data, dict) else []
                self._fno_symbols = {s["symbol"] for s in fno_data if "symbol" in s}
                logger.info("Loaded %d F&O stocks from NSE.", len(self._fno_symbols))
        except Exception as exc:
            logger.warning("Failed to fetch F&O list: %s", exc)

        session.close()
        self._loaded = True

    def _normalize_symbol(self, symbol: str) -> str:
        """Strip -EQ suffix to match NSE list format."""
        return symbol.replace("-EQ", "").replace(".NS", "").upper()

    def is_restricted(self, symbol: str) -> tuple[bool, str]:
        """Check if a stock is restricted from intraday trading.

        Returns (is_restricted, reason).
        """
        clean = self._normalize_symbol(symbol)

        # Check session blacklist first (fastest).
        if clean in self._session_blacklist:
            return True, self._session_blacklist[clean]
        if symbol in self._session_blacklist:
            return True, self._session_blacklist[symbol]

        # Check ASM list.
        if clean in self._asm_symbols:
            reason = f"ASM listed (Additional Surveillance Measure)"
            self._session_blacklist[clean] = reason
            return True, reason

        # Check GSM list.
        if clean in self._gsm_symbols:
            reason = f"GSM listed (Graded Surveillance Measure)"
            self._session_blacklist[clean] = reason
            return True, reason

        return False, ""

    def is_fno_stock(self, symbol: str) -> bool:
        """Check if a stock is in the F&O universe (high liquidity)."""
        clean = self._normalize_symbol(symbol)
        return clean in self._fno_symbols

    def check_circuit_limits(
        self, symbol: str, prev_close: float, lower_circuit: float, upper_circuit: float,
    ) -> tuple[bool, str]:
        """Detect T2T/BE stocks by their tight circuit limits.

        T2T/BE stocks typically have <=5% circuit bands.
        Returns (is_restricted, reason).
        """
        if prev_close <= 0:
            return False, ""

        lower_pct = ((prev_close - lower_circuit) / prev_close) * 100
        upper_pct = ((upper_circuit - prev_close) / prev_close) * 100

        if lower_pct <= 5.0 and upper_pct <= 5.0:
            reason = f"Tight circuit limits ({lower_pct:.1f}%/{upper_pct:.1f}%) — likely T2T/BE"
            clean = self._normalize_symbol(symbol)
            self._session_blacklist[clean] = reason
            return True, reason

        return False, ""

    def add_to_blacklist(self, symbol: str, reason: str) -> None:
        """Add a stock to the session blacklist after a broker rejection."""
        clean = self._normalize_symbol(symbol)
        self._session_blacklist[clean] = reason
        logger.warning("BLACKLISTED %s: %s", symbol, reason)

    def record_broker_rejection(self, symbol: str, error_message: str) -> bool:
        """Check if a broker error indicates an untradable stock and blacklist it.

        Returns True if the error was a tradability rejection.
        """
        error_lower = str(error_message).lower()
        for keyword in REJECTION_KEYWORDS:
            if keyword in error_lower:
                self.add_to_blacklist(symbol, f"Broker rejected: {error_message}")
                return True
        return False

    def filter_candidates(
        self,
        candidates: list[dict[str, Any]],
        fno_only: bool = False,
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        """Filter a list of stock candidates, removing restricted ones.

        Returns (tradable_candidates, skipped_with_reasons).
        """
        tradable: list[dict[str, Any]] = []
        skipped: list[tuple[str, str]] = []

        for candidate in candidates:
            symbol = candidate.get("symbol", "")

            # Check ASM/GSM/blacklist.
            restricted, reason = self.is_restricted(symbol)
            if restricted:
                skipped.append((symbol, reason))
                continue

            # Check circuit limits if available.
            prev_close = float(candidate.get("prev_close", 0))
            lower = float(candidate.get("lower_circuit", 0))
            upper = float(candidate.get("upper_circuit", 0))
            if lower > 0 and upper > 0 and prev_close > 0:
                restricted, reason = self.check_circuit_limits(symbol, prev_close, lower, upper)
                if restricted:
                    skipped.append((symbol, reason))
                    continue

            # F&O-only filter in safe mode.
            if fno_only and not self.is_fno_stock(symbol):
                skipped.append((symbol, "Not in F&O universe (safe mode)"))
                continue

            tradable.append(candidate)

        return tradable, skipped

    @property
    def blacklist_summary(self) -> dict[str, str]:
        """Return a copy of the current session blacklist."""
        return dict(self._session_blacklist)

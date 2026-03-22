from __future__ import annotations  # Lets Python postpone evaluation of type hints.

import os  # Imports access to environment variables.
from dataclasses import dataclass  # Imports the dataclass decorator for compact settings storage.
from pathlib import Path  # Imports Path for locating the project env file.


@dataclass(frozen=True)  # Creates an immutable data container for runtime settings.
class Settings:  # Defines the structure of all settings loaded from the environment.
    """Runtime settings loaded from environment variables."""

    api_key: str  # Stores the Angel One API key.
    client_id: str  # Stores the Angel One client identifier.
    access_token: str  # Stores the Angel One session access token.
    paper_trade: bool  # Controls whether the bot simulates trades instead of sending live orders.
    risk_per_trade_pct: float  # Stores the percentage of capital to risk on each trade.
    capital: float  # Stores the total capital used for position sizing.
    scan_interval_seconds: float  # Stores the delay between repeated scan cycles.
    alert_every_check: bool  # Controls whether Telegram should receive non-trade status updates too.
    market_data_provider: str  # Stores whether market data should come from Angel One or yfinance.
    order_product_type: str  # Stores the Angel One product type used for live orders.
    order_variety: str  # Stores the Angel One order variety used for live orders.


def _as_bool(value: str, default: bool = True) -> bool:  # Converts string-like environment values into booleans.
    if value is None:  # Handles the case where the environment variable is missing entirely.
        return default  # Falls back to the provided default value.
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}  # Treats common truthy strings as True.


def _load_env_file(env_path: Path | None = None) -> None:  # Loads key-value pairs from a local .env file into the process environment.
    if env_path is None:  # Handles the default project env-file location.
        env_path = Path(__file__).resolve().parent.parent / ".env"  # Points to the repository-level .env file.
    if not env_path.exists():  # Skips loading when no local env file is present.
        return  # Leaves the current environment unchanged when the file is missing.

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():  # Reads the env file line by line.
        line = raw_line.strip()  # Removes surrounding whitespace so parsing is predictable.
        if not line or line.startswith("#") or "=" not in line:  # Ignores blank lines, comments, and malformed entries.
            continue  # Moves to the next line when nothing usable is found.
        key, value = line.split("=", 1)  # Splits only on the first equals sign to preserve the rest of the value.
        key = key.strip()  # Normalizes surrounding whitespace on the environment key.
        value = value.strip().strip("'\"")  # Normalizes whitespace and surrounding quotes on the environment value.
        os.environ.setdefault(key, value)  # Preserves already-exported variables while backfilling from .env.


def load_settings() -> Settings:  # Builds a Settings object from environment variables.
    _load_env_file()  # Loads repository env variables before reading runtime settings.
    return Settings(  # Creates and returns the immutable settings snapshot.
        api_key=os.getenv("ANGELONE_API_KEY", ""),  # Reads the API key or defaults to an empty string.
        client_id=os.getenv("ANGELONE_CLIENT_ID", ""),  # Reads the client identifier or defaults to an empty string.
        access_token=os.getenv("ANGELONE_ACCESS_TOKEN", ""),  # Reads the access token or defaults to an empty string.
        paper_trade=_as_bool(os.getenv("PAPER_TRADE", "true")),  # Reads the paper-trading flag and converts it to a boolean.
        risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "1.0")),  # Reads the per-trade risk percentage and converts it to a float.
        capital=float(os.getenv("CAPITAL", "100000")),  # Reads the available capital and converts it to a float.
        scan_interval_seconds=float(os.getenv("SCAN_INTERVAL_SECONDS", "2")),  # Reads the delay between repeated scans and converts it to a float.
        alert_every_check=_as_bool(os.getenv("ALERT_EVERY_CHECK", "true")),  # Reads whether Telegram should receive updates even without trades.
        market_data_provider=os.getenv("MARKET_DATA_PROVIDER", "angelone").strip().lower(),  # Reads the configured market data provider and defaults to Angel One for broker-native trading.
        order_product_type=os.getenv("ORDER_PRODUCT_TYPE", "INTRADAY").strip().upper(),  # Reads the live-order product type used by Angel One.
        order_variety=os.getenv("ORDER_VARIETY", "NORMAL").strip().upper(),  # Reads the live-order variety used by Angel One.
    )  # Finishes constructing the settings object.

from __future__ import annotations  # Lets Python postpone evaluation of type hints.

import os  # Imports access to environment variables.
from dataclasses import dataclass  # Imports the dataclass decorator for compact settings storage.


@dataclass(frozen=True)  # Creates an immutable data container for runtime settings.
class Settings:  # Defines the structure of all settings loaded from the environment.
    """Runtime settings loaded from environment variables."""

    api_key: str  # Stores the Zerodha API key.
    api_secret: str  # Stores the Zerodha API secret.
    access_token: str  # Stores the Zerodha session access token.
    paper_trade: bool  # Controls whether the bot simulates trades instead of sending live orders.
    risk_per_trade_pct: float  # Stores the percentage of capital to risk on each trade.
    capital: float  # Stores the total capital used for position sizing.


def _as_bool(value: str, default: bool = True) -> bool:  # Converts string-like environment values into booleans.
    if value is None:  # Handles the case where the environment variable is missing entirely.
        return default  # Falls back to the provided default value.
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}  # Treats common truthy strings as True.


def load_settings() -> Settings:  # Builds a Settings object from environment variables.
    return Settings(  # Creates and returns the immutable settings snapshot.
        api_key=os.getenv("ZERODHA_API_KEY", ""),  # Reads the API key or defaults to an empty string.
        api_secret=os.getenv("ZERODHA_API_SECRET", ""),  # Reads the API secret or defaults to an empty string.
        access_token=os.getenv("ZERODHA_ACCESS_TOKEN", ""),  # Reads the access token or defaults to an empty string.
        paper_trade=_as_bool(os.getenv("PAPER_TRADE", "true")),  # Reads the paper-trading flag and converts it to a boolean.
        risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "1.0")),  # Reads the per-trade risk percentage and converts it to a float.
        capital=float(os.getenv("CAPITAL", "100000")),  # Reads the available capital and converts it to a float.
    )  # Finishes constructing the settings object.

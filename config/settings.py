from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from environment variables."""

    api_key: str
    api_secret: str
    access_token: str
    paper_trade: bool
    risk_per_trade_pct: float
    capital: float



def _as_bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}



def load_settings() -> Settings:
    return Settings(
        api_key=os.getenv("ZERODHA_API_KEY", ""),
        api_secret=os.getenv("ZERODHA_API_SECRET", ""),
        access_token=os.getenv("ZERODHA_ACCESS_TOKEN", ""),
        paper_trade=_as_bool(os.getenv("PAPER_TRADE", "true")),
        risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "1.0")),
        capital=float(os.getenv("CAPITAL", "100000")),
    )

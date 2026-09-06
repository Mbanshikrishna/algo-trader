from __future__ import annotations  # Lets Python postpone evaluation of type hints.

import os  # Imports access to environment variables.
from dataclasses import dataclass  # Imports the dataclass decorator for compact settings storage.
from pathlib import Path  # Imports Path for locating the project env file.


@dataclass(frozen=True)  # Creates an immutable data container for runtime settings.
class Settings:  # Defines the structure of all settings loaded from the environment.
    """Runtime settings loaded from environment variables."""

    # Broker selection: "angelone" or "dhan".
    broker: str
    api_key: str  # Stores the Angel One API key.
    client_id: str  # Stores the Angel One client identifier.
    pin: str  # Stores the Angel One login PIN for auto-login.
    totp_secret: str  # Stores the TOTP secret for generating login codes.
    # Dhan credentials (used when broker="dhan").
    dhan_client_id: str
    dhan_access_token: str
    risk_per_trade_pct: float  # Stores the percentage of capital to risk on each trade.
    capital: float  # Stores the total capital used for position sizing.
    scan_interval_seconds: float  # Stores the delay between repeated scan cycles.
    monitor_interval_seconds: float  # Delay between price checks during position monitoring (should be short).
    alert_every_check: bool  # Controls whether Telegram should receive non-trade status updates too.
    order_product_type: str  # Stores the Angel One product type used for live orders.
    order_variety: str  # Stores the Angel One order variety used for live orders.
    intraday_leverage: float  # Intraday margin multiplier (e.g. 5.0 for 5x leverage).
    max_consecutive_losses: int  # Stop trading for the day after this many consecutive losing trades.
    safe_mode: bool  # When True, applies ASM/GSM/T2T tradability filters.
    fno_only: bool  # When True, restricts trading to F&O stocks only (very conservative).
    paper_trade: bool  # When True, simulates orders instead of placing real ones.
    live_trading_enabled: bool  # Independent live-order kill switch.
    live_trading_confirmation: str  # Deliberate acknowledgement required for live orders.
    max_daily_loss_pct: float  # Maximum realized daily loss as a percentage of capital.
    intraday_target_pct: float  # Exit target measured from previous close, not entry.
    staged_protection_enabled: bool  # Enable progressive stop floors after favorable movement.
    strict_entry_policy_enabled: bool  # Promote completed-volume/persistence shadow checks.
    protection_cost_buffer_pct: float  # Profit above entry locked after +2% MFE.
    paper_slippage_bps: float  # Adverse simulated fill movement per order side.
    execution_fees_bps_per_side: float  # Modeled charges used by paper P&L and risk state.
    runtime_state_path: str  # Persistent position/order state file.
    daily_risk_state_path: str  # Persistent daily P&L and symbol state file.


def _as_bool(value: str | None, default: bool = True) -> bool:  # Converts string-like environment values into booleans.
    if value is None or value.strip() == "":  # Handles missing or empty environment variables.
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


LIVE_TRADING_CONFIRMATION = "I_UNDERSTAND_LIVE_ORDERS"


def _validate(settings: Settings) -> Settings:
    if settings.broker not in {"angelone", "dhan"}:
        raise ValueError("BROKER must be either 'angelone' or 'dhan'")
    if settings.capital <= 0:
        raise ValueError("CAPITAL must be greater than zero")
    if not 0 < settings.risk_per_trade_pct <= 5:
        raise ValueError("RISK_PER_TRADE_PCT must be in the range (0, 5]")
    if not 0 < settings.max_daily_loss_pct <= 10:
        raise ValueError("MAX_DAILY_LOSS_PCT must be in the range (0, 10]")
    if not 1 <= settings.intraday_leverage <= 10:
        raise ValueError("INTRADAY_LEVERAGE must be in the range [1, 10]")
    if not 5 <= settings.intraday_target_pct <= 30:
        raise ValueError("INTRADAY_TARGET_PCT must be in the range [5, 30]")
    if not 0 <= settings.protection_cost_buffer_pct <= 2:
        raise ValueError("PROTECTION_COST_BUFFER_PCT must be in the range [0, 2]")
    if not 0 <= settings.paper_slippage_bps <= 100:
        raise ValueError("PAPER_SLIPPAGE_BPS must be in the range [0, 100]")
    if not 0 <= settings.execution_fees_bps_per_side <= 100:
        raise ValueError("EXECUTION_FEES_BPS_PER_SIDE must be in the range [0, 100]")
    if not settings.paper_trade and (
        not settings.live_trading_enabled
        or settings.live_trading_confirmation != LIVE_TRADING_CONFIRMATION
    ):
        raise ValueError(
            "Live trading is locked. Set PAPER_TRADE=false, LIVE_TRADING_ENABLED=true, "
            f"and LIVE_TRADING_CONFIRMATION={LIVE_TRADING_CONFIRMATION} deliberately."
        )
    return settings


def load_settings() -> Settings:  # Builds a Settings object from environment variables.
    _load_env_file()  # Loads repository env variables before reading runtime settings.
    settings = Settings(  # Creates the immutable settings snapshot.
        broker=os.getenv("BROKER", "angelone").strip().lower(),  # "angelone" or "dhan".
        api_key=os.getenv("ANGELONE_API_KEY", ""),  # Reads the API key or defaults to an empty string.
        client_id=os.getenv("ANGELONE_CLIENT_ID", ""),  # Reads the client identifier or defaults to an empty string.
        pin=os.getenv("ANGELONE_PIN", ""),  # Reads the login PIN or defaults to an empty string.
        totp_secret=os.getenv("ANGELONE_TOTP_SECRET", ""),  # Reads the TOTP secret or defaults to an empty string.
        dhan_client_id=os.getenv("DHAN_CLIENT_ID", ""),  # Dhan client ID.
        dhan_access_token=os.getenv("DHAN_ACCESS_TOKEN", ""),  # Dhan access token.
        risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "1.0")),  # Reads the per-trade risk percentage and converts it to a float.
        capital=float(os.getenv("CAPITAL", "100000")),  # Reads the available capital and converts it to a float.
        scan_interval_seconds=float(os.getenv("SCAN_INTERVAL_SECONDS", "2")),  # Reads the delay between repeated scans and converts it to a float.
        monitor_interval_seconds=float(os.getenv("MONITOR_INTERVAL_SECONDS", "10")),  # Delay between price checks while monitoring positions.
        alert_every_check=_as_bool(os.getenv("ALERT_EVERY_CHECK", "true")),  # Reads whether Telegram should receive updates even without trades.
        order_product_type=os.getenv("ORDER_PRODUCT_TYPE", "INTRADAY").strip().upper(),  # Reads the live-order product type used by Angel One.
        order_variety=os.getenv("ORDER_VARIETY", "NORMAL").strip().upper(),  # Reads the live-order variety used by Angel One.
        intraday_leverage=float(os.getenv("INTRADAY_LEVERAGE", "5.0")),  # Reads the intraday margin multiplier.
        max_consecutive_losses=int(os.getenv("MAX_CONSECUTIVE_LOSSES", "2")),  # Reads the max consecutive losses before stopping.
        safe_mode=_as_bool(os.getenv("SAFE_MODE", "true")),  # Reads whether to apply tradability filters.
        fno_only=_as_bool(os.getenv("FNO_ONLY", "false")),  # Reads whether to restrict to F&O stocks.
        paper_trade=_as_bool(os.getenv("PAPER_TRADE", "true"), default=True),  # Safe default: simulated orders.
        live_trading_enabled=_as_bool(os.getenv("LIVE_TRADING_ENABLED", "false"), default=False),
        live_trading_confirmation=os.getenv("LIVE_TRADING_CONFIRMATION", "").strip(),
        max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "2.0")),
        intraday_target_pct=float(os.getenv("INTRADAY_TARGET_PCT", "15.0")),
        staged_protection_enabled=_as_bool(
            os.getenv("STAGED_PROTECTION_ENABLED", "true"), default=True
        ),
        strict_entry_policy_enabled=_as_bool(
            os.getenv("STRICT_ENTRY_POLICY_ENABLED", "false"), default=False
        ),
        protection_cost_buffer_pct=float(
            os.getenv("PROTECTION_COST_BUFFER_PCT", "0.25")
        ),
        paper_slippage_bps=float(os.getenv("PAPER_SLIPPAGE_BPS", "5")),
        execution_fees_bps_per_side=float(
            os.getenv("EXECUTION_FEES_BPS_PER_SIDE", "10")
        ),
        runtime_state_path=os.getenv("RUNTIME_STATE_PATH", "data/runtime_state.json").strip(),
        daily_risk_state_path=os.getenv("DAILY_RISK_STATE_PATH", "data/daily_risk_state.json").strip(),
    )
    return _validate(settings)

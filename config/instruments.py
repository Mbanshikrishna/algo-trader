from __future__ import annotations

from dataclasses import dataclass  # Imports dataclass for compact immutable instrument records.


@dataclass(frozen=True)
class Instrument:  # Defines the broker-specific metadata needed to request data and place trades.
    symbol: str  # Stores the user-facing symbol used throughout the app and logs.
    exchange: str = "NSE"  # Stores the exchange segment used by Angel One.
    tradingsymbol: str | None = None  # Stores the Angel One trading symbol such as SBIN-EQ.
    symboltoken: str | None = None  # Stores the Angel One numeric symbol token.

    def with_broker_fields(self, tradingsymbol: str, symboltoken: str) -> "Instrument":  # Returns a copy enriched with broker identifiers.
        return Instrument(
            symbol=self.symbol,
            exchange=self.exchange,
            tradingsymbol=tradingsymbol,
            symboltoken=str(symboltoken),
        )


DEFAULT_WATCHLIST = [  # Defines the default set of instruments to scan and trade.
    "RELIANCE.NS",
    "HDFCBANK.NS",
    "ICICIBANK.NS",
    "INFY.NS",
    "TCS.NS",
    "SBIN.NS",
    "AXISBANK.NS",
    "ITC.NS",
    "LT.NS",
    "JUBLFOOD.NS",
]


def angel_tradingsymbol_for(symbol: str) -> str:  # Converts the project symbol format into an Angel One tradingsymbol guess.
    if symbol.endswith(".NS"):  # Maps Yahoo/NSE-style symbols into Angel One equity symbols.
        return f"{symbol.removesuffix('.NS')}-EQ"
    return symbol  # Leaves already normalized symbols untouched.


def default_watchlist() -> list[Instrument]:  # Builds the default watchlist as Instrument records.
    return [Instrument(symbol=symbol, tradingsymbol=angel_tradingsymbol_for(symbol)) for symbol in DEFAULT_WATCHLIST]

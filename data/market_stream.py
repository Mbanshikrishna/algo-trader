from __future__ import annotations  # Lets Python postpone evaluation of type annotations.

from datetime import datetime, timedelta  # Imports datetime helpers for historical candle windows.
from zoneinfo import ZoneInfo  # Imports ZoneInfo for consistent Indian market timestamps.

import pandas as pd  # Imports pandas for DataFrame handling.
import yfinance as yf  # Imports yfinance for fallback market data downloads.

from config.instruments import Instrument  # Imports the broker instrument record used by the Angel One path.


class MarketStream:  # Defines an adapter for fetching intraday OHLCV market data.
    """Market data adapter that can use Angel One or yfinance."""

    _INTERVAL_TO_ANGEL = {  # Maps the local interval strings to SmartAPI candle interval names.
        "1m": "ONE_MINUTE",
        "3m": "THREE_MINUTE",
        "5m": "FIVE_MINUTE",
        "10m": "TEN_MINUTE",
        "15m": "FIFTEEN_MINUTE",
        "30m": "THIRTY_MINUTE",
        "1h": "ONE_HOUR",
        "1d": "ONE_DAY",
    }

    _PERIOD_TO_DELTA = {  # Maps period strings to the amount of history the broker request should cover.
        "1d": timedelta(days=1),
        "2d": timedelta(days=2),
        "5d": timedelta(days=5),
        "1mo": timedelta(days=30),
    }

    def __init__(
        self,
        interval: str = "5m",
        period: str = "1d",
        data_provider: str = "yfinance",
        angel_client: object | None = None,
    ) -> None:  # Initializes the market stream configuration.
        self.interval = interval  # Stores the candle interval to request from the data provider.
        self.period = period  # Stores the overall lookback period to request from the data provider.
        self.data_provider = data_provider.strip().lower()  # Stores the configured market data provider.
        self.angel_client = angel_client  # Stores the optional Angel One client used for broker-native market data.
        self._instrument_cache: dict[str, Instrument] = {}  # Caches resolved Angel One instrument metadata by app symbol.

    @staticmethod
    def _extract_series(df: pd.DataFrame, column: str) -> pd.Series:  # Converts a possibly nested OHLCV column into a plain Series.
        column_data = df[column]  # Selects the requested column from the market data frame.
        if isinstance(column_data, pd.DataFrame):  # Handles MultiIndex downloads where one price field still contains a nested frame.
            column_data = column_data.iloc[:, 0]  # Uses the first ticker column because fetch_ohlcv downloads one symbol at a time.
        return pd.to_numeric(column_data, errors="coerce")  # Coerces the result into a numeric Series for indicator calculations.

    def _normalize_ohlcv(self, df: pd.DataFrame) -> pd.DataFrame:  # Normalizes downloaded market data into plain 1D OHLCV columns.
        normalized = pd.DataFrame(index=df.index)  # Starts a fresh frame so downstream code never sees nested columns.
        for col in ["Open", "High", "Low", "Close", "Volume"]:  # Loops through the standard OHLCV columns.
            normalized[col] = self._extract_series(df, col)  # Flattens each column into a simple Series.
        return normalized  # Returns only the normalized OHLCV data required by the strategy.

    def resolve_instrument(self, instrument_or_symbol: Instrument | str) -> Instrument:  # Resolves missing Angel One metadata and caches it for later order placement.
        if isinstance(instrument_or_symbol, Instrument):
            instrument = instrument_or_symbol
        else:
            instrument = Instrument(symbol=instrument_or_symbol)

        cached = self._instrument_cache.get(instrument.symbol)  # Reuses previously resolved broker metadata for repeated scans of the same symbol.
        if cached is not None:
            return cached
        if instrument.symboltoken and instrument.tradingsymbol:  # Returns already-resolved instruments immediately.
            self._instrument_cache[instrument.symbol] = instrument
            return instrument
        if self.angel_client is None:  # Fails clearly when broker resolution is requested without a broker client.
            raise ValueError("Angel One instrument resolution requires an Angel One client")

        resolved = self.angel_client.resolve_instrument(instrument.symbol, exchange=instrument.exchange)  # Resolves the broker tradingsymbol and token via SmartAPI.
        self._instrument_cache[instrument.symbol] = resolved
        return resolved

    def fetch_ohlcv(self, instrument_or_symbol: Instrument | str) -> pd.DataFrame:  # Downloads OHLCV data for one symbol or instrument.
        if self.data_provider == "angelone":  # Uses SmartAPI candles when broker-native market data is enabled.
            instrument = self.resolve_instrument(instrument_or_symbol)
            return self._fetch_angelone_ohlcv(instrument)

        symbol = instrument_or_symbol.symbol if isinstance(instrument_or_symbol, Instrument) else instrument_or_symbol  # Extracts the yfinance symbol when the caller passed an Instrument.
        df = yf.download(symbol, interval=self.interval, period=self.period, progress=False)  # Requests historical candles from yfinance.
        if df.empty:  # Checks whether the download returned any rows.
            return df  # Returns the empty DataFrame immediately when no data is available.
        return self._normalize_ohlcv(df)  # Returns the normalized market data DataFrame.

    def _fetch_angelone_ohlcv(self, instrument: Instrument) -> pd.DataFrame:  # Downloads OHLCV data for one resolved Angel One instrument.
        if self.angel_client is None:  # Guards against an invalid stream configuration.
            raise ValueError("Angel One market data requires an Angel One client")

        interval = self._INTERVAL_TO_ANGEL.get(self.interval)
        if interval is None:  # Fails fast when the interval is not mapped to a SmartAPI value.
            raise ValueError(f"Unsupported Angel One interval: {self.interval}")

        now = datetime.now(ZoneInfo("Asia/Kolkata"))  # Uses Indian market time for SmartAPI candle requests.
        start = now - self._PERIOD_TO_DELTA.get(self.period, timedelta(days=1))  # Computes the requested lookback window from the configured period.
        rows = self.angel_client.get_candle_data(  # Requests candles from SmartAPI.
            exchange=instrument.exchange,
            symboltoken=instrument.symboltoken or "",
            interval=interval,
            from_datetime=start,
            to_datetime=now,
        )
        return self._candle_rows_to_frame(rows)

    @staticmethod
    def _candle_rows_to_frame(rows: list[list[object]]) -> pd.DataFrame:  # Converts SmartAPI candle arrays into the OHLCV DataFrame expected by the strategy.
        if not rows:  # Returns an empty frame when SmartAPI has no data for the request.
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        frame = pd.DataFrame(rows, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])  # Assigns standard SmartAPI candle columns.
        frame["Timestamp"] = pd.to_datetime(frame["Timestamp"], errors="coerce")  # Parses candle timestamps into pandas datetimes.
        frame = frame.dropna(subset=["Timestamp"]).set_index("Timestamp")  # Drops malformed rows and uses candle timestamps as the frame index.
        for column in ["Open", "High", "Low", "Close", "Volume"]:  # Coerces all OHLCV values to numeric types for indicator calculations.
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame.dropna(subset=["Open", "High", "Low", "Close", "Volume"])  # Removes incomplete candles before returning the normalized frame.

    def fetch_closing_prices(self, symbols: list[str], period: str = "2d") -> pd.DataFrame:
        """Downloads the latest closing prices for symbol list and returns percentage changes."""
        if not symbols:
            return pd.DataFrame()

        df = yf.download(
            symbols,
            interval="1d",
            period=period,
            progress=False,
            threads=True,
        )

        if df.empty:
            return pd.DataFrame()

        close = df["Close"] if "Close" in df else df
        if isinstance(close, pd.Series):
            close = close.to_frame(name=symbols[0])

        close = close.dropna(axis=1, how="all")
        return close

    def top_gainers(self, symbols: list[str], limit: int = 10, period: str = "2d") -> list[dict]:
        """Returns top gainers by percentage change from previous close."""
        close = self.fetch_closing_prices(symbols, period=period)
        if close.empty or close.shape[0] < 2:
            return []

        latest = close.iloc[-1]
        previous = close.iloc[-2]

        changes = ((latest - previous) / previous) * 100
        changes = changes.dropna()

        gainers = (
            changes.sort_values(ascending=False)
            .head(limit)
            .reset_index()
            .rename(columns={"index": "symbol", 0: "pct_change"})
        )

        result = []
        for _, row in gainers.iterrows():
            symbol = row["symbol"]
            if symbol not in latest or symbol not in previous:
                continue
            result.append(
                {
                    "symbol": symbol,
                    "prev_close": float(previous[symbol]),
                    "last_close": float(latest[symbol]),
                    "pct_change": float(row[0]),
                }
            )
        return result

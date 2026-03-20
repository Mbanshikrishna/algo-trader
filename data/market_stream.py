from __future__ import annotations  # Lets Python postpone evaluation of type annotations.

import pandas as pd  # Imports pandas for DataFrame handling.
import yfinance as yf  # Imports yfinance for market data downloads.


class MarketStream:  # Defines a simple adapter for fetching intraday OHLCV market data.
    """Simple market data adapter for intraday OHLCV data."""

    def __init__(self, interval: str = "5m", period: str = "1d") -> None:  # Initializes the market stream configuration.
        self.interval = interval  # Stores the candle interval to request from the data provider.
        self.period = period  # Stores the overall lookback period to request from the data provider.

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

    def fetch_ohlcv(self, symbol: str) -> pd.DataFrame:  # Downloads OHLCV data for one symbol.
        df = yf.download(symbol, interval=self.interval, period=self.period, progress=False)  # Requests historical candles from yfinance.
        if df.empty:  # Checks whether the download returned any rows.
            return df  # Returns the empty DataFrame immediately when no data is available.
        return self._normalize_ohlcv(df)  # Returns the normalized market data DataFrame.

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


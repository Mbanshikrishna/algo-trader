from __future__ import annotations  # Lets Python postpone evaluation of type annotations.

import pandas as pd  # Imports pandas for DataFrame handling.
import yfinance as yf  # Imports yfinance for market data downloads.


class MarketStream:  # Defines a simple adapter for fetching intraday OHLCV market data.
    """Simple market data adapter for intraday OHLCV data."""

    def __init__(self, interval: str = "5m", period: str = "1d") -> None:  # Initializes the market stream configuration.
        self.interval = interval  # Stores the candle interval to request from the data provider.
        self.period = period  # Stores the overall lookback period to request from the data provider.

    def fetch_ohlcv(self, symbol: str) -> pd.DataFrame:  # Downloads OHLCV data for one symbol.
        df = yf.download(symbol, interval=self.interval, period=self.period, progress=False)  # Requests historical candles from yfinance.
        if df.empty:  # Checks whether the download returned any rows.
            return df  # Returns the empty DataFrame immediately when no data is available.

        # Normalize into plain series columns.
        for col in ["Open", "High", "Low", "Close", "Volume"]:  # Loops through the standard OHLCV columns.
            df[col] = pd.Series(df[col].values.flatten(), index=df.index)  # Flattens any nested column values into regular Series columns.
        return df  # Returns the normalized market data DataFrame.

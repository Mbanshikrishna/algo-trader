from __future__ import annotations

import pandas as pd
import yfinance as yf


class MarketStream:
    """Simple market data adapter for intraday OHLCV data."""

    def __init__(self, interval: str = "5m", period: str = "1d") -> None:
        self.interval = interval
        self.period = period

    def fetch_ohlcv(self, symbol: str) -> pd.DataFrame:
        df = yf.download(symbol, interval=self.interval, period=self.period, progress=False)
        if df.empty:
            return df

        # Normalize into plain series columns.
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.Series(df[col].values.flatten(), index=df.index)
        return df

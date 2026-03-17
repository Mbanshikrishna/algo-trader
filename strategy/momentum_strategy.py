from __future__ import annotations

import pandas as pd
from ta.trend import EMAIndicator


class MomentumStrategy:
    """EMA + VWAP based long-bias intraday strategy."""

    def __init__(self, ema_fast: int = 20, ema_slow: int = 50, volume_window: int = 10) -> None:
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.volume_window = volume_window

    @staticmethod
    def _calculate_vwap(df: pd.DataFrame) -> pd.Series:
        typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
        return (typical_price * df["Volume"]).cumsum() / df["Volume"].cumsum()

    def apply_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["EMA_FAST"] = EMAIndicator(out["Close"], window=self.ema_fast).ema_indicator()
        out["EMA_SLOW"] = EMAIndicator(out["Close"], window=self.ema_slow).ema_indicator()
        out["VWAP"] = self._calculate_vwap(out)
        out["AVG_VOLUME"] = out["Volume"].rolling(self.volume_window).mean()
        out["DAY_HIGH"] = out["High"].cummax()
        return out

    def build_signal(self, symbol: str, df: pd.DataFrame) -> dict | None:
        if df.empty or len(df) < self.ema_slow:
            return None

        data = self.apply_indicators(df)
        latest = data.iloc[-1]

        if (
            latest["Close"] > latest["VWAP"]
            and latest["Close"] > latest["EMA_FAST"]
            and latest["Close"] > latest["EMA_SLOW"]
            and latest["Volume"] > latest["AVG_VOLUME"]
            and latest["Close"] >= 0.995 * latest["DAY_HIGH"]
        ):
            return {
                "symbol": symbol,
                "side": "BUY",
                "price": float(round(latest["Close"], 2)),
                "stop_loss": float(round(latest["EMA_FAST"], 2)),
            }

        return None

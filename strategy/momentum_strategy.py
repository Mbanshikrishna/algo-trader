from __future__ import annotations  # Lets Python postpone evaluation of type hints.

import pandas as pd  # Imports pandas for DataFrame and Series operations.
from ta.trend import EMAIndicator  # Imports the EMA indicator helper from the ta library.


class MomentumStrategy:  # Defines an intraday long-bias strategy using trend and volume filters.
    """EMA + VWAP based long-bias intraday strategy."""

    def __init__(self, ema_fast: int = 20, ema_slow: int = 50, volume_window: int = 10) -> None:  # Initializes indicator parameters for the strategy.
        self.ema_fast = ema_fast  # Stores the fast EMA window size.
        self.ema_slow = ema_slow  # Stores the slow EMA window size.
        self.volume_window = volume_window  # Stores the rolling window used for average volume.

    @staticmethod
    def _calculate_vwap(df: pd.DataFrame) -> pd.Series:  # Calculates VWAP from OHLCV data, resetting at each day boundary.
        typical_price = (df["High"] + df["Low"] + df["Close"]) / 3  # Computes the typical price for each candle.
        tp_volume = typical_price * df["Volume"]  # Computes the volume-weighted typical price per candle.

        if hasattr(df.index, "date"):  # Resets VWAP at day boundaries when the index carries date information.
            day_groups = df.index.date  # Extracts the date component for grouping.
            cum_tp_vol = tp_volume.groupby(day_groups).cumsum()  # Accumulates volume-weighted price within each day.
            cum_vol = df["Volume"].groupby(day_groups).cumsum()  # Accumulates volume within each day.
        else:  # Falls back to a single cumulative VWAP when no date info is available.
            cum_tp_vol = tp_volume.cumsum()
            cum_vol = df["Volume"].cumsum()

        return cum_tp_vol / cum_vol  # Returns the per-day cumulative VWAP.

    def apply_indicators(self, df: pd.DataFrame) -> pd.DataFrame:  # Adds strategy indicators to a copy of the input DataFrame.
        out = df.copy()  # Copies the data so the original frame is not modified in place.
        out["EMA_FAST"] = EMAIndicator(out["Close"], window=self.ema_fast).ema_indicator()  # Calculates the fast EMA.
        out["EMA_SLOW"] = EMAIndicator(out["Close"], window=self.ema_slow).ema_indicator()  # Calculates the slow EMA.
        out["VWAP"] = self._calculate_vwap(out)  # Calculates VWAP for the session.
        out["AVG_VOLUME"] = out["Volume"].rolling(self.volume_window).mean()  # Calculates rolling average volume.
        out["DAY_HIGH"] = out["High"].cummax()  # Tracks the running intraday high.
        return out  # Returns the indicator-enriched DataFrame.

    def build_signal(self, symbol: str, df: pd.DataFrame) -> dict | None:  # Evaluates whether the latest candle produces a buy signal.
        if df.empty or len(df) < self.ema_slow:  # Ensures there is enough data to compute indicators reliably.
            return None  # Returns no signal when data is missing or too short.

        data = self.apply_indicators(df)  # Adds all required indicators to the market data.
        latest = data.iloc[-1]  # Selects the most recent candle and its indicators.

        if (  # Checks whether all momentum and confirmation conditions are satisfied.
            latest["Close"] > latest["VWAP"]  # Requires price to be above VWAP.
            and latest["Close"] > latest["EMA_FAST"]  # Requires price to be above the fast EMA.
            and latest["Close"] > latest["EMA_SLOW"]  # Requires price to be above the slow EMA.
            and latest["Volume"] > latest["AVG_VOLUME"]  # Requires current volume to exceed average volume.
            and latest["Close"] >= 0.995 * latest["DAY_HIGH"]  # Requires price to be very close to the day's high.
        ):
            return {  # Returns a BUY signal payload when all conditions pass.
                "symbol": symbol,  # Includes the symbol that triggered the setup.
                "side": "BUY",  # Marks the signal direction as a buy.
                "price": float(round(latest["Close"], 2)),  # Uses the latest close as the rounded entry price.
                "stop_loss": float(round(latest["EMA_FAST"], 2)),  # Uses the fast EMA as the rounded stop-loss reference.
            }  # Finishes the signal dictionary.

        return None  # Returns no signal when the latest candle does not satisfy the rules.

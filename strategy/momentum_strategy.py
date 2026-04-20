from __future__ import annotations  # Lets Python postpone evaluation of type hints.

import numpy as np  # Imports numpy for fast EMA and array operations.
import pandas as pd  # Imports pandas for DataFrame and Series operations.


def _ema_numpy(values: np.ndarray, window: int) -> np.ndarray:  # Computes EMA using numpy — ~10x faster than the ta library.
    alpha = 2.0 / (window + 1)
    out = np.empty_like(values, dtype=np.float64)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


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
        close_arr = out["Close"].to_numpy(dtype=np.float64)  # Extracts close prices as a numpy array for fast EMA.
        out["EMA_FAST"] = _ema_numpy(close_arr, self.ema_fast)  # Calculates the fast EMA using numpy.
        out["EMA_SLOW"] = _ema_numpy(close_arr, self.ema_slow)  # Calculates the slow EMA using numpy.
        out["VWAP"] = self._calculate_vwap(out)  # Calculates VWAP for the session.
        out["AVG_VOLUME"] = out["Volume"].rolling(self.volume_window).mean()  # Calculates rolling average volume.
        out["DAY_HIGH"] = out["High"].cummax()  # Tracks the running intraday high.
        return out  # Returns the indicator-enriched DataFrame.

    def build_signal(self, symbol: str, df: pd.DataFrame) -> dict | None:  # Evaluates whether the latest candle produces a buy signal.
        if df.empty or len(df) < self.ema_slow:  # Ensures there is enough data to compute indicators reliably.
            return None  # Returns no signal when data is missing or too short.

        # Fast path: check only the latest candle values without full DataFrame copy.
        close_arr = df["Close"].to_numpy(dtype=np.float64)
        ema_fast_arr = _ema_numpy(close_arr, self.ema_fast)
        ema_slow_arr = _ema_numpy(close_arr, self.ema_slow)

        latest_close = close_arr[-1]
        latest_ema_fast = ema_fast_arr[-1]
        latest_ema_slow = ema_slow_arr[-1]
        latest_volume = float(df["Volume"].iloc[-1])
        avg_volume = float(df["Volume"].iloc[-self.volume_window:].mean())
        day_high = float(df["High"].cummax().iloc[-1])

        # Compute VWAP for the latest candle.
        tp = (df["High"] + df["Low"] + df["Close"]) / 3
        tp_vol = tp * df["Volume"]
        if hasattr(df.index, "date"):
            last_date = df.index[-1].date() if hasattr(df.index[-1], "date") else None
            if last_date is not None:
                day_mask = df.index.date == last_date
                vwap = float(tp_vol[day_mask].sum() / df["Volume"][day_mask].sum())
            else:
                vwap = float(tp_vol.sum() / df["Volume"].sum())
        else:
            vwap = float(tp_vol.sum() / df["Volume"].sum())

        if (  # Checks whether all momentum and confirmation conditions are satisfied.
            latest_close > vwap
            and latest_close > latest_ema_fast
            and latest_close > latest_ema_slow
            and latest_volume > avg_volume
            and latest_close >= 0.995 * day_high
        ):
            return {
                "symbol": symbol,
                "side": "BUY",
                "price": float(round(latest_close, 2)),
                "stop_loss": float(round(latest_ema_fast, 2)),
            }

        return None  # Returns no signal when the latest candle does not satisfy the rules.

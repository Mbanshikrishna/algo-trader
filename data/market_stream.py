from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from config.instruments import Instrument


class MarketStream:
    """Market data adapter using Angel One SmartAPI."""

    _INTERVAL_TO_ANGEL = {
        "1m": "ONE_MINUTE",
        "3m": "THREE_MINUTE",
        "5m": "FIVE_MINUTE",
        "10m": "TEN_MINUTE",
        "15m": "FIFTEEN_MINUTE",
        "30m": "THIRTY_MINUTE",
        "1h": "ONE_HOUR",
        "1d": "ONE_DAY",
    }

    _PERIOD_TO_DELTA = {
        "1d": timedelta(days=1),
        "2d": timedelta(days=2),
        "5d": timedelta(days=5),
        "1mo": timedelta(days=30),
    }

    def __init__(
        self,
        angel_client: object,
        interval: str = "5m",
        period: str = "1d",
    ) -> None:
        self.angel_client = angel_client
        self.interval = interval
        self.period = period
        self._instrument_cache: dict[str, Instrument] = {}

    def resolve_instrument(self, instrument_or_symbol: Instrument | str) -> Instrument:
        if isinstance(instrument_or_symbol, Instrument):
            instrument = instrument_or_symbol
        else:
            instrument = Instrument(symbol=instrument_or_symbol)

        cached = self._instrument_cache.get(instrument.symbol)
        if cached is not None:
            return cached
        if instrument.symboltoken and instrument.tradingsymbol:
            self._instrument_cache[instrument.symbol] = instrument
            return instrument

        resolved = self.angel_client.resolve_instrument(instrument.symbol, exchange=instrument.exchange)
        self._instrument_cache[instrument.symbol] = resolved
        return resolved

    def fetch_ohlcv(self, instrument_or_symbol: Instrument | str) -> pd.DataFrame:
        instrument = self.resolve_instrument(instrument_or_symbol)
        return self._fetch_angelone_ohlcv(instrument)

    def _fetch_angelone_ohlcv(self, instrument: Instrument) -> pd.DataFrame:
        interval = self._INTERVAL_TO_ANGEL.get(self.interval)
        if interval is None:
            raise ValueError(f"Unsupported interval: {self.interval}")

        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        start = now - self._PERIOD_TO_DELTA.get(self.period, timedelta(days=1))
        rows = self.angel_client.get_candle_data(
            exchange=instrument.exchange,
            symboltoken=instrument.symboltoken or "",
            interval=interval,
            from_datetime=start,
            to_datetime=now,
        )
        return self._candle_rows_to_frame(rows)

    @staticmethod
    def _candle_rows_to_frame(rows: list[list[object]]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        frame = pd.DataFrame(rows, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
        frame["Timestamp"] = pd.to_datetime(frame["Timestamp"], errors="coerce")
        frame = frame.dropna(subset=["Timestamp"]).set_index("Timestamp")
        for column in ["Open", "High", "Low", "Close", "Volume"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame.dropna(subset=["Open", "High", "Low", "Close", "Volume"])

    def fetch_ohlcv_batch(self, instruments: list[Instrument]) -> dict[str, pd.DataFrame]:
        """Fetches OHLCV data for all instruments concurrently using a thread pool."""
        if not instruments:
            return {}

        result: dict[str, pd.DataFrame] = {}

        def _fetch_one(inst: Instrument) -> tuple[str, pd.DataFrame]:
            resolved = self.resolve_instrument(inst)
            return inst.symbol, self._fetch_angelone_ohlcv(resolved)

        with ThreadPoolExecutor(max_workers=min(len(instruments), 5)) as pool:
            futures = {pool.submit(_fetch_one, inst): inst for inst in instruments}
            for future in as_completed(futures):
                inst = futures[future]
                try:
                    symbol, df = future.result()
                    result[symbol] = df
                except Exception:
                    result[inst.symbol] = pd.DataFrame()
        return result

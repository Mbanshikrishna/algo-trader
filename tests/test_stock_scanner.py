from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

import Stock
from config.instruments import Instrument


class ScannerTests(unittest.TestCase):
    def test_run_scanner_skips_symbols_that_fail_resolution(self) -> None:
        instruments = [Instrument(symbol="SBIN.NS"), Instrument(symbol="BAD.NS")]

        class StubStream:
            def resolve_instrument(self, instrument: Instrument) -> Instrument:
                if instrument.symbol == "BAD.NS":
                    raise ValueError("Could not resolve Angel One instrument for BAD.NS")
                return instrument.with_broker_fields("SBIN-EQ", "3045")

            def fetch_ohlcv(self, instrument: Instrument) -> pd.DataFrame:
                return pd.DataFrame(
                    {
                        "Open": [100.0] * 60,
                        "High": [101.0] * 60,
                        "Low": [99.0] * 60,
                        "Close": [100.0 + i * 0.2 for i in range(60)],
                        "Volume": [1000.0 + i * 10 for i in range(60)],
                    }
                )

        class StubStrategy:
            def build_signal(self, symbol: str, df: pd.DataFrame) -> dict | None:
                if symbol == "SBIN.NS":
                    return {"price": 111.25, "stop_loss": 109.5}
                return None

        with patch("Stock.watchlist_from_xlsx", return_value=instruments), patch(
            "Stock.MarketStream",
            return_value=StubStream(),
        ), patch("Stock.MomentumStrategy", return_value=StubStrategy()), patch(
            "Stock.load_settings",
            return_value=type("Settings", (), {"api_key": "", "client_id": "", "access_token": "", "market_data_provider": "angelone"})(),
        ):
            matches, failures = Stock.run_scanner(excel_path="holdings.xlsx")

        self.assertEqual(matches, [{"Stock": "SBIN.NS", "Price": 111.25, "StopLoss": 109.5}])
        self.assertEqual(failures, [("BAD.NS", "Could not resolve Angel One instrument for BAD.NS")])


if __name__ == "__main__":
    unittest.main()

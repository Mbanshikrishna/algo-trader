from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

import send_stock_updates


class DailySummaryTests(unittest.TestCase):
    def test_resolve_requested_symbols_normalizes_and_deduplicates(self) -> None:
        resolved = send_stock_updates.resolve_requested_symbols(["sbin", "INFY.NS", "SBIN", "tcs-eq"])
        self.assertEqual(resolved, ["SBIN.NS", "INFY.NS", "TCS.NS"])

    def test_resolve_requested_symbols_can_load_from_excel_path(self) -> None:
        with patch("send_stock_updates.symbols_from_xlsx", return_value=["SBIN.NS", "INFY.NS"]):
            resolved = send_stock_updates.resolve_requested_symbols([], excel_path="holdings.xlsx")
        self.assertEqual(resolved, ["SBIN.NS", "INFY.NS"])

    def test_collect_daily_snapshots_builds_summary_and_failures(self) -> None:
        class StubStream:
            data_provider = "angelone"

            def fetch_ohlcv(self, symbol: str) -> pd.DataFrame:
                if symbol == "BAD.NS":
                    return pd.DataFrame()
                return pd.DataFrame(
                    {
                        "Open": [100.0, 102.0],
                        "High": [101.0, 106.0],
                        "Low": [99.0, 101.0],
                        "Close": [100.0, 105.0],
                        "Volume": [1000, 2500],
                    },
                    index=pd.to_datetime(["2026-04-14", "2026-04-15"]),
                )

        snapshots, failures = send_stock_updates.collect_daily_snapshots(
            stream=StubStream(),
            symbols=["SBIN.NS", "BAD.NS"],
        )

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["symbol"], "SBIN.NS")
        self.assertAlmostEqual(float(snapshots[0]["change_pct"]), 5.0)
        self.assertEqual(failures, [("BAD.NS", "No market data returned")])

    def test_collect_daily_snapshots_uses_batched_yfinance_rows(self) -> None:
        class StubStream:
            data_provider = "yfinance"

            def fetch_daily_rows(self, symbols: list[str]) -> pd.DataFrame:
                columns = pd.MultiIndex.from_product(
                    [["Open", "High", "Low", "Close", "Volume"], ["SBIN.NS", "BAD.NS"]]
                )
                return pd.DataFrame(
                    [
                        [100.0, None, 101.0, None, 99.0, None, 100.0, None, 1000.0, None],
                        [102.0, None, 106.0, None, 101.0, None, 105.0, None, 2500.0, None],
                    ],
                    columns=columns,
                    index=pd.to_datetime(["2026-04-14", "2026-04-15"]),
                )

        snapshots, failures = send_stock_updates.collect_daily_snapshots(
            stream=StubStream(),
            symbols=["SBIN.NS", "BAD.NS"],
        )

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["symbol"], "SBIN.NS")
        self.assertEqual(failures, [("BAD.NS", "No market data returned")])

    def test_build_telegram_message_formats_snapshot_details(self) -> None:
        message = send_stock_updates.build_telegram_message(
            snapshots=[
                {
                    "symbol": "SBIN.NS",
                    "as_of": "2026-04-15",
                    "open": 810.0,
                    "high": 825.5,
                    "low": 808.0,
                    "close": 820.25,
                    "change_pct": 1.75,
                    "volume": 1234567,
                }
            ],
            failures=[("BAD.NS", "No market data returned")],
        )

        self.assertIn("Stock market update for 2026-04-15", message)
        self.assertIn("SBIN.NS: Close 820.25 (+1.75%)", message)
        self.assertIn("V 1,234,567", message)
        self.assertIn("Skipped symbols:", message)

    def test_send_market_update_returns_delivery_status_and_message(self) -> None:
        with patch(
            "send_stock_updates._build_market_stream",
            return_value=object(),
        ), patch(
            "send_stock_updates.collect_daily_snapshots",
            return_value=(
                [
                    {
                        "symbol": "INFY.NS",
                        "as_of": "2026-04-15",
                        "open": 1500.0,
                        "high": 1510.0,
                        "low": 1490.0,
                        "close": 1505.0,
                        "change_pct": 0.8,
                        "volume": 99999,
                    }
                ],
                [],
            ),
        ), patch("send_stock_updates.send_telegram_message", return_value=True) as send_message:
            delivered, message = send_stock_updates.send_market_update(["infy"])

        self.assertTrue(delivered)
        self.assertIn("INFY.NS: Close 1505.00 (+0.80%)", message)
        send_message.assert_called_once_with(message)


if __name__ == "__main__":
    unittest.main()

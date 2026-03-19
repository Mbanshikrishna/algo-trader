from __future__ import annotations  # Lets Python postpone evaluation of type hints.

import tempfile  # Imports temporary directory utilities for isolated database tests.
import unittest  # Imports Python's built-in unit testing framework.

from data.market_stream import MarketStream  # Imports the market data adapter for normalization tests.
from db.trade_db import TradeDB  # Imports the database helper under test.
from execution.order_manager import OrderManager  # Imports the order manager under test.
from monitor.position_tracker import PositionTracker  # Imports the position tracker under test.
from risk.risk_manager import RiskManager  # Imports the risk manager under test.

try:  # Tries to import optional strategy dependencies.
    import pandas as pd  # Imports pandas for building test DataFrames.
    from strategy.momentum_strategy import MomentumStrategy  # Imports the momentum strategy for signal tests.
except ModuleNotFoundError:  # optional in minimal CI env
    pd = None  # Falls back to None when pandas is not installed.
    MomentumStrategy = None  # Falls back to None when the strategy dependencies are not installed.


class CoreTests(unittest.TestCase):  # Groups core behavioral tests for the trading components.
    def test_risk_position_size(self) -> None:  # Verifies the risk manager returns expected quantities.
        manager = RiskManager(capital=100000, risk_per_trade_pct=1.0)  # Creates a risk manager with 1 percent risk on 100000 capital.
        self.assertEqual(manager.position_size(entry_price=100, stop_loss=98), 500)  # Expects 500 shares when risking 2 currency units per share.
        self.assertEqual(manager.position_size(entry_price=100, stop_loss=100), 0)  # Expects zero shares when there is no risk distance.

    def test_order_manager_paper_order(self) -> None:  # Verifies paper orders are marked as filled immediately.
        order = OrderManager(paper_trade=True).place_market_order("RELIANCE.NS", "BUY", 10)  # Places a simulated market order.
        self.assertEqual(order["status"], "PAPER_FILLED")  # Confirms paper mode returns the simulated fill status.

    def test_position_tracker(self) -> None:  # Verifies buys and sells update tracked positions correctly.
        tracker = PositionTracker()  # Creates a fresh position tracker.
        pos = tracker.update_buy("INFY.NS", 10, 100)  # Adds an initial 10-share position at price 100.
        self.assertEqual(pos.quantity, 10)  # Confirms the initial quantity is stored correctly.

        pos = tracker.update_buy("INFY.NS", 10, 110)  # Adds 10 more shares at a higher price.
        self.assertEqual(pos.quantity, 20)  # Confirms the quantities were combined.
        self.assertAlmostEqual(pos.average_price, 105)  # Confirms the weighted average price was recalculated correctly.

        tracker.update_sell("INFY.NS", 5)  # Sells part of the position.
        self.assertEqual(tracker.snapshot()["INFY.NS"].quantity, 15)  # Confirms the remaining quantity is tracked correctly.

    def test_trade_db_log(self) -> None:  # Verifies a trade can be written into the SQLite database.
        with tempfile.TemporaryDirectory() as tmpdir:  # Creates an isolated temporary directory for the test database.
            db = TradeDB(f"{tmpdir}/trades.db")  # Initializes the trade database in the temporary directory.
            db.log_trade("TCS.NS", "BUY", 1, 100.0, "PAPER_FILLED")  # Writes one sample trade to confirm inserts succeed.

    @unittest.skipIf(pd is None, "pandas not installed in current environment")  # Skips the normalization test when pandas is unavailable.
    def test_market_stream_normalizes_multiindex_columns(self) -> None:  # Verifies yfinance-style nested OHLCV columns are flattened correctly.
        columns = pd.MultiIndex.from_product(  # Builds a two-level column index similar to yfinance output for one ticker.
            [["Open", "High", "Low", "Close", "Volume"], ["RELIANCE.NS"]]
        )
        frame = pd.DataFrame(  # Creates a small synthetic market data set with MultiIndex columns.
            [
                [100.0, 101.0, 99.0, 100.5, 1000],
                [101.0, 102.0, 100.0, 101.5, 1200],
            ],
            columns=columns,
        )

        normalized = MarketStream()._normalize_ohlcv(frame)  # Normalizes the nested OHLCV columns into plain Series columns.

        self.assertEqual(list(normalized.columns), ["Open", "High", "Low", "Close", "Volume"])  # Confirms the normalized frame exposes only flat OHLCV columns.
        self.assertEqual(normalized["Close"].ndim, 1)  # Confirms Close is a Series-compatible 1D column.
        self.assertEqual(float(normalized.iloc[-1]["Close"]), 101.5)  # Confirms the expected close price survives normalization.

    @unittest.skipIf(pd is None or MomentumStrategy is None, "pandas/ta not installed in current environment")  # Skips the strategy test when optional dependencies are unavailable.
    def test_strategy_signal_shape(self) -> None:  # Verifies strategy output, when present, has the expected keys.
        strategy = MomentumStrategy()  # Creates the momentum strategy instance.
        rows = 60  # Defines enough rows to satisfy the slow EMA lookback.
        frame = pd.DataFrame(  # Builds a steadily rising synthetic market data set.
            {
                "Open": [100 + i * 0.1 for i in range(rows)],  # Creates synthetic open prices.
                "High": [101 + i * 0.1 for i in range(rows)],  # Creates synthetic high prices.
                "Low": [99 + i * 0.1 for i in range(rows)],  # Creates synthetic low prices.
                "Close": [100 + i * 0.2 for i in range(rows)],  # Creates synthetic close prices with a stronger upward trend.
                "Volume": [1000 + i * 10 for i in range(rows)],  # Creates synthetic volumes that gradually increase.
            }
        )  # Finishes the synthetic DataFrame.
        signal = strategy.build_signal("SBIN.NS", frame)  # Builds a trading signal from the synthetic data.
        if signal is not None:  # Checks the signal only when the strategy actually returns one.
            self.assertEqual(signal["side"], "BUY")  # Confirms the strategy returns a buy-side signal.
            self.assertIn("stop_loss", signal)  # Confirms the signal includes a stop-loss field.


if __name__ == "__main__":  # Runs the test suite when this file is executed directly.
    unittest.main()  # Starts the unittest test runner.

from __future__ import annotations

import tempfile
import unittest

from db.trade_db import TradeDB
from execution.order_manager import OrderManager
from monitor.position_tracker import PositionTracker
from risk.risk_manager import RiskManager

try:
    import pandas as pd
    from strategy.momentum_strategy import MomentumStrategy
except ModuleNotFoundError:  # optional in minimal CI env
    pd = None
    MomentumStrategy = None


class CoreTests(unittest.TestCase):
    def test_risk_position_size(self) -> None:
        manager = RiskManager(capital=100000, risk_per_trade_pct=1.0)
        self.assertEqual(manager.position_size(entry_price=100, stop_loss=98), 500)
        self.assertEqual(manager.position_size(entry_price=100, stop_loss=100), 0)

    def test_order_manager_paper_order(self) -> None:
        order = OrderManager(paper_trade=True).place_market_order("RELIANCE.NS", "BUY", 10)
        self.assertEqual(order["status"], "PAPER_FILLED")

    def test_position_tracker(self) -> None:
        tracker = PositionTracker()
        pos = tracker.update_buy("INFY.NS", 10, 100)
        self.assertEqual(pos.quantity, 10)

        pos = tracker.update_buy("INFY.NS", 10, 110)
        self.assertEqual(pos.quantity, 20)
        self.assertAlmostEqual(pos.average_price, 105)

        tracker.update_sell("INFY.NS", 5)
        self.assertEqual(tracker.snapshot()["INFY.NS"].quantity, 15)

    def test_trade_db_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TradeDB(f"{tmpdir}/trades.db")
            db.log_trade("TCS.NS", "BUY", 1, 100.0, "PAPER_FILLED")

    @unittest.skipIf(pd is None or MomentumStrategy is None, "pandas/ta not installed in current environment")
    def test_strategy_signal_shape(self) -> None:
        strategy = MomentumStrategy()
        rows = 60
        frame = pd.DataFrame(
            {
                "Open": [100 + i * 0.1 for i in range(rows)],
                "High": [101 + i * 0.1 for i in range(rows)],
                "Low": [99 + i * 0.1 for i in range(rows)],
                "Close": [100 + i * 0.2 for i in range(rows)],
                "Volume": [1000 + i * 10 for i in range(rows)],
            }
        )
        signal = strategy.build_signal("SBIN.NS", frame)
        if signal is not None:
            self.assertEqual(signal["side"], "BUY")
            self.assertIn("stop_loss", signal)


if __name__ == "__main__":
    unittest.main()

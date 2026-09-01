from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from backtest import simulate_trade
from config.settings import load_settings
from execution.order_manager import OrderManager, OrderState
from monitor.position_tracker import Position, PositionTracker
from monitor.risk_state import DailyRiskState, calculate_position_size


class TestOrderConfirmation(unittest.TestCase):
    def test_missing_order_id_is_unknown_not_filled(self):
        manager = OrderManager(MagicMock())
        execution = manager.wait_for_fill({}, "TEST-EQ", 10, timeout=0)
        self.assertEqual(execution.state, OrderState.UNKNOWN)
        self.assertFalse(execution.is_filled)

    def test_timeout_is_unknown_not_filled(self):
        broker = MagicMock()
        manager = OrderManager(broker)
        execution = manager.wait_for_fill(
            {"response": {"data": {"orderid": "42"}}},
            "TEST-EQ", 10, timeout=0,
        )
        self.assertEqual(execution.state, OrderState.UNKNOWN)
        self.assertFalse(execution.is_filled)

    def test_partial_fill_preserves_actual_quantity(self):
        broker = MagicMock()
        broker.get_order_book.return_value = {"data": [{
            "orderid": "42", "orderstatus": "complete",
            "quantity": "10", "filledshares": "4", "averageprice": "101.25",
        }]}
        execution = OrderManager(broker).wait_for_fill(
            {"response": {"data": {"orderid": "42"}}}, "TEST-EQ", 10,
        )
        self.assertEqual(execution.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(execution.filled_quantity, 4)
        self.assertEqual(execution.average_price, 101.25)


class TestPersistentReconciliation(unittest.TestCase):
    def test_staged_stop_is_shadow_only(self):
        position = Position("TEST-EQ", 10, 100, 1, 95, highest_price=103)
        active_stop = position.stop_loss

        shadow_stop = PositionTracker.shadow_staged_stop(position)

        self.assertEqual(shadow_stop, 101.0)
        self.assertEqual(position.stop_loss, active_stop)

    def test_positions_survive_restart_and_reconcile_quantity(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "positions.json"
            tracker = PositionTracker(state_file)
            tracker.update_buy("TEST-EQ", 10, 100.0, 1.0, 98.0, token="123")

            restarted = PositionTracker(state_file)
            self.assertEqual(restarted.snapshot()["TEST-EQ"].quantity, 10)
            changes = restarted.reconcile({"data": [{
                "tradingsymbol": "TEST-EQ", "netqty": "6",
                "avgnetprice": "101.5", "symboltoken": "123",
                "producttype": "INTRADAY",
            }]})
            self.assertTrue(changes)
            self.assertEqual(restarted.snapshot()["TEST-EQ"].quantity, 6)
            self.assertEqual(PositionTracker(state_file).snapshot()["TEST-EQ"].quantity, 6)

    def test_untracked_broker_position_is_adopted(self):
        tracker = PositionTracker()
        tracker.reconcile({"data": [{
            "tradingsymbol": "RECOVER-EQ", "netqty": "3",
            "buyavgprice": "250", "symboltoken": "999",
        }]})
        self.assertEqual(tracker.snapshot()["RECOVER-EQ"].quantity, 3)


class TestRiskControls(unittest.TestCase):
    def test_position_size_uses_stop_risk_and_notional_cap(self):
        self.assertEqual(calculate_position_size(100_000, 100, 98, 1.0, 500_000), 500)
        self.assertEqual(calculate_position_size(100_000, 100, 98, 1.0, 20_000), 200)

    def test_daily_loss_and_symbols_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "risk.json"
            state = DailyRiskState.load(path)
            state.record_entry("TEST-EQ")
            state.record_exit(-2_100)
            restored = DailyRiskState.load(path)
            self.assertIn("TEST-EQ", restored.traded_symbols)
            self.assertTrue(restored.loss_limit_breached(100_000, 2.0))

    @patch.dict(os.environ, {
        "PAPER_TRADE": "false",
        "LIVE_TRADING_ENABLED": "false",
        "LIVE_TRADING_CONFIRMATION": "",
    })
    def test_live_trading_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(ValueError, "Live trading is locked"):
            load_settings()


class TestProtectiveOrderRace(unittest.TestCase):
    @patch("main.time.sleep", return_value=None)
    def test_cancel_must_be_confirmed(self, _sleep):
        from main import _cancel_slm_order

        broker = MagicMock()
        broker.get_order_book.return_value = {
            "data": [{"orderid": "SL1", "orderstatus": "cancelled"}],
        }
        self.assertTrue(_cancel_slm_order(broker, "SL1", "TEST-EQ", MagicMock()))


class TestBacktestExecutionAssumptions(unittest.TestCase):
    def test_same_candle_target_and_stop_uses_adverse_outcome_and_costs(self):
        candles = [
            ["2026-01-01 10:00", 100, 100, 100, 100, 1000],
            ["2026-01-01 10:05", 100, 120, 95, 110, 1000],
        ]
        trade = simulate_trade(
            candles, 0, 100, 95, 1, 10,
            "TEST-EQ", "1", "2026-01-01", "10:00",
        )
        self.assertIn(trade.exit_reason, {"HARD_STOP", "TRAILING_STOP"})
        self.assertGreater(trade.fees, 0)
        self.assertLess(trade.pnl, trade.gross_pnl)


class TestRuntimeScheduling(unittest.TestCase):
    def test_post_close_sleep_skips_weekend(self):
        from main import _seconds_until_next_scan

        friday = datetime(2026, 9, 4, 16, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        seconds = _seconds_until_next_scan(friday)
        monday = friday.timestamp() + seconds

        wake = datetime.fromtimestamp(monday, tz=ZoneInfo("Asia/Kolkata"))
        self.assertEqual(wake.weekday(), 0)
        self.assertEqual((wake.hour, wake.minute), (10, 0))


if __name__ == "__main__":
    unittest.main()

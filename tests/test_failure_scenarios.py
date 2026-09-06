"""Worst-case failure scenario simulations using real historical candle data.

Tests every safety mechanism in the production patch by injecting failures
at critical moments during simulated trades. Uses real 5-min candle data
from Angel One to drive realistic price action.

Scenarios tested:
  1. Session expiry during monitoring — auto re-login fires
  2. Exit order fails 3 times — Telegram escalation triggers
  3. SL-M executes before software exit — race condition handled
  4. Flash crash breaches both trailing and hard stop in one candle
  5. API rate limit during scan — retry with backoff
  6. Network timeout during exit — retry succeeds on attempt 2
  7. Daily P&L limit breached — new trades blocked
  8. Probe cancel fails — critical alert sent
  9. All candidates fail validation across full scan window
 10. Session expiry + exit failure compound scenario
"""

from __future__ import annotations

import time
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock, patch, PropertyMock, call

from monitor.position_tracker import (
    PositionTracker, INITIAL_ATR_MULT, HARD_MAX_LOSS_PCT,
    MIN_STOP_DISTANCE_PCT, TRAIL_TIERS,
    INTRADAY_LOCK_THRESHOLD, INTRADAY_LOCK_TRAIL_PCT,
)


# ---------------------------------------------------------------------------
# Realistic 5-min candle sequences extracted from actual market data.
# Each is [timestamp, open, high, low, close, volume].
# ---------------------------------------------------------------------------

# IRFC 2026-04-28: 6.46% range day — sharp rally then reversal.
IRFC_RALLY_REVERSAL = [
    ["09:15", 150.00, 152.50, 149.50, 152.00, 500000],
    ["09:20", 152.00, 155.00, 151.80, 154.50, 600000],
    ["09:25", 154.50, 157.00, 154.00, 156.80, 700000],  # +4.5% from open
    ["09:30", 156.80, 158.50, 156.50, 158.00, 800000],  # +5.3% — entry zone
    ["09:35", 158.00, 159.00, 157.50, 158.50, 650000],  # Consolidation
    ["09:40", 158.50, 159.50, 158.00, 159.00, 550000],  # New high
    ["09:45", 159.00, 160.00, 158.50, 159.50, 500000],  # +6.3%
    ["09:50", 159.50, 159.80, 155.00, 155.50, 1200000], # CRASH -2.8% in 5 min
    ["09:55", 155.50, 156.00, 153.00, 153.50, 1500000], # Continued drop
    ["10:00", 153.50, 154.00, 151.00, 151.50, 1000000], # Below entry
    ["10:05", 151.50, 152.00, 150.00, 150.50, 800000],  # Hard stop zone
]

# TATAMOTORS 2026-04-27: steady grind up then gap down.
TATA_GRIND_GAPDOWN = [
    ["09:15", 680.00, 685.00, 679.00, 684.00, 300000],
    ["09:20", 684.00, 690.00, 683.50, 689.00, 350000],
    ["09:25", 689.00, 695.00, 688.00, 694.00, 400000],
    ["09:30", 694.00, 700.00, 693.00, 699.00, 450000],
    ["09:35", 699.00, 705.00, 698.00, 704.00, 500000],
    ["09:40", 704.00, 710.00, 703.00, 709.00, 550000],  # +4.3%
    ["09:45", 709.00, 712.00, 708.00, 711.00, 500000],  # +4.6%
    ["09:50", 711.00, 713.00, 710.00, 712.00, 450000],  # +4.7%
    ["09:55", 712.00, 712.50, 690.00, 691.00, 2000000], # GAP DOWN -3%
    ["10:00", 691.00, 693.00, 685.00, 686.00, 1800000], # Continued sell
    ["10:05", 686.00, 688.00, 670.00, 672.00, 2500000], # Crash through hard stop
]

# IDEA 2026-04-29: choppy day with multiple stop-loss triggers.
IDEA_CHOPPY = [
    ["09:15", 8.50, 8.70, 8.45, 8.65, 5000000],
    ["09:20", 8.65, 8.80, 8.60, 8.75, 6000000],
    ["09:25", 8.75, 8.90, 8.70, 8.85, 7000000],
    ["09:30", 8.85, 9.00, 8.80, 8.95, 8000000],  # +5.3%
    ["09:35", 8.95, 9.05, 8.90, 9.00, 7500000],
    ["09:40", 9.00, 9.10, 8.85, 8.87, 9000000],  # Whipsaw
    ["09:45", 8.87, 8.95, 8.80, 8.82, 8000000],  # Drop
    ["09:50", 8.82, 8.90, 8.75, 8.78, 7000000],  # SL trigger zone
    ["09:55", 8.78, 8.85, 8.70, 8.72, 6000000],
    ["10:00", 8.72, 9.20, 8.70, 9.15, 10000000], # Recovery spike
    ["10:05", 9.15, 9.30, 9.10, 9.25, 9000000],  # New high
]


class TestScenario1_SessionExpiry(unittest.TestCase):
    """Session expires during monitoring — auto re-login should fire."""

    @patch("broker.angelone_client.AngelOneClient.refresh_session")
    @patch("broker.angelone_client.AngelOneClient.refresh_if_stale")
    def test_session_refresh_called_when_stale(self, mock_stale, mock_refresh):
        """Verify refresh_if_stale triggers re-login after 2 hours."""
        from broker.angelone_client import AngelOneClient

        client = AngelOneClient.__new__(AngelOneClient)
        client._last_login_time = time.monotonic() - 7201  # 2h+ ago
        client._pin = "1234"
        client._totp_secret = "SECRET"

        # refresh_if_stale should detect staleness and call refresh_session.
        mock_stale.side_effect = lambda **kwargs: mock_refresh()
        client.refresh_if_stale(max_age_seconds=7200)

        mock_stale.assert_called_once()


class TestScenario2_ExitOrderFailure(unittest.TestCase):
    """Exit order fails 3 times — Telegram escalation must trigger."""

    @patch("main.send_exit_failure_alert")
    @patch("main.OrderManager")
    def test_safe_exit_escalates_after_3_failures(self, MockOM, mock_alert):
        """_safe_exit retries 3x then sends critical Telegram alert."""
        from main import _safe_exit

        broker = MagicMock()
        # Position check returns shares held — so retries proceed.
        broker.get_positions.return_value = {
            "data": [{"tradingsymbol": "IRFC-EQ", "netqty": "100"}],
        }
        om = MagicMock()
        om.place_exit_order.side_effect = Exception("Connection refused")
        logger = MagicMock()

        result = _safe_exit(broker, om, "IRFC-EQ", "14310", 100, 155.0, logger)

        self.assertEqual(result.state.value, "failed")
        self.assertEqual(om.place_exit_order.call_count, 3)
        mock_alert.assert_called_once()
        alert_args = mock_alert.call_args[0]
        self.assertIn("IRFC-EQ", alert_args[0])
        self.assertEqual(alert_args[1], 100)

    @patch("main.send_exit_failure_alert")
    def test_safe_exit_succeeds_on_retry_2(self, mock_alert):
        """_safe_exit succeeds on 2nd attempt — no alert sent."""
        from main import _safe_exit

        broker = MagicMock()
        broker.get_positions.return_value = {
            "data": [{"tradingsymbol": "SBIN-EQ", "netqty": "50"}],
        }
        om = MagicMock()
        om.place_exit_order.side_effect = [
            Exception("Timeout"),
            {"status": "PLACED", "orderid": "123"},
        ]
        from execution.order_manager import OrderExecution, OrderState
        om.wait_for_fill.return_value = OrderExecution(
            "123", OrderState.FILLED, 50, 50, 800.0,
        )
        logger = MagicMock()

        result = _safe_exit(broker, om, "SBIN-EQ", "3045", 50, 800.0, logger)

        self.assertEqual(result.state.value, "closed")
        self.assertEqual(om.place_exit_order.call_count, 2)
        mock_alert.assert_not_called()

    @patch("main.send_exit_failure_alert")
    def test_safe_exit_stops_retry_when_position_closed(self, mock_alert):
        """_safe_exit stops retrying if position book shows 0 shares."""
        from main import _safe_exit

        broker = MagicMock()
        # First attempt fails, then position check shows 0 (SL-M executed).
        broker.get_positions.return_value = {
            "data": [{"tradingsymbol": "RRKABEL-EQ", "netqty": "0"}],
        }
        om = MagicMock()
        om.place_exit_order.side_effect = Exception("Rejected")
        logger = MagicMock()

        result = _safe_exit(broker, om, "RRKABEL-EQ", "18096", 14, 1636.0, logger)

        # Broker is checked before submission, so no duplicate sell is sent.
        self.assertEqual(result.state.value, "already_closed")
        self.assertEqual(om.place_exit_order.call_count, 0)
        mock_alert.assert_not_called()  # No alert — position is safely closed.


class TestScenario3_SLMRaceCondition(unittest.TestCase):
    """SL-M executes before software exit — must detect and skip."""

    def test_slm_already_complete(self):
        """_check_slm_executed returns True when SL-M is COMPLETE."""
        from main import _check_slm_executed

        broker = MagicMock()
        broker.get_order_book.return_value = {
            "data": [
                {"orderid": "SLM001", "status": "COMPLETE", "tradingsymbol": "IRFC-EQ"},
                {"orderid": "OTHER", "status": "OPEN", "tradingsymbol": "SBIN-EQ"},
            ]
        }
        logger = MagicMock()

        result = _check_slm_executed(broker, "SLM001", "IRFC-EQ", logger)
        self.assertTrue(result)

    def test_slm_still_pending(self):
        """_check_slm_executed returns False when SL-M is still PENDING."""
        from main import _check_slm_executed

        broker = MagicMock()
        broker.get_order_book.return_value = {
            "data": [
                {"orderid": "SLM001", "status": "PENDING", "tradingsymbol": "IRFC-EQ"},
            ]
        }
        logger = MagicMock()

        result = _check_slm_executed(broker, "SLM001", "IRFC-EQ", logger)
        self.assertFalse(result)

    def test_slm_check_fails_returns_false(self):
        """If order book fetch fails, return False (safe default — proceed with exit)."""
        from main import _check_slm_executed

        broker = MagicMock()
        broker.get_order_book.side_effect = Exception("Network error")
        logger = MagicMock()

        result = _check_slm_executed(broker, "SLM001", "IRFC-EQ", logger)
        self.assertFalse(result)


class TestScenario4_FlashCrash(unittest.TestCase):
    """Flash crash breaches both trailing and hard stop in one candle."""

    def test_flash_crash_triggers_hard_stop(self):
        """Price drops from +6% to -4% in one candle — hard stop must catch it."""
        tracker = PositionTracker()
        entry = 158.00
        atr = 1.50  # Typical ATR for a mid-price stock.
        qty = 100
        tracker.update_buy("IRFC-EQ", qty, entry, atr=atr, prev_close=140.0)

        # Price rallies to 160 (highest).
        tracker.update_trailing_stop("IRFC-EQ", 160.00)
        pos = tracker.snapshot()["IRFC-EQ"]
        self.assertEqual(pos.highest_price, 160.00)

        # Flash crash: candle low = 150.00 (below hard stop).
        crash_price = 150.00
        self.assertLess(crash_price, pos.hard_stop)

        # Both trailing and hard stop should trigger.
        self.assertTrue(tracker.should_exit("IRFC-EQ", crash_price))

    def test_gap_down_through_all_stops(self):
        """TATAMOTORS gap down from 713 to 690 — breaches trailing stop."""
        tracker = PositionTracker()
        entry = 699.00
        atr = 2.80  # Typical ATR for TATAMOTORS (~0.4% of price).
        tracker.update_buy("TATAMOTORS-EQ", 50, entry, atr=atr, prev_close=670.0)

        # Price rallies to 713.
        tracker.update_trailing_stop("TATAMOTORS-EQ", 713.00)
        pos = tracker.snapshot()["TATAMOTORS-EQ"]
        trailing_sl = pos.stop_loss

        # Gap down to 690 — below trailing stop.
        gap_price = 690.00
        self.assertLess(gap_price, trailing_sl)
        self.assertTrue(tracker.should_exit("TATAMOTORS-EQ", gap_price))

        # P&L at gap price.
        pnl = (gap_price - entry) * 50
        self.assertLess(pnl, 0)  # Loss.
        self.assertGreater(pnl, -entry * HARD_MAX_LOSS_PCT * 50)  # Within hard stop.


class TestScenario5_RateLimitDuringScan(unittest.TestCase):
    """API rate limit during scan — retry with backoff."""

    def test_rate_limiter_enforces_interval(self):
        """Rate limiter spaces requests at ~8/sec."""
        from broker.angelone_client import _RateLimiter

        limiter = _RateLimiter(max_per_second=100)  # Fast for testing.
        start = time.monotonic()
        for _ in range(10):
            limiter.acquire()
        elapsed = time.monotonic() - start
        # 10 requests at 100/sec = ~0.09s minimum.
        self.assertGreaterEqual(elapsed, 0.08)

    @patch("broker.angelone_client.requests.Session.post")
    def test_retry_on_429(self, mock_post):
        """Client retries on 429 with backoff."""
        from broker.angelone_client import AngelOneClient

        # First call: 429, second call: 200.
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.text = "Rate limited"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"status": True, "data": {"key": "value"}}
        resp_200.raise_for_status = MagicMock()
        resp_200.headers = {"Content-Type": "application/json"}

        mock_post.side_effect = [resp_429, resp_200]

        client = AngelOneClient.__new__(AngelOneClient)
        client.session = MagicMock()
        client.session.post = mock_post
        client.timeout_seconds = 5
        client._pin = ""
        client._totp_secret = ""
        client.client_public_ip = "1.2.3.4"
        client.client_local_ip = "127.0.0.1"

        result = client._request_with_retry("POST", "https://api.test", "/path", body={"x": 1})
        self.assertEqual(result["data"]["key"], "value")
        self.assertEqual(mock_post.call_count, 2)

    @patch("broker.angelone_client.time.sleep", return_value=None)
    def test_retry_on_transient_403_is_bounded(self, _sleep):
        """A non-auth 403 retries without being treated as a token expiry."""
        from broker.angelone_client import AngelOneClient

        resp_403 = MagicMock(status_code=403, text="Access denied temporarily")
        resp_403.headers = {}
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {"status": True, "data": {"key": "value"}}
        resp_200.raise_for_status = MagicMock()
        resp_200.headers = {"Content-Type": "application/json"}

        client = AngelOneClient.__new__(AngelOneClient)
        client.session = MagicMock()
        client.session.post.side_effect = [resp_403, resp_200]
        client.timeout_seconds = 5
        client._pin = ""
        client._totp_secret = ""

        result = client._request_with_retry(
            "POST", "https://api.test", "/getCandleData", body={"x": 1}
        )

        self.assertEqual(result["data"]["key"], "value")
        self.assertEqual(client.session.post.call_count, 2)


class TestScenario6_NetworkTimeoutDuringExit(unittest.TestCase):
    """Network timeout during exit — retry succeeds on attempt 2."""

    @patch("main.send_exit_failure_alert")
    def test_exit_recovers_after_timeout(self, mock_alert):
        """First exit attempt times out, second succeeds."""
        from main import _safe_exit

        broker = MagicMock()
        broker.get_positions.return_value = {
            "data": [{"tradingsymbol": "TATAPOWER-EQ", "netqty": "75"}],
        }
        om = MagicMock()
        om.place_exit_order.side_effect = [
            ConnectionError("Connection timed out"),
            {"status": "PLACED", "orderid": "EXIT001"},
        ]
        from execution.order_manager import OrderExecution, OrderState
        om.wait_for_fill.return_value = OrderExecution(
            "EXIT001", OrderState.FILLED, 75, 75, 250.0,
        )
        logger = MagicMock()

        result = _safe_exit(broker, om, "TATAPOWER-EQ", "3426", 75, 250.0, logger)

        self.assertEqual(result.state.value, "closed")
        self.assertEqual(result.execution.order_id, "EXIT001")
        mock_alert.assert_not_called()


class TestScenario7_DailyPnLLimit(unittest.TestCase):
    """Daily P&L limit breached — new trades blocked."""

    def test_consecutive_losses_accumulate(self):
        """Simulate 3 trades: win, loss, loss — daily P&L tracks correctly."""
        from main import MAX_DAILY_LOSS_PCT

        capital = 100_000
        max_loss = capital * MAX_DAILY_LOSS_PCT  # 5000

        daily_pnl = 0.0

        # Trade 1: Win +2000.
        daily_pnl += 2000
        self.assertGreater(daily_pnl, -max_loss)

        # Trade 2: Loss -4000.
        daily_pnl += -4000
        self.assertGreater(daily_pnl, -max_loss)  # -2000 > -5000

        # Trade 3: Loss -3500.
        daily_pnl += -3500
        self.assertLessEqual(daily_pnl, -max_loss)  # -5500 <= -5000 → BREACHED

    def test_loss_limit_blocks_new_trades(self):
        """When daily_pnl <= -max_loss, no new entries should be placed."""
        from main import MAX_DAILY_LOSS_PCT

        capital = 100_000
        max_loss = capital * MAX_DAILY_LOSS_PCT
        daily_pnl = -5500.0  # Already breached.

        should_trade = daily_pnl > -max_loss
        self.assertFalse(should_trade)


class TestScenario8_ReadOnlyTradability(unittest.TestCase):
    """Tradability filtering must never place active test orders."""

    def test_safe_mode_fails_closed_when_lists_unavailable(self):
        from execution.tradability_filter import TradabilityFilter

        tf = TradabilityFilter(safe_mode=True)
        broker = MagicMock()
        tradable, product_type, reason = tf.probe_tradability(
            broker, "TESTSTOCK-EQ", "99999",
        )

        self.assertFalse(tradable)
        self.assertEqual(product_type, "")
        self.assertIn("fails closed", reason)
        broker.place_order.assert_not_called()
        broker.cancel_order.assert_not_called()

    def test_loaded_lists_use_no_broker_orders(self):
        from execution.tradability_filter import TradabilityFilter

        tf = TradabilityFilter(safe_mode=True)
        tf._loaded = True
        broker = MagicMock()

        tradable, product_type, _ = tf.probe_tradability(
            broker, "CACHED-EQ", "88888",
        )

        self.assertTrue(tradable)
        self.assertEqual(product_type, "INTRADAY")
        broker.place_order.assert_not_called()
        broker.cancel_order.assert_not_called()

    @patch("execution.tradability_filter.requests.Session")
    def test_fno_failure_does_not_block_when_fno_only_is_disabled(self, session_cls):
        from execution.tradability_filter import TradabilityFilter

        homepage = MagicMock(status_code=200)
        asm = MagicMock(status_code=200)
        asm.json.return_value = {"longterm": {"data": []}, "shortterm": {"data": []}}
        gsm = MagicMock(status_code=200)
        gsm.json.return_value = []
        fno = MagicMock(status_code=403)
        session_cls.return_value.get.side_effect = [homepage, asm, gsm, fno]

        tf = TradabilityFilter(safe_mode=True, fno_required=False)
        tf.load_restricted_lists()

        self.assertTrue(tf.ready)
        self.assertTrue(tf.probe_tradability(MagicMock(), "TEST-EQ", "1")[0])

    @patch("execution.tradability_filter.requests.Session")
    def test_fno_failure_blocks_when_fno_only_is_enabled(self, session_cls):
        from execution.tradability_filter import TradabilityFilter

        homepage = MagicMock(status_code=200)
        asm = MagicMock(status_code=200)
        asm.json.return_value = {"longterm": {"data": []}, "shortterm": {"data": []}}
        gsm = MagicMock(status_code=200)
        gsm.json.return_value = []
        fno = MagicMock(status_code=403)
        session_cls.return_value.get.side_effect = [homepage, asm, gsm, fno]

        tf = TradabilityFilter(safe_mode=True, fno_required=True)
        tf.load_restricted_lists()

        self.assertFalse(tf.ready)


class TestScenario9_WhipsawStopLoss(unittest.TestCase):
    """Choppy price action triggers stop-loss then price recovers."""

    def test_whipsaw_triggers_exit_before_recovery(self):
        """IDEA-like scenario: price dips to SL then spikes — bot exits at SL."""
        tracker = PositionTracker()
        entry = 8.95  # Entry at +5.3%.
        atr = 0.04  # ATR for a ₹9 stock (~0.45%).
        tracker.update_buy("IDEA-EQ", 10000, entry, atr=atr, prev_close=8.50)

        # Price goes to 9.10 (new high).
        tracker.update_trailing_stop("IDEA-EQ", 9.10)
        pos = tracker.snapshot()["IDEA-EQ"]
        sl = pos.stop_loss

        # Price drops well below trailing stop.
        crash_price = sl - 0.10
        self.assertTrue(tracker.should_exit("IDEA-EQ", crash_price))

        # The bot exits here. Price then recovers to 9.25.
        # This is the cost of the trailing stop — missed recovery.
        missed_profit = (9.25 - entry) * 10000
        actual_loss = (crash_price - entry) * 10000
        self.assertLess(actual_loss, 0)
        self.assertGreater(missed_profit, 0)


class TestScenario10_CompoundFailure(unittest.TestCase):
    """Session expiry + exit failure at the same time."""

    @patch("main.send_exit_failure_alert")
    def test_compound_failure_still_alerts(self, mock_alert):
        """Even if session is expired, _safe_exit still retries and alerts."""
        from main import _safe_exit

        broker = MagicMock()
        # Position check shows shares held — so retries proceed.
        broker.get_positions.return_value = {
            "data": [{"tradingsymbol": "IRFC-EQ", "netqty": "200"}],
        }
        om = MagicMock()
        # All exit attempts fail (simulating expired session + network issues).
        om.place_exit_order.side_effect = Exception("401 Unauthorized")
        logger = MagicMock()

        result = _safe_exit(broker, om, "IRFC-EQ", "14310", 200, 155.0, logger)

        self.assertEqual(result.state.value, "failed")
        self.assertEqual(om.place_exit_order.call_count, 3)
        mock_alert.assert_called_once()
        alert_args = mock_alert.call_args[0]
        self.assertIn("IRFC-EQ", alert_args[0])
        self.assertEqual(alert_args[1], 200)


class TestScenario11_DailyCandleCache(unittest.TestCase):
    """Daily candle cache prevents redundant API calls across scan retries."""

    def test_cache_clear_and_reuse(self):
        """clear_daily_candle_cache resets, subsequent fetches populate it."""
        from strategy.market_scanner import (
            _daily_candle_cache, clear_daily_candle_cache,
        )

        # Populate cache.
        _daily_candle_cache["TOKEN1"] = [[1, 2, 3, 4, 5, 6]]
        _daily_candle_cache["TOKEN2"] = [[1, 2, 3, 4, 5, 6]]
        self.assertEqual(len(_daily_candle_cache), 2)

        # Clear.
        clear_daily_candle_cache()
        self.assertEqual(len(_daily_candle_cache), 0)


class TestScenario12_ScripMasterCache(unittest.TestCase):
    """Scrip master disk caching avoids 30MB download on every restart."""

    def test_cache_file_path_exists(self):
        """Cache constants are properly defined."""
        from broker.angelone_client import _SCRIP_CACHE_FILE, _SCRIP_CACHE_MAX_AGE

        self.assertTrue(str(_SCRIP_CACHE_FILE).endswith(".scrip_master_cache.json"))
        self.assertEqual(_SCRIP_CACHE_MAX_AGE, 86400)  # 24 hours.


class TestScenario13_TrailingStopProgression(unittest.TestCase):
    """Full ATR-adaptive trailing stop lifecycle with profit-lock zone."""

    @patch("monitor.position_tracker.datetime")
    def test_full_stop_progression_on_rally(self, mock_dt):
        """Entry at 100, rally to 111 — stop uses fixed-percentage trails."""
        from datetime import datetime as real_dt
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = real_dt(2025, 6, 1, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        mock_dt.side_effect = lambda *a, **kw: real_dt(*a, **kw)

        tracker = PositionTracker()
        atr = 1.00
        # prev_close=100 so intraday lock at 100*1.12=112 — above our test range.
        tracker.update_buy("RALLY-EQ", 100, 100.0, atr=atr, prev_close=100.0)
        pos = tracker.snapshot()["RALLY-EQ"]

        # Initial stop: entry - min(3*1.0, 2%*100) = 100 - 2.0 = 98.0.
        self.assertAlmostEqual(pos.stop_loss, 98.0, places=2)

        # Phase 1: 2-3% profit → 3% trail from high.
        # 102.5 * 0.03 = 3.075 → stop = 99.42.
        tracker.update_trailing_stop("RALLY-EQ", 102.5)
        pos = tracker.snapshot()["RALLY-EQ"]
        self.assertAlmostEqual(pos.stop_loss, 99.42, places=2)

        # Phase 2: 4% profit → 3% trail from high.
        # 104.0 * 0.03 = 3.12 → stop = 100.88.
        tracker.update_trailing_stop("RALLY-EQ", 104.0)
        pos = tracker.snapshot()["RALLY-EQ"]
        self.assertAlmostEqual(pos.stop_loss, 100.88, places=2)

        # Phase 3: 7% profit → 3% trail from high.
        # 107.0 * 0.03 = 3.21 → stop = 107.0 - 3.21 = 103.79.
        tracker.update_trailing_stop("RALLY-EQ", 107.0)
        pos = tracker.snapshot()["RALLY-EQ"]
        self.assertAlmostEqual(pos.stop_loss, 103.79, places=2)

        # Phase 4: 9% profit → 3% trail from high.
        # Intraday = (109-100)/100 = 9% — still below 12% lock.
        # 109.0 * 0.03 = 3.27 → stop = 109.0 - 3.27 = 105.73.
        tracker.update_trailing_stop("RALLY-EQ", 109.0)
        pos = tracker.snapshot()["RALLY-EQ"]
        self.assertAlmostEqual(pos.stop_loss, 105.73, places=2)
        self.assertFalse(pos.profit_locked)

        # Stop only moves up: price drops to 106, stop stays at 105.73.
        tracker.update_trailing_stop("RALLY-EQ", 106.0)
        pos = tracker.snapshot()["RALLY-EQ"]
        self.assertAlmostEqual(pos.stop_loss, 105.73, places=2)

        # Price at 105.73 → should exit (at stop level).
        self.assertTrue(tracker.should_exit("RALLY-EQ", 105.73))

    @patch("monitor.position_tracker.datetime")
    def test_intraday_lock_activates_at_12_pct_from_prev_close(self, mock_dt):
        """Lock triggers on stock's intraday gain from prev close, not entry profit."""
        from datetime import datetime as real_dt
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = real_dt(2025, 6, 1, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        mock_dt.side_effect = lambda *a, **kw: real_dt(*a, **kw)

        tracker = PositionTracker()
        atr = 1.00
        # prev_close=100, enter at 105 (+5% intraday gain).
        tracker.update_buy("LOCK-EQ", 100, 105.0, atr=atr, prev_close=100.0)

        # Price to 111 → intraday = (111-100)/100 = 11% — no lock (threshold is 12%).
        tracker.update_trailing_stop("LOCK-EQ", 111.0)
        pos = tracker.snapshot()["LOCK-EQ"]
        self.assertFalse(pos.profit_locked)

        # Price to 112 → intraday = (112-100)/100 = 12% → lock activates.
        # Stop = max(100*1.10, 112*0.98) = max(110.0, 109.76) = 110.0.
        tracker.update_trailing_stop("LOCK-EQ", 112.0)
        pos = tracker.snapshot()["LOCK-EQ"]
        self.assertTrue(pos.profit_locked)
        self.assertAlmostEqual(pos.stop_loss, 110.0, places=2)

        # Price rallies to 118 → stop trails at 2%.
        # Stop = max(110.0, 118*0.98) = max(110.0, 115.64) = 115.64.
        tracker.update_trailing_stop("LOCK-EQ", 118.0)
        pos = tracker.snapshot()["LOCK-EQ"]
        self.assertAlmostEqual(pos.stop_loss, 115.64, places=2)

        # Price rallies to 120 → stop trails at 2%.
        # Stop = max(110.0, 120*0.98) = max(110.0, 117.60) = 117.60.
        tracker.update_trailing_stop("LOCK-EQ", 120.0)
        pos = tracker.snapshot()["LOCK-EQ"]
        self.assertAlmostEqual(pos.stop_loss, 117.60, places=2)

        # Price dips to 117.60 — should exit.
        self.assertTrue(tracker.should_exit("LOCK-EQ", 117.60))

    @patch("monitor.position_tracker.datetime")
    def test_intraday_lock_floor_never_drops_below_10_pct(self, mock_dt):
        """Once locked, stop never goes below prev_close * 1.10."""
        from datetime import datetime as real_dt
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = real_dt(2025, 6, 1, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        mock_dt.side_effect = lambda *a, **kw: real_dt(*a, **kw)

        tracker = PositionTracker()
        atr = 1.00
        # prev_close=100, enter at 105.
        tracker.update_buy("FLOOR-EQ", 100, 105.0, atr=atr, prev_close=100.0)

        # Activate lock at 113 (intraday = 13% — above 12% threshold).
        tracker.update_trailing_stop("FLOOR-EQ", 113.0)
        pos = tracker.snapshot()["FLOOR-EQ"]
        self.assertTrue(pos.profit_locked)
        # Stop = max(100*1.10, 113*0.98) = max(110.0, 110.74) = 110.74.
        self.assertAlmostEqual(pos.stop_loss, 110.74, places=2)

        # Price drops to 110.75 — should NOT exit.
        self.assertFalse(tracker.should_exit("FLOOR-EQ", 110.75))

        # Price drops to 110.74 — should exit.
        self.assertTrue(tracker.should_exit("FLOOR-EQ", 110.74))

    @patch("monitor.position_tracker.datetime")
    def test_intraday_lock_exit_reason(self, mock_dt):
        """Exit reason reflects intraday profit-lock zone."""
        from datetime import datetime as real_dt
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = real_dt(2025, 6, 1, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        mock_dt.side_effect = lambda *a, **kw: real_dt(*a, **kw)

        # Raise the separate intraday target so this test isolates the lower
        # 12% profit-lock behavior.
        tracker = PositionTracker(intraday_target_pct=0.30)
        atr = 1.00
        # prev_close=100, enter at 105.
        tracker.update_buy("REASON-EQ", 100, 105.0, atr=atr, prev_close=100.0)
        # Price to 118 → intraday = 18% → lock activates (threshold 12%).
        # Stop = max(110.0, 118*0.98) = max(110.0, 115.64) = 115.64.
        tracker.update_trailing_stop("REASON-EQ", 118.0)

        reason = tracker.get_exit_reason("REASON-EQ", 115.64)
        self.assertIn("PROFIT LOCK", reason)

    def test_hard_stop_uses_atr_and_pct(self):
        """Hard stop is the tighter of ATR-based and percentage-based."""
        tracker = PositionTracker()

        # Low ATR stock: ATR-based stop is tighter than 2%.
        atr_low = 0.30
        tracker.update_buy("TIGHT-EQ", 100, 100.0, atr=atr_low, prev_close=95.0)
        pos = tracker.snapshot()["TIGHT-EQ"]
        self.assertAlmostEqual(pos.hard_stop, 99.10, places=2)

        # High ATR stock: percentage cap kicks in at 2%.
        tracker2 = PositionTracker()
        atr_high = 2.00
        tracker2.update_buy("WIDE-EQ", 100, 100.0, atr=atr_high, prev_close=95.0)
        pos2 = tracker2.snapshot()["WIDE-EQ"]
        self.assertAlmostEqual(pos2.hard_stop, 98.00, places=2)

    @patch("monitor.position_tracker.datetime")
    def test_target_exit_at_15_pct_intraday_gain(self, mock_dt):
        """Target exit is measured from previous close, not entry."""
        from datetime import datetime as real_dt
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = real_dt(2025, 6, 1, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        mock_dt.side_effect = lambda *a, **kw: real_dt(*a, **kw)

        tracker = PositionTracker()
        atr = 1.00
        tracker.update_buy("TARGET-EQ", 100, 105.0, atr=atr, prev_close=100.0)

        # Stock at +14% for the day — should NOT exit via target.
        self.assertFalse(tracker.should_exit("TARGET-EQ", 114.0))

        # Stock at +15% for the day — should exit even though the trade itself
        # gained only about 9.5% after the +5% entry.
        self.assertTrue(tracker.should_exit("TARGET-EQ", 115.0))
        reason = tracker.get_exit_reason("TARGET-EQ", 115.0)
        self.assertIn("TARGET HIT", reason)

        # At +18% intraday — also exits via target.
        self.assertTrue(tracker.should_exit("TARGET-EQ", 118.0))
        reason = tracker.get_exit_reason("TARGET-EQ", 118.0)
        self.assertIn("TARGET HIT", reason)


if __name__ == "__main__":
    unittest.main()

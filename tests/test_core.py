from __future__ import annotations

import os
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from config.instruments import Instrument, angel_tradingsymbol_for
from broker.angelone_client import AngelOneClient
from data.market_stream import MarketStream
from execution.order_manager import OrderManager
from monitor.position_tracker import PositionTracker
from risk.risk_manager import RiskManager
from utils.telegram_alert import send_telegram_message

try:
    import pandas as pd
    from strategy.momentum_strategy import MomentumStrategy
except ModuleNotFoundError:
    pd = None
    MomentumStrategy = None


class CoreTests(unittest.TestCase):
    def test_risk_position_size(self) -> None:
        manager = RiskManager(capital=100000, risk_per_trade_pct=1.0)
        self.assertEqual(manager.position_size(entry_price=100, stop_loss=98), 500)
        self.assertEqual(manager.position_size(entry_price=100, stop_loss=100), 0)

    def test_order_manager_requires_resolved_instrument(self) -> None:
        class StubBrokerClient:
            def place_order(self, order_payload: dict) -> dict:
                return {"status": "PLACED", **order_payload}

        with self.assertRaises(ValueError):
            OrderManager(broker_client=StubBrokerClient()).place_market_order("RELIANCE.NS", "BUY", 10)

    def test_order_manager_angel_payload(self) -> None:
        class StubBrokerClient:
            def place_order(self, order_payload: dict) -> dict:
                return {"status": "PLACED", **order_payload}

        instrument = Instrument(symbol="SBIN.NS", exchange="NSE", tradingsymbol="SBIN-EQ", symboltoken="3045")
        order = OrderManager(broker_client=StubBrokerClient()).place_market_order("SBIN.NS", "BUY", 10, instrument=instrument)
        self.assertEqual(order["tradingsymbol"], "SBIN-EQ")
        self.assertEqual(order["symboltoken"], "3045")
        self.assertEqual(order["transactiontype"], "BUY")
        self.assertEqual(order["status"], "PLACED")

    def test_position_tracker(self) -> None:
        tracker = PositionTracker()
        pos = tracker.update_buy("INFY.NS", 10, 100, atr=0.5, prev_close=95.0)
        self.assertEqual(pos.quantity, 10)

        pos = tracker.update_buy("INFY.NS", 10, 110, atr=0.5, prev_close=95.0)
        self.assertEqual(pos.quantity, 20)
        self.assertAlmostEqual(pos.average_price, 105)

        tracker.update_sell("INFY.NS", 5)
        self.assertEqual(tracker.snapshot()["INFY.NS"].quantity, 15)

    @patch("monitor.position_tracker.datetime")
    def test_position_tracker_trailing_stop_only_moves_up(self, mock_dt) -> None:
        from datetime import datetime as real_dt
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = real_dt(2025, 6, 1, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        mock_dt.side_effect = lambda *a, **kw: real_dt(*a, **kw)

        tracker = PositionTracker()
        atr = 1.00
        # prev_close=80 so intraday gain won't hit 12% lock until price ~89.6.
        pos = tracker.update_buy("INFY.NS", 10, 100, atr=atr, prev_close=80.0)
        self.assertEqual(pos.highest_price, 100)
        self.assertAlmostEqual(pos.stop_loss, 98.0, places=2)

        # Price rises to 110 (+10% profit → 1.0x ATR trail).
        # Intraday gain = (110-80)/80 = 37.5% → lock activates.
        # But we want to test normal trailing, so use high prev_close
        # where intraday gain stays below 12% lock threshold.
        tracker2 = PositionTracker()
        pos2 = tracker2.update_buy("INFY2.NS", 10, 100, atr=atr, prev_close=97.0)

        pos2 = tracker2.update_trailing_stop("INFY2.NS", 108)
        assert pos2 is not None
        self.assertEqual(pos2.highest_price, 108)
        # profit = 8%, intraday = (108-97)/97 = 11.3% (below 12% lock).
        # 6-10% tier → 1.5x ATR → 108 - 1.5 = 106.5.
        self.assertAlmostEqual(pos2.stop_loss, 106.5, places=2)

        # Price drops to 106 — stop must NOT move down.
        pos2 = tracker2.update_trailing_stop("INFY2.NS", 106)
        assert pos2 is not None
        self.assertEqual(pos2.highest_price, 108)
        self.assertAlmostEqual(pos2.stop_loss, 106.5, places=2)

    @patch("monitor.position_tracker.datetime")
    def test_position_tracker_tightens_trail_at_profit_tiers(self, mock_dt) -> None:
        from datetime import datetime as real_dt
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = real_dt(2025, 6, 1, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        mock_dt.side_effect = lambda *a, **kw: real_dt(*a, **kw)

        tracker = PositionTracker()
        atr = 1.00
        tracker.update_buy("ITC.NS", 10, 100, atr=atr, prev_close=95.0)

        # At +5% profit → 3-6% tier → 2.0x ATR trail.
        pos = tracker.update_trailing_stop("ITC.NS", 105)
        assert pos is not None
        self.assertAlmostEqual(pos.stop_loss, 103.0, places=2)

    @patch("monitor.position_tracker.datetime")
    def test_position_tracker_intraday_lock_at_12_pct(self, mock_dt) -> None:
        from datetime import datetime as real_dt
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = real_dt(2025, 6, 1, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        mock_dt.side_effect = lambda *a, **kw: real_dt(*a, **kw)

        tracker = PositionTracker()
        atr = 1.00
        # prev_close=100, enter at 105 (+5% intraday).
        tracker.update_buy("TEST.NS", 10, 105, atr=atr, prev_close=100.0)

        # Price to 111 → intraday = 11% (below 12% lock).
        pos = tracker.update_trailing_stop("TEST.NS", 111)
        assert pos is not None
        self.assertFalse(pos.profit_locked)

        # Price to 112 → intraday = 12% → lock activates.
        # Stop = max(100*1.12, 112*0.99) = max(112.0, 110.88) = 112.0.
        pos = tracker.update_trailing_stop("TEST.NS", 112)
        assert pos is not None
        self.assertTrue(pos.profit_locked)
        self.assertAlmostEqual(pos.stop_loss, 112.0, places=2)

        # Price rallies to 119 → stop trails at 1%.
        # Stop = max(112.0, 119*0.99) = max(112.0, 117.81) = 117.81.
        pos = tracker.update_trailing_stop("TEST.NS", 119)
        assert pos is not None
        self.assertAlmostEqual(pos.stop_loss, 117.81, places=2)

        # Price drops to 117.81 — should trigger exit.
        self.assertTrue(tracker.should_exit("TEST.NS", 117.81))

    @patch("monitor.position_tracker.datetime")
    def test_position_tracker_should_exit_when_price_hits_stop_loss(self, mock_dt) -> None:
        from datetime import datetime as real_dt
        from zoneinfo import ZoneInfo
        mock_dt.now.return_value = real_dt(2025, 6, 1, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        mock_dt.side_effect = lambda *a, **kw: real_dt(*a, **kw)

        tracker = PositionTracker()
        atr = 1.00
        tracker.update_buy("HDFCBANK.NS", 10, 100, atr=atr, prev_close=97.0)
        # Price rises to 108 (intraday=(108-97)/97=11.3%, below 12% lock). 6-10% tier → 1.5x ATR.
        tracker.update_trailing_stop("HDFCBANK.NS", 108)

        self.assertFalse(tracker.should_exit("HDFCBANK.NS", 106.60))
        self.assertTrue(tracker.should_exit("HDFCBANK.NS", 106.50))

    @unittest.skipIf(pd is None, "pandas not installed")
    def test_market_stream_converts_angel_candles(self) -> None:
        rows = [
            ["2026-03-23 09:15", "100", "101", "99", "100.5", "1000"],
            ["2026-03-23 09:20", "100.5", "102", "100", "101.25", "1200"],
        ]
        frame = MarketStream._candle_rows_to_frame(rows)
        self.assertEqual(list(frame.columns), ["Open", "High", "Low", "Close", "Volume"])
        self.assertEqual(float(frame.iloc[-1]["Close"]), 101.25)

    def test_market_stream_resolves_angel_instrument(self) -> None:
        class StubAngelClient:
            def resolve_instrument(self, symbol: str, exchange: str = "NSE") -> Instrument:
                return Instrument(symbol=symbol, exchange=exchange, tradingsymbol="RELIANCE-EQ", symboltoken="2885")

        stream = MarketStream(angel_client=StubAngelClient())
        instrument = stream.resolve_instrument("RELIANCE.NS")
        self.assertEqual(instrument.tradingsymbol, "RELIANCE-EQ")
        self.assertEqual(instrument.symboltoken, "2885")

    def test_angelone_client_reports_non_json_response(self) -> None:
        class StubResponse:
            status_code = 200
            text = "<html>bad gateway</html>"
            headers = {"Content-Type": "text/html"}

            def json(self) -> dict:
                raise ValueError("not json")

        with self.assertRaises(ValueError) as ctx:
            AngelOneClient._parse_json_response(StubResponse(), "POST", "https://example.test/searchScrip")
        self.assertIn("non-JSON response", str(ctx.exception))

    def test_angelone_client_request_rejected_error_includes_hint(self) -> None:
        class StubResponse:
            status_code = 200
            text = "<html><head><title>Request Rejected</title></head><body>Request Rejected</body></html>"
            headers = {"Content-Type": "text/html"}

            def json(self) -> dict:
                raise ValueError("not json")

        with self.assertRaises(ValueError) as ctx:
            AngelOneClient._parse_json_response(StubResponse(), "POST", "https://example.test/getLtpData")
        self.assertIn("Primary Static IP", str(ctx.exception))

    def test_angelone_client_resolves_from_scrip_master_fallback(self) -> None:
        client = AngelOneClient(api_key="key", client_id="client", access_token="token")
        client.search_scrip = lambda exchange, search_text: []
        client._load_scrip_master = lambda: [
            {"exch_seg": "NSE", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "token": "2885"}
        ]
        instrument = client.resolve_instrument("RELIANCE.NS")
        self.assertEqual(instrument.tradingsymbol, "RELIANCE-EQ")
        self.assertEqual(instrument.symboltoken, "2885")

    def test_angelone_client_resolves_from_scrip_master_when_search_fails(self) -> None:
        client = AngelOneClient(api_key="key", client_id="client", access_token="token")

        def raising_search(exchange: str, search_text: str) -> list[dict[str, object]]:
            raise ValueError("403 Client Error")

        client.search_scrip = raising_search
        client._load_scrip_master = lambda: [
            {"exch_seg": "NSE", "symbol": "SBIN-EQ", "name": "SBIN", "token": "3045"}
        ]
        instrument = client.resolve_instrument("SBIN.NS")
        self.assertEqual(instrument.tradingsymbol, "SBIN-EQ")
        self.assertEqual(instrument.symboltoken, "3045")

    def test_angelone_client_ltp_uses_order_service_endpoint(self) -> None:
        client = AngelOneClient(api_key="key", client_id="client", access_token="token")

        def stub_post(base_url: str, path: str, body: dict[str, object]) -> dict[str, object]:
            self.assertEqual(base_url, client.ORDER_BASE_URL)
            self.assertEqual(path, "/getLtpData")
            self.assertEqual(
                body,
                {"exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045"},
            )
            return {"status": True, "data": {"ltp": 800.0}}

        client._post = stub_post  # type: ignore[method-assign]
        payload = client.get_ltp_data("NSE", "SBIN-EQ", "3045")
        self.assertEqual(payload["ltp"], 800.0)

    def test_angelone_client_secure_routes_use_angelone_host(self) -> None:
        expected_prefix = "https://apiconnect.angelone.in/rest/secure/angelbroking/"
        self.assertTrue(AngelOneClient.ORDER_BASE_URL.startswith(expected_prefix))
        self.assertTrue(AngelOneClient.USER_BASE_URL.startswith(expected_prefix))
        self.assertTrue(AngelOneClient.PORTFOLIO_BASE_URL.startswith(expected_prefix))
        self.assertTrue(AngelOneClient.TRADE_BASE_URL.startswith(expected_prefix))
        self.assertTrue(AngelOneClient.HISTORICAL_BASE_URL.startswith(expected_prefix))
        self.assertTrue(AngelOneClient.MARKET_BASE_URL.startswith(expected_prefix))

    def test_angelone_client_prefers_env_ip_overrides(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ANGELONE_CLIENT_PUBLIC_IP": "1.2.3.4",
                "ANGELONE_CLIENT_LOCAL_IP": "10.0.0.5",
                "ANGELONE_CLIENT_MAC_ADDRESS": "aa:bb:cc:dd:ee:ff",
            },
            clear=False,
        ):
            client = AngelOneClient(api_key="key", client_id="client", access_token="token")
        self.assertEqual(client.client_public_ip, "1.2.3.4")
        self.assertEqual(client.client_local_ip, "10.0.0.5")
        self.assertEqual(client.client_mac_address, "aa:bb:cc:dd:ee:ff")

    def test_angel_tradingsymbol_for_nse_equity(self) -> None:
        self.assertEqual(angel_tradingsymbol_for("SBIN.NS"), "SBIN-EQ")

    def test_telegram_message_returns_false_for_placeholder_values(self) -> None:
        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": "your_bot_token", "TELEGRAM_CHAT_ID": "your_chat_id"},
            clear=False,
        ), patch("utils.telegram_alert.urlopen") as urlopen:
            self.assertFalse(send_telegram_message("hello"))
        urlopen.assert_not_called()

    def test_telegram_message_swallows_http_errors(self) -> None:
        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": "real-token", "TELEGRAM_CHAT_ID": "12345"},
            clear=False,
        ), patch(
            "utils.telegram_alert.urlopen",
            side_effect=HTTPError("https://api.telegram.org", 404, "Not Found", hdrs=None, fp=None),
        ):
            self.assertFalse(send_telegram_message("hello"))

    def test_telegram_message_splits_large_payloads(self) -> None:
        class StubResponse:
            status = 200

            def __enter__(self) -> "StubResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        long_message = ("line 1\n" * 900) + "line 2"
        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": "real-token", "TELEGRAM_CHAT_ID": "12345"},
            clear=False,
        ), patch("utils.telegram_alert.urlopen", return_value=StubResponse()) as urlopen:
            self.assertTrue(send_telegram_message(long_message))

        self.assertGreater(urlopen.call_count, 1)

    @unittest.skipIf(pd is None or MomentumStrategy is None, "pandas/numpy not installed")
    def test_strategy_signal_shape(self) -> None:
        strategy = MomentumStrategy()
        rows = 60
        frame = pd.DataFrame({
            "Open": [100 + i * 0.1 for i in range(rows)],
            "High": [101 + i * 0.1 for i in range(rows)],
            "Low": [99 + i * 0.1 for i in range(rows)],
            "Close": [100 + i * 0.2 for i in range(rows)],
            "Volume": [1000 + i * 10 for i in range(rows)],
        })
        signal = strategy.build_signal("SBIN.NS", frame)
        if signal is not None:
            self.assertEqual(signal["side"], "BUY")
            self.assertIn("stop_loss", signal)


    def test_probe_tradability_accepts_when_order_succeeds(self) -> None:
        from execution.tradability_filter import TradabilityFilter

        class StubBroker:
            def place_order(self, payload):
                return {"response": {"data": {"orderid": "PROBE123"}}}

            def cancel_order(self, order_id, variety):
                pass

        tf = TradabilityFilter(safe_mode=True)
        ok, reason = tf.probe_tradability(StubBroker(), "SBIN-EQ", "3045")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_probe_tradability_rejects_cautionary_stock(self) -> None:
        from execution.tradability_filter import TradabilityFilter

        class StubBroker:
            def place_order(self, payload):
                raise ValueError(
                    "The order cannot be processed as the token is categorised "
                    "under cautionary listings by the exchange."
                )

        tf = TradabilityFilter(safe_mode=True)
        ok, reason = tf.probe_tradability(StubBroker(), "IMFA-EQ", "1234")
        self.assertFalse(ok)
        self.assertIn("cautionary", reason.lower())
        # Should also be blacklisted now.
        self.assertIn("IMFA", tf.blacklist_summary)

    def test_probe_candidates_filters_and_preserves_order(self) -> None:
        from execution.tradability_filter import TradabilityFilter

        call_count = {"n": 0}

        class StubBroker:
            def place_order(self, payload):
                call_count["n"] += 1
                sym = payload.get("tradingsymbol", "")
                if sym == "BAD-EQ":
                    raise ValueError("cautionary listings")
                return {"response": {"data": {"orderid": f"P{call_count['n']}"}}}

            def cancel_order(self, order_id, variety):
                pass

        candidates = [
            {"symbol": "GOOD1-EQ", "token": "100", "composite_score": 0.9},
            {"symbol": "BAD-EQ", "token": "200", "composite_score": 0.8},
            {"symbol": "GOOD2-EQ", "token": "300", "composite_score": 0.7},
        ]

        tf = TradabilityFilter(safe_mode=True)
        tradable, skipped = tf.probe_candidates(StubBroker(), candidates, max_workers=1)

        self.assertEqual(len(tradable), 2)
        self.assertEqual(tradable[0]["symbol"], "GOOD1-EQ")
        self.assertEqual(tradable[1]["symbol"], "GOOD2-EQ")
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0][0], "BAD-EQ")


    def test_entry_validator_passes_all_checks(self) -> None:
        from execution.entry_validator import validate_entry

        class StubClient:
            def get_market_data(self, mode, tokens):
                return {"fetched": [{
                    "symbolToken": "3045",
                    "ltp": 840.0,       # +7.0% from close of 785.05
                    "open": 800.0,
                    "high": 845.0,
                    "low": 790.0,
                    "close": 785.05,    # prev close
                    "tradeVolume": 500000,
                    "depth": {
                        "buy": [{"price": 839.90, "quantity": 100, "orders": 5}],
                        "sell": [{"price": 840.10, "quantity": 100, "orders": 5}],
                    },
                }]}

            def get_candle_data(self, exchange, token, interval, from_dt, to_dt):
                if interval == "FIVE_MINUTE":
                    return [
                        ["2026-05-02T10:00:00+05:30", 830, 835, 828, 833, 10000],
                        ["2026-05-02T10:05:00+05:30", 833, 838, 832, 837, 12000],
                        ["2026-05-02T10:10:00+05:30", 837, 841, 836, 840, 11000],
                    ]
                elif interval == "ONE_MINUTE":
                    return [
                        ["2026-05-02T10:06:00+05:30", 833, 834, 832, 833, 2000],
                        ["2026-05-02T10:07:00+05:30", 833, 835, 833, 835, 2200],
                        ["2026-05-02T10:08:00+05:30", 835, 837, 834, 836, 2100],
                        ["2026-05-02T10:09:00+05:30", 836, 838, 835, 837, 2300],
                        ["2026-05-02T10:10:00+05:30", 837, 840, 837, 840, 3000],
                    ]
                return []

        result = validate_entry("SBIN-EQ", "3045", StubClient())
        self.assertTrue(result.valid)
        self.assertAlmostEqual(result.live_price, 840.0)
        self.assertTrue(result.breakout_ok)

    def test_entry_validator_rejects_fading_momentum(self) -> None:
        from execution.entry_validator import validate_entry

        class StubClient:
            def get_market_data(self, mode, tokens):
                return {"fetched": [{
                    "symbolToken": "3045",
                    "ltp": 810.0,       # +3.2% — below 5% threshold
                    "open": 800.0,
                    "high": 845.0,
                    "low": 790.0,
                    "close": 785.05,
                    "depth": {"buy": [{"price": 809.9}], "sell": [{"price": 810.1}]},
                }]}

            def get_candle_data(self, *a, **kw):
                return []

        result = validate_entry("SBIN-EQ", "3045", StubClient())
        self.assertFalse(result.valid)
        self.assertIn("outside", result.reason)

    def test_entry_validator_rejects_wide_spread(self) -> None:
        from execution.entry_validator import validate_entry

        class StubClient:
            def get_market_data(self, mode, tokens):
                return {"fetched": [{
                    "symbolToken": "100",
                    "ltp": 200.0,       # +6.4%
                    "open": 190.0,
                    "high": 201.0,
                    "low": 188.0,
                    "close": 188.0,
                    "depth": {
                        "buy": [{"price": 199.0}],   # spread = 2.0 / 200 = 1.0%
                        "sell": [{"price": 201.0}],
                    },
                }]}

            def get_candle_data(self, exchange, token, interval, from_dt, to_dt):
                if interval == "FIVE_MINUTE":
                    return [
                        ["2026-05-02T10:00:00+05:30", 195, 198, 194, 197, 5000],
                        ["2026-05-02T10:05:00+05:30", 197, 199, 196, 199, 6000],
                        ["2026-05-02T10:10:00+05:30", 199, 201, 198, 200, 5500],
                    ]
                elif interval == "ONE_MINUTE":
                    # Volume increasing: avg of first 4 = 1000, last = 1500 → ratio 1.5
                    return [
                        ["2026-05-02T10:06:00+05:30", 197, 198, 197, 198, 1000],
                        ["2026-05-02T10:07:00+05:30", 198, 199, 197, 198, 1000],
                        ["2026-05-02T10:08:00+05:30", 198, 199, 198, 199, 1000],
                        ["2026-05-02T10:09:00+05:30", 199, 200, 199, 200, 1000],
                        ["2026-05-02T10:10:00+05:30", 200, 201, 199, 200, 1500],
                    ]
                return []

        result = validate_entry("TEST-EQ", "100", StubClient())
        self.assertFalse(result.valid)
        self.assertIn("spread", result.reason.lower())


if __name__ == "__main__":
    unittest.main()

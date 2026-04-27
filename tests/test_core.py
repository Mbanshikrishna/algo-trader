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
        pos = tracker.update_buy("INFY.NS", 10, 100)
        self.assertEqual(pos.quantity, 10)

        pos = tracker.update_buy("INFY.NS", 10, 110)
        self.assertEqual(pos.quantity, 20)
        self.assertAlmostEqual(pos.average_price, 105)

        tracker.update_sell("INFY.NS", 5)
        self.assertEqual(tracker.snapshot()["INFY.NS"].quantity, 15)

    def test_position_tracker_trailing_stop_only_moves_up(self) -> None:
        tracker = PositionTracker()
        pos = tracker.update_buy("INFY.NS", 10, 100)
        self.assertEqual(pos.highest_price, 100)
        self.assertEqual(pos.stop_loss, 98.0)

        pos = tracker.update_trailing_stop("INFY.NS", 110)
        assert pos is not None
        self.assertEqual(pos.highest_price, 110)
        self.assertEqual(pos.stop_loss, 108.35)

        pos = tracker.update_trailing_stop("INFY.NS", 108)
        assert pos is not None
        self.assertEqual(pos.highest_price, 110)
        self.assertEqual(pos.stop_loss, 108.35)

    def test_position_tracker_tightens_trail_after_five_percent_profit(self) -> None:
        tracker = PositionTracker()
        tracker.update_buy("ITC.NS", 10, 100)

        pos = tracker.update_trailing_stop("ITC.NS", 105)
        assert pos is not None
        self.assertEqual(pos.stop_loss, 103.42)

    def test_position_tracker_should_exit_when_price_hits_stop_loss(self) -> None:
        tracker = PositionTracker()
        tracker.update_buy("HDFCBANK.NS", 10, 100)
        tracker.update_trailing_stop("HDFCBANK.NS", 110)

        self.assertFalse(tracker.should_exit("HDFCBANK.NS", 108.4))
        self.assertTrue(tracker.should_exit("HDFCBANK.NS", 108.35))

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


if __name__ == "__main__":
    unittest.main()

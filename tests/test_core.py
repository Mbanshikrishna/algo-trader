from __future__ import annotations  # Lets Python postpone evaluation of type hints.

import unittest  # Imports Python's built-in unit testing framework.

from config.instruments import Instrument, angel_tradingsymbol_for  # Imports instrument helpers used by the Angel One integration.
from broker.angelone_client import AngelOneClient  # Imports the broker client for SmartAPI response-parsing tests.
from data.market_stream import MarketStream  # Imports the market data adapter for normalization tests.
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

    def test_order_manager_angel_payload(self) -> None:  # Verifies Angel One order payloads include the broker fields needed for live execution.
        instrument = Instrument(symbol="SBIN.NS", exchange="NSE", tradingsymbol="SBIN-EQ", symboltoken="3045")  # Builds a resolved broker instrument for the order manager.
        order = OrderManager(paper_trade=True).place_market_order("SBIN.NS", "BUY", 10, instrument=instrument)  # Places a simulated broker-shaped market order.
        self.assertEqual(order["tradingsymbol"], "SBIN-EQ")  # Confirms the broker tradingsymbol is propagated into the order payload.
        self.assertEqual(order["symboltoken"], "3045")  # Confirms the broker symbol token is included for live compatibility.
        self.assertEqual(order["transactiontype"], "BUY")  # Confirms the transaction side matches Angel One's expected field.

    def test_position_tracker(self) -> None:  # Verifies buys and sells update tracked positions correctly.
        tracker = PositionTracker()  # Creates a fresh position tracker.
        pos = tracker.update_buy("INFY.NS", 10, 100)  # Adds an initial 10-share position at price 100.
        self.assertEqual(pos.quantity, 10)  # Confirms the initial quantity is stored correctly.

        pos = tracker.update_buy("INFY.NS", 10, 110)  # Adds 10 more shares at a higher price.
        self.assertEqual(pos.quantity, 20)  # Confirms the quantities were combined.
        self.assertAlmostEqual(pos.average_price, 105)  # Confirms the weighted average price was recalculated correctly.

        tracker.update_sell("INFY.NS", 5)  # Sells part of the position.
        self.assertEqual(tracker.snapshot()["INFY.NS"].quantity, 15)  # Confirms the remaining quantity is tracked correctly.

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

    @unittest.skipIf(pd is None, "pandas not installed in current environment")  # Skips the candle-conversion test when pandas is unavailable.
    def test_market_stream_converts_angel_candles(self) -> None:  # Verifies SmartAPI candle arrays are converted into the OHLCV frame expected by the strategy.
        rows = [  # Builds two sample Angel One candle rows.
            ["2026-03-23 09:15", "100", "101", "99", "100.5", "1000"],
            ["2026-03-23 09:20", "100.5", "102", "100", "101.25", "1200"],
        ]
        frame = MarketStream._candle_rows_to_frame(rows)  # Converts the SmartAPI candle arrays into a DataFrame.
        self.assertEqual(list(frame.columns), ["Open", "High", "Low", "Close", "Volume"])  # Confirms the converted frame exposes the expected OHLCV columns.
        self.assertEqual(float(frame.iloc[-1]["Close"]), 101.25)  # Confirms the close price survives the conversion correctly.

    def test_market_stream_resolves_angel_instrument_in_paper_mode(self) -> None:  # Verifies paper trading still allows Angel One symbol resolution for market data.
        class StubAngelClient:  # Provides a minimal stub for Angel One resolution without live network access.
            def resolve_instrument(self, symbol: str, exchange: str = "NSE") -> Instrument:
                return Instrument(symbol=symbol, exchange=exchange, tradingsymbol="RELIANCE-EQ", symboltoken="2885")

        stream = MarketStream(data_provider="angelone", angel_client=StubAngelClient())  # Builds an Angel One stream with a stub broker client.
        instrument = stream.resolve_instrument("RELIANCE.NS")  # Resolves a symbol through the stub broker client.
        self.assertEqual(instrument.tradingsymbol, "RELIANCE-EQ")  # Confirms the instrument resolution works independently of live order mode.
        self.assertEqual(instrument.symboltoken, "2885")  # Confirms the resolved token is cached on the instrument.

    def test_angelone_client_reports_non_json_response(self) -> None:  # Verifies SmartAPI HTML/plaintext failures are surfaced with useful diagnostics.
        class StubResponse:  # Provides the minimal response surface needed by the JSON parser helper.
            status_code = 200
            text = "<html>bad gateway</html>"
            headers = {"Content-Type": "text/html"}

            def json(self) -> dict:
                raise ValueError("not json")

        with self.assertRaises(ValueError) as ctx:  # Confirms the helper raises a descriptive value error on non-JSON content.
            AngelOneClient._parse_json_response(StubResponse(), "POST", "https://example.test/searchScrip")
        self.assertIn("non-JSON response", str(ctx.exception))  # Confirms the raised message explains the parsing failure clearly.

    def test_angelone_client_resolves_from_scrip_master_fallback(self) -> None:  # Verifies instrument resolution can fall back to the public scrip master when searchScrip returns no usable rows.
        client = AngelOneClient(api_key="key", client_id="client", access_token="token")  # Builds a client instance for exercising the fallback resolution path.
        client.search_scrip = lambda exchange, search_text: []  # Simulates an empty searchScrip result from SmartAPI.
        client._load_scrip_master = lambda: [  # Supplies a tiny in-memory scrip master sample for deterministic token resolution.
            {"exch_seg": "NSE", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "token": "2885"}
        ]
        instrument = client.resolve_instrument("RELIANCE.NS")  # Resolves the instrument using the scrip-master fallback path.
        self.assertEqual(instrument.tradingsymbol, "RELIANCE-EQ")  # Confirms the fallback returns the expected Angel One tradingsymbol.
        self.assertEqual(instrument.symboltoken, "2885")  # Confirms the fallback returns the expected Angel One token.

    def test_angel_tradingsymbol_for_nse_equity(self) -> None:  # Verifies Yahoo-style NSE symbols are translated into Angel One equity tradingsymbols.
        self.assertEqual(angel_tradingsymbol_for("SBIN.NS"), "SBIN-EQ")  # Confirms Angel One receives the expected equity tradingsymbol.

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

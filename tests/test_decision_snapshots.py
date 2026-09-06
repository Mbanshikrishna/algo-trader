from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config.instruments import Instrument
from execution.entry_validator import (
    ValidationResult,
    evaluate_shadow_entry_policy,
    validate_entries_batch,
)
from execution.order_manager import OrderManager, OrderState
from monitor.decision_journal import DecisionJournal, decode_payload
from production_replay import (
    calibrate_slippage,
    compare_run,
    export_universe_snapshots,
    summarize_performance,
)
from strategy.market_scanner import (
    INDIA_VIX_TOKEN,
    NIFTY_50_TOKEN,
    NIFTYBEES_TOKEN,
    clear_daily_candle_cache,
    scan_top_gainers,
    score_candidate_quote,
)


class TestDecisionJournal(unittest.TestCase):
    def test_daily_performance_summary_uses_confirmed_closed_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "journal.sqlite3"
            journal = DecisionJournal(database, snapshot_dir=Path(directory) / "u")
            journal.record(
                "position_closed",
                symbol="TEST-EQ",
                decision="closed",
                reason="market close",
                payload={
                    "quantity": 10,
                    "entry_price": 100,
                    "exit_price": 102,
                    "pnl": 17.98,
                    "position": {
                        "highest_price": 103,
                        "lowest_price": 99,
                    },
                },
            )
            run_id = journal.run_id
            journal.close()
            day = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()

            summary = summarize_performance(database, run_id, day)

            self.assertEqual(summary["closed_trades"], 1)
            self.assertEqual(summary["wins"], 1)
            self.assertEqual(summary["net_pnl"], 17.98)
            self.assertEqual(summary["average_mfe_pct"], 3.0)
            self.assertEqual(summary["average_mae_pct"], -1.0)
            self.assertFalse(summary["evidence_ready"])
            self.assertEqual(summary["minimum_evidence_trades"], 50)

            second_run = DecisionJournal(
                database, snapshot_dir=Path(directory) / "u2"
            )
            second_run.record(
                "position_closed",
                symbol="SECOND-EQ",
                decision="closed",
                reason="target",
                payload={
                    "quantity": 1,
                    "entry_price": 100,
                    "exit_price": 101,
                    "pnl": 0.79,
                    "position": {
                        "highest_price": 101,
                        "lowest_price": 100,
                    },
                },
            )
            second_run.close()

            cumulative = summarize_performance(database, all_runs=True)
            self.assertEqual(cumulative["run_id"], "all")
            self.assertEqual(cumulative["closed_trades"], 2)
            self.assertEqual(cumulative["net_pnl"], 18.77)
            daily_all_runs = summarize_performance(
                database, trading_date=day, all_runs=True
            )
            self.assertEqual(daily_all_runs["closed_trades"], 2)

    def test_large_payload_is_compressed_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "journal.sqlite3"
            journal = DecisionJournal(database, snapshot_dir=Path(directory) / "u")
            payload = {"quotes": [{"symbol": "TEST", "value": "x" * 1000}] * 100}
            journal.record("scan_completed", payload=payload)
            event = journal.events("scan_completed")[0]
            journal.close()

            connection = sqlite3.connect(database)
            stored = connection.execute(
                "SELECT payload_json FROM decision_events"
            ).fetchone()[0]
            connection.close()

            self.assertTrue(stored.startswith("gzip+base64:"))
            self.assertEqual(decode_payload(stored), payload)
            self.assertEqual(event["payload"], payload)
            replay_run, comparisons = compare_run(database)
            self.assertEqual(replay_run, journal.run_id)
            self.assertEqual(len(comparisons), 1)

    def test_shadow_entry_policy_uses_only_completed_minute_candles(self):
        candles = [
            ["2026-08-21T10:00:00+05:30", 105.8, 106.0, 105.7, 105.9, 100],
            ["2026-08-21T10:01:00+05:30", 105.9, 106.1, 105.8, 106.0, 100],
            ["2026-08-21T10:02:00+05:30", 106.0, 106.2, 105.9, 106.1, 100],
            ["2026-08-21T10:03:00+05:30", 106.1, 106.3, 106.0, 106.2, 100],
            ["2026-08-21T10:04:00+05:30", 106.2, 106.4, 106.1, 106.3, 100],
            ["2026-08-21T10:05:00+05:30", 106.3, 106.6, 106.35, 106.5, 200],
            ["2026-08-21T10:06:00+05:30", 106.5, 106.7, 106.4, 106.6, 200],
            # Still forming at 10:07:30 and must not enter either calculation.
            ["2026-08-21T10:07:00+05:30", 106.6, 107.0, 105.0, 105.1, 10_000],
        ]
        result = evaluate_shadow_entry_policy(
            ValidationResult(
                valid=True,
                symbol="TEST-EQ",
                live_price=106.6,
                range_position=0.97,
            ),
            {"volume_candles": candles},
            now=datetime(2026, 8, 21, 10, 7, 30, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        self.assertEqual(result["decision"], "accepted")
        self.assertEqual(result["breakout_path"], "persistent")
        self.assertEqual(result["completed_minute_count"], 7)
        self.assertLess(result["completed_volume_ratio"], 3)

    def test_redacts_secrets_and_persists_immutable_dated_universe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "journal.sqlite3"
            journal = DecisionJournal(
                database,
                snapshot_dir=root / "universes",
                config={"api_key": "secret", "paper_trade": True},
            )
            journal.record(
                "quote",
                payload={
                    "authorization": "Bearer secret",
                    "dhan_access_token": "secret",
                    "jwtToken": "secret",
                    "symbolToken": "3045",
                    "nested": {"totp_secret": "secret"},
                },
            )
            universe_file = journal.snapshot_universe(
                "2026-08-21",
                [{
                    "symbol": "SBIN-EQ",
                    "token": "3045",
                    "name": "SBI",
                    "restricted_reason": "ASM",
                    "is_fno": True,
                    "tradability_lists_complete": True,
                }],
            )
            event = journal.events("quote")[0]
            journal.close()

            self.assertEqual(event["payload"]["authorization"], "[REDACTED]")
            self.assertEqual(event["payload"]["dhan_access_token"], "[REDACTED]")
            self.assertEqual(event["payload"]["jwtToken"], "[REDACTED]")
            self.assertEqual(event["payload"]["nested"]["totp_secret"], "[REDACTED]")
            self.assertEqual(event["payload"]["symbolToken"], "3045")
            self.assertTrue(universe_file.exists())

            connection = sqlite3.connect(database)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE decision_events SET decision = 'changed'")
            connection.close()

            exported = export_universe_snapshots(database, root / "universe.json")
            payload = json.loads(exported.read_text(encoding="utf-8"))
            self.assertEqual(payload["2026-08-21"][0]["symbol"], "SBIN-EQ")
            self.assertEqual(
                payload["2026-08-21"][0]["restricted_reason"], "ASM"
            )
            self.assertTrue(payload["2026-08-21"][0]["is_fno"])

    def test_replay_recomputes_market_scan_validation_and_sizing(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "journal.sqlite3"
            journal = DecisionJournal(
                database, snapshot_dir=Path(directory) / "u", mode="live"
            )
            journal.record(
                "market_gate",
                decision="trade",
                payload={
                    "market_quotes": [
                        {
                            "symbolToken": NIFTY_50_TOKEN,
                            "ltp": 101,
                            "open": 100,
                            "close": 100,
                            "percentChange": 1,
                        },
                        {
                            "symbolToken": INDIA_VIX_TOKEN,
                            "percentChange": 1,
                        },
                        {
                            "symbolToken": NIFTYBEES_TOKEN,
                            "ltp": 101,
                            "avgPrice": 100,
                        },
                    ],
                    "breadth_quotes": [
                        {"symbolToken": "1", "percentChange": 1}
                    ],
                },
            )

            quote = {
                "_symbol": "TEST-EQ",
                "_token": "1",
                "_name": "TEST",
                "symbolToken": "1",
                "ltp": 106,
                "close": 100,
                "open": 100,
                "high": 106,
                "low": 100,
                "tradeVolume": 200_000,
                "totBuyQuan": 100,
                "totSellQuan": 100,
            }
            daily = {
                "1": [
                    ["2026-08-19T00:00:00+05:30", 99, 100, 98, 99, 100_000],
                    ["2026-08-20T00:00:00+05:30", 99, 101, 99, 100, 100_000],
                ]
            }
            ranked = [score_candidate_quote(quote, daily["1"])]
            journal.record(
                "scan_completed",
                decision="candidates_ranked",
                payload={
                    "quotes": [quote],
                    "daily_candles": daily,
                    "ranked_candidates": ranked,
                    "top_n": 1,
                },
            )
            validation_quote = {
                **quote,
                "depth": {
                    "buy": [{"price": 105.9}],
                    "sell": [{"price": 106.0}],
                },
            }
            journal.record(
                "candidate_validation",
                symbol="TEST-EQ",
                token="1",
                decision="accepted",
                reason="All checks passed",
                payload={
                    "quote": validation_quote,
                    "candles": {
                        "volume_candles": [
                            ["2026-08-21T09:58:00+05:30", 1, 1, 1, 1, 100],
                            ["2026-08-21T09:59:00+05:30", 1, 1, 1, 1, 100],
                            ["2026-08-21T10:00:00+05:30", 1, 1, 1, 1, 250],
                        ]
                    },
                },
            )
            journal.record(
                "position_sized",
                symbol="TEST-EQ",
                decision="200",
                payload={
                    "capital": 100_000,
                    "entry_price": 100,
                    "stop_price": 98,
                    "risk_per_trade_pct": 1,
                    "maximum_notional": 20_000,
                    "quantity": 200,
                },
            )
            journal.record(
                "position_evaluated",
                symbol="TEST-EQ",
                decision="hold",
                payload={
                    "stop_check_price": 99,
                    "position_after": {
                        "average_price": 100,
                        "stop_loss": 98,
                        "hard_stop": 98,
                    },
                },
            )
            run_id = journal.run_id
            journal.close()

            replayed_run, comparisons = compare_run(database, run_id)

            self.assertEqual(replayed_run, run_id)
            self.assertEqual(len(comparisons), 5)
            self.assertTrue(all(comparison.matched for comparison in comparisons))


class TestConfirmedFillCalibration(unittest.TestCase):
    def test_paper_fills_are_not_used_to_calibrate_live_slippage(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "journal.sqlite3"
            journal = DecisionJournal(database, snapshot_dir=Path(directory) / "u")
            journal.record(
                "confirmed_fill",
                symbol="TEST-EQ",
                payload={
                    "side": "BUY",
                    "reference_price": 100,
                    "fill_price": 110,
                    "filled_quantity": 10,
                },
            )
            run_id = journal.run_id
            journal.close()

            calibration = calibrate_slippage(database, run_id)

        self.assertEqual(calibration["mode"], "paper")
        self.assertEqual(calibration["confirmed_fills"], 0)

    def test_order_manager_journals_fill_and_calibrates_adverse_slippage(self):
        class Broker:
            def place_order(self, _payload):
                return {"response": {"data": {"orderid": "order-1"}}}

            def get_order_book(self):
                return {
                    "data": [
                        {
                            "orderid": "order-1",
                            "orderstatus": "complete",
                            "filledshares": "10",
                            "averageprice": "100.20",
                        }
                    ]
                }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "journal.sqlite3"
            journal = DecisionJournal(
                database, snapshot_dir=Path(directory) / "u", mode="live"
            )
            manager = OrderManager(Broker(), event_sink=journal.record)
            instrument = Instrument(
                symbol="TEST-EQ",
                exchange="NSE",
                tradingsymbol="TEST-EQ",
                symboltoken="1",
            )

            result = manager.place_market_order(
                "TEST-EQ",
                "BUY",
                10,
                instrument=instrument,
                current_price=100,
            )
            execution = manager.wait_for_fill(result, "TEST-EQ", 10)
            run_id = journal.run_id
            journal.close()

            calibration = calibrate_slippage(database, run_id)

            self.assertEqual(execution.state, OrderState.FILLED)
            self.assertEqual(calibration["confirmed_fills"], 1)
            self.assertEqual(calibration["recommended_backtest_slippage_bps"], 20)


class TestProductionCaptureHooks(unittest.TestCase):
    def test_scanner_and_validator_capture_raw_point_in_time_inputs(self):
        clear_daily_candle_cache()
        self.addCleanup(clear_daily_candle_cache)
        quote = {
            "symbolToken": "1",
            "ltp": 106,
            "close": 100,
            "open": 100,
            "high": 106,
            "low": 100,
            "tradeVolume": 200_000,
            "totBuyQuan": 60,
            "totSellQuan": 40,
            "depth": {
                "buy": [{"price": 105.9, "quantity": 100}],
                "sell": [{"price": 106.0, "quantity": 90}],
            },
            "exchFeedTime": "21-Aug-2026 10:00:15",
        }
        volume_candles = [
            ["2026-08-21T09:58:00+05:30", 1, 1, 1, 1, 100],
            ["2026-08-21T09:59:00+05:30", 1, 1, 1, 1, 100],
            ["2026-08-21T10:00:00+05:30", 1, 1, 1, 1, 250],
        ]

        class Broker:
            def get_market_data(self, _mode, _tokens):
                return {"fetched": [dict(quote)]}

            def get_candle_data(self, _exchange, _token, interval, *_range):
                if interval == "ONE_DAY":
                    return [
                        ["2026-08-19T00:00:00+05:30", 99, 100, 98, 99, 100_000],
                        ["2026-08-20T00:00:00+05:30", 99, 101, 99, 100, 100_000],
                    ]
                return volume_candles

        events: list[tuple[str, dict]] = []

        def sink(event_type, **kwargs):
            events.append((event_type, kwargs))

        candidate = {"symbol": "TEST-EQ", "token": "1", "name": "TEST"}
        ranked = scan_top_gainers(Broker(), [candidate], top_n=1, event_sink=sink)
        validated = validate_entries_batch(
            ranked,
            Broker(),
            max_valid=1,
            max_workers=1,
            event_sink=sink,
        )

        scan_event = next(payload for name, payload in events if name == "scan_completed")
        validation_event = next(
            payload for name, payload in events if name == "candidate_validation"
        )
        self.assertEqual(len(validated), 1)
        self.assertEqual(scan_event["payload"]["quotes"][0]["depth"]["buy"][0]["price"], 105.9)
        self.assertEqual(
            validation_event["payload"]["candles"]["volume_candles"][-1][5],
            250,
        )
        self.assertEqual(validation_event["decision"], "accepted")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from requests import HTTPError

from production_backtest import (
    BacktestConfig,
    BacktestDay,
    BacktestTrade,
    CandleStore,
    HistoricalStock,
    MarketData,
    _completed_bars,
    backtest_day,
    break_even_plus_cost_floor,
    breakout_persisted_or_retested,
    build_point_in_time_quote,
    calculate_backtest_quantity,
    entry_atr_at,
    load_universe_snapshots,
    market_gate_at,
    rank_candidates_at,
    resolve_backtest_dates,
    resolve_slippage_bps,
    scan_times,
    simulate_trade,
    staged_stop_floor,
    universe_for_day,
    validate_candidate_at,
    write_report_files,
)
from strategy.market_scanner import (
    INDIA_VIX_TOKEN,
    NIFTY_50_CONSTITUENTS,
    NIFTY_50_TOKEN,
    NIFTYBEES_TOKEN,
)

DAY = "2026-01-05"


def bar(
    at: str, open_: float, high: float, low: float, close: float, volume: float
) -> list:
    return [f"{DAY}T{at}:00+05:30", open_, high, low, close, volume]


def daily(day: str, close: float, volume: float = 100_000) -> list:
    return [f"{day}T00:00:00+05:30", close, close, close, close, volume]


def bullish_market() -> MarketData:
    previous = {
        NIFTY_50_TOKEN: 100.0,
        INDIA_VIX_TOKEN: 20.0,
        NIFTYBEES_TOKEN: 100.0,
    }
    intraday = {
        NIFTY_50_TOKEN: [
            bar("09:15", 100, 101, 100, 100.5, 10),
            bar("09:55", 100.5, 102, 100, 101, 10),
            bar("10:15", 101, 103, 101, 102, 10),
        ],
        INDIA_VIX_TOKEN: [
            bar("09:15", 20, 20, 19, 20, 10),
            bar("09:55", 20, 20, 19, 20, 10),
            bar("10:15", 20, 20, 19, 20, 10),
        ],
        NIFTYBEES_TOKEN: [
            bar("09:15", 100, 101, 99, 100, 100),
            bar("09:55", 100, 103, 100, 102, 300),
            bar("10:15", 102, 104, 102, 103, 300),
        ],
    }
    for token in list(NIFTY_50_CONSTITUENTS.values())[:25]:
        previous[token] = 100.0
        intraday[token] = [
            bar("09:15", 100, 101, 99, 100.5, 10),
            bar("09:55", 100.5, 102, 100, 101, 10),
            bar("10:15", 101, 103, 101, 102, 10),
        ]
    return MarketData(previous_closes=previous, intraday_candles=intraday)


def stock(
    symbol: str, closes: list[tuple[str, float, float]], previous_close: float = 100.0
) -> HistoricalStock:
    candles = []
    prior_close = previous_close
    for at, close, volume in closes:
        candles.append(
            bar(
                at,
                prior_close,
                max(prior_close, close) + 0.2,
                min(prior_close, close) - 0.2,
                close,
                volume,
            )
        )
        prior_close = close
    return HistoricalStock(
        symbol=symbol,
        token=symbol,
        previous_close=previous_close,
        completed_daily_candles=[
            daily("2026-01-01", 98, 100_000),
            daily("2026-01-02", 100, 100_000),
        ],
        intraday_candles=candles,
    )


class TestPointInTimeInputs(unittest.TestCase):
    def test_strict_range_position_rejects_below_point_95(self):
        item = stock(
            "RANGE-EQ",
            [
                ("09:53", 103, 100),
                ("09:54", 104, 100),
                ("09:55", 105.6, 100),
                ("10:00", 106, 1_000),
            ],
        )
        item.intraday_candles[0] = bar(
            "09:53", 100, 103.2, 100, 103, 100
        )
        item.intraday_candles[1] = bar(
            "09:54", 103, 104.2, 103, 104, 100
        )
        item.intraday_candles[2] = bar(
            "09:55", 104, 106, 104, 105.6, 100
        )

        valid, reason = validate_candidate_at(
            {"ltp": 105.6},
            item,
            "10:00",
            BacktestConfig(entry_range_position_min=0.95),
        )

        self.assertFalse(valid)
        self.assertIn("range position", reason)

    def test_completed_minute_volume_excludes_forming_candle(self):
        item = stock(
            "VOLUME-EQ",
            [
                ("09:57", 105.5, 100),
                ("09:58", 105.7, 100),
                ("09:59", 106, 100),
                ("10:00", 106.2, 10_000),
            ],
        )
        config = BacktestConfig(
            intraday_interval="ONE_MINUTE",
            entry_range_position_min=0.85,
            completed_minute_volume_required=True,
        )

        valid, reason = validate_candidate_at(
            {"ltp": 106}, item, "10:00", config
        )

        self.assertFalse(valid)
        self.assertEqual(reason, "volume confirmation failed")

    def test_breakout_requires_persistence_or_successful_retest(self):
        level = 106.0
        persistent = [
            bar("10:00", 106, 106.3, 105.95, 106.1, 100),
            bar("10:01", 106.1, 106.4, 106.0, 106.2, 100),
        ]
        retest = [
            bar("10:00", 106, 106.4, 106.0, 106.2, 100),
            bar("10:01", 106.2, 106.2, 105.7, 105.8, 100),
            bar("10:02", 105.8, 106.2, 105.9, 106.1, 100),
        ]
        failed = [
            bar("10:00", 106, 106.4, 106.0, 106.2, 100),
            bar("10:01", 106.2, 106.2, 105.4, 105.6, 100),
        ]

        self.assertTrue(breakout_persisted_or_retested(persistent, level))
        self.assertTrue(breakout_persisted_or_retested(retest, level))
        self.assertFalse(breakout_persisted_or_retested(failed, level))

    def test_observation_stages_are_disabled_by_default(self):
        self.assertIsNone(staged_stop_floor(100, 110, BacktestConfig()))

    def test_observation_stages_progress_without_loosening(self):
        config = BacktestConfig(staged_stops_enabled=True)

        self.assertIsNone(staged_stop_floor(100, 100.99, config))
        self.assertEqual(staged_stop_floor(100, 101, config), 98.75)
        self.assertEqual(staged_stop_floor(100, 102, config), 99.65)
        self.assertEqual(staged_stop_floor(100, 103, config), 101.0)
        self.assertEqual(staged_stop_floor(100, 110, config), 101.0)

    def test_staged_floor_earned_on_one_bar_applies_to_next_bar(self):
        item = stock(
            "STAGED-EQ",
            [("09:55", 100, 10_000), ("10:00", 102, 10_000),
             ("10:05", 100, 10_000)],
        )
        item.intraday_candles = [
            bar("09:55", 100, 100.2, 99.8, 100, 10_000),
            bar("10:00", 100, 102.1, 99.8, 102, 10_000),
            bar("10:05", 102, 102.2, 99.5, 100, 10_000),
        ]

        trade = simulate_trade(
            item, "10:00", 10, 0, 3, 0.8,
            BacktestConfig(
                slippage_bps=0,
                fees_bps_per_side=0,
                entry_delay_minutes=0,
                staged_stops_enabled=True,
            ),
        )

        self.assertEqual(trade.exit_reason, "STAGED_STOP")
        self.assertEqual(trade.exit_price, 99.65)

    def test_staged_floor_does_not_use_same_candle_high(self):
        item = stock(
            "ORDERING-EQ",
            [("09:55", 100, 10_000), ("10:00", 101, 10_000)],
        )
        item.intraday_candles = [
            bar("09:55", 100, 100.2, 99.8, 100, 10_000),
            # This candle touches both the initial stop and the +3% stage.
            bar("10:00", 100, 103.5, 98.0, 101, 10_000),
        ]

        trade = simulate_trade(
            item, "10:00", 10, 0, 3, 0.8,
            BacktestConfig(
                slippage_bps=0,
                fees_bps_per_side=0,
                entry_delay_minutes=0,
                staged_stops_enabled=True,
            ),
        )

        self.assertEqual(trade.exit_reason, "HARD_STOP")
        self.assertLess(trade.exit_price, trade.entry_price)

    def test_break_even_plus_cost_activates_after_two_percent(self):
        config = BacktestConfig(
            slippage_bps=5,
            fees_bps_per_side=10,
            break_even_plus_cost_enabled=True,
        )

        self.assertIsNone(break_even_plus_cost_floor(100, 101.99, config))
        floor = break_even_plus_cost_floor(100, 102, config)

        self.assertGreater(floor, 100)
        exit_fill = floor * (1 - config.slippage_bps / 10_000)
        net = exit_fill - 100 - (100 + exit_fill) * 0.001
        self.assertGreaterEqual(net, -0.01)

    def test_break_even_plus_cost_earned_on_prior_bar_exits_near_flat(self):
        item = stock(
            "COST-EQ",
            [("09:55", 100, 100), ("10:00", 102, 100),
             ("10:05", 100, 100)],
        )
        item.intraday_candles = [
            bar("09:55", 100, 100.2, 99.8, 100, 100),
            bar("10:00", 100, 102.1, 99.8, 102, 100),
            bar("10:05", 102, 102.1, 99.9, 100, 100),
        ]

        trade = simulate_trade(
            item,
            "10:00",
            10,
            0,
            3,
            0.8,
            BacktestConfig(
                slippage_bps=0,
                fees_bps_per_side=10,
                entry_delay_minutes=0,
                break_even_plus_cost_enabled=True,
            ),
        )

        self.assertEqual(trade.exit_reason, "BREAK_EVEN_PLUS_COST")
        self.assertAlmostEqual(trade.pnl, 0, delta=0.1)

    def test_one_minute_scan_schedule_matches_production_retry_cadence(self):
        self.assertEqual(
            scan_times("10:00", "10:06", step_minutes=2),
            ["10:00", "10:02", "10:04", "10:06"],
        )

    def test_breakout_uses_only_completed_five_minute_candles(self):
        candles = [
            bar("09:55", 100, 105, 99, 104, 100),
            bar("10:00", 104, 110, 103, 109, 100),
        ]

        at_1002 = _completed_bars(candles, "10:02", 5)
        at_1005 = _completed_bars(candles, "10:05", 5)

        self.assertEqual([_time[0][11:16] for _time in at_1002], ["09:55"])
        self.assertEqual(
            [_time[0][11:16] for _time in at_1005], ["09:55", "10:00"]
        )

    def test_dated_universe_uses_latest_snapshot_known_on_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.json"
            path.write_text(
                json.dumps(
                    {
                        "2025-01-01": [{"symbol": "OLD-EQ", "token": "1"}],
                        "2025-07-01": [{"symbol": "NEW-EQ", "token": "2"}],
                    }
                ),
                encoding="utf-8",
            )

            snapshots = load_universe_snapshots(path)

        self.assertEqual(
            universe_for_day(snapshots, "2025-06-30")[0]["symbol"], "OLD-EQ"
        )
        self.assertEqual(
            universe_for_day(snapshots, "2025-07-01")[0]["symbol"], "NEW-EQ"
        )

    def test_current_bar_is_not_visible_at_its_open_timestamp(self):
        item = stock("LATE-EQ", [("09:55", 104, 120_000), ("10:00", 107, 200_000)])

        quote = build_point_in_time_quote(item, "10:00")

        self.assertEqual(quote["ltp"], 104)
        self.assertEqual(quote["tradeVolume"], 120_000)
        self.assertEqual(rank_candidates_at([item], "10:00"), [])

    def test_cross_section_uses_production_composite_score(self):
        common = [
            ("09:15", 101, 15_000),
            ("09:20", 102, 15_000),
            ("09:25", 103, 15_000),
            ("09:30", 104, 15_000),
            ("09:35", 104.5, 15_000),
            ("09:55", 106, 40_000),
        ]
        high_relative_volume = stock("HIGHVOL-EQ", common)
        low_relative_volume = stock("LOWVOL-EQ", common)
        low_relative_volume.completed_daily_candles = [
            daily("2026-01-01", 98, 1_000_000),
            daily("2026-01-02", 100, 1_000_000),
        ]

        ranked = rank_candidates_at(
            [low_relative_volume, high_relative_volume], "10:00"
        )

        self.assertEqual([row["symbol"] for row in ranked], ["HIGHVOL-EQ", "LOWVOL-EQ"])


class TestProductionWorkflow(unittest.TestCase):
    def test_intraday_market_gate_reproduces_all_four_factors(self):
        gate = market_gate_at(bullish_market(), "10:00")

        self.assertTrue(gate.bullish)
        self.assertEqual(gate.score, 4)
        self.assertTrue(gate.index_pass)
        self.assertTrue(gate.breadth_pass)
        self.assertTrue(gate.strength_pass)
        self.assertTrue(gate.volatility_pass)

    def test_market_gate_retries_hourly_before_scanning(self):
        market = bullish_market()
        for token in market.intraday_candles:
            last = market.intraday_candles[token][-1]
            market.intraday_candles[token] = [
                bar(
                    "10:55",
                    float(last[1]),
                    float(last[2]),
                    float(last[3]),
                    float(last[4]),
                    float(last[5]),
                )
            ]
        candidate = stock(
            "LATESTART-EQ",
            [
                ("10:25", 101, 15_000),
                ("10:30", 102, 15_000),
                ("10:35", 103, 15_000),
                ("10:40", 104, 15_000),
                ("10:45", 104.5, 15_000),
                ("10:55", 106, 40_000),
                ("11:00", 101, 20_000),
            ],
        )
        candidate.intraday_candles[-1] = bar("11:00", 105, 106, 100, 101, 20_000)

        result = backtest_day(
            DAY,
            [candidate],
            market,
            BacktestConfig(
                slippage_bps=0, fees_bps_per_side=0, entry_delay_minutes=0
            ),
        )

        self.assertEqual(result.market_checks[:2], [("10:00", 1), ("11:00", 3)])
        self.assertEqual(result.trades[0].entry_time, "11:00")

    def test_profit_lock_allows_one_reentry_after_cooldown(self):
        first = stock(
            "FIRST-EQ",
            [
                ("09:15", 101, 15_000),
                ("09:20", 102, 15_000),
                ("09:25", 103, 15_000),
                ("09:30", 104, 15_000),
                ("09:35", 104.5, 15_000),
                ("09:55", 106, 40_000),
                ("10:00", 112.5, 20_000),
                ("10:05", 110, 20_000),
            ],
        )
        # Explicit OHLC values create a +12% intraday high, then breach the
        # newly active profit-lock stop on the following candle.
        first.intraday_candles[-2] = bar("10:00", 106, 113, 105, 112.5, 20_000)
        first.intraday_candles[-1] = bar("10:05", 111, 112, 110, 110.5, 20_000)

        second = stock(
            "SECOND-EQ",
            [
                ("09:15", 100.5, 15_000),
                ("09:20", 101, 15_000),
                ("09:25", 101.5, 15_000),
                ("09:30", 102, 15_000),
                ("09:35", 102, 15_000),
                ("09:55", 102, 15_000),
                ("10:00", 103, 15_000),
                ("10:05", 104, 15_000),
                ("10:10", 104.5, 15_000),
                ("10:15", 106, 40_000),
                ("10:20", 101, 20_000),
            ],
        )
        second.intraday_candles[-1] = bar("10:20", 105, 106, 100, 101, 20_000)

        result = backtest_day(
            DAY,
            [first, second],
            bullish_market(),
            BacktestConfig(
                slippage_bps=0, fees_bps_per_side=0, entry_delay_minutes=0
            ),
        )

        self.assertEqual(len(result.trades), 2)
        self.assertEqual(result.trades[0].symbol, "FIRST-EQ")
        self.assertEqual(result.trades[0].exit_reason, "PROFIT_LOCK")
        self.assertEqual(result.trades[1].symbol, "SECOND-EQ")
        self.assertEqual(result.trades[1].reentry_round, 1)


class TestBacktestPersistence(unittest.TestCase):
    def test_history_cache_key_includes_requested_range(self):
        first = CandleStore._range_key(
            "history", "2025-01-01 09:00", "2025-12-31 15:30"
        )
        second = CandleStore._range_key(
            "history", "2026-01-01 09:00", "2026-12-31 15:30"
        )

        self.assertNotEqual(first, second)
        self.assertEqual(CandleStore._range_key("2026-01-05", "a", "b"), "2026-01-05")

    def test_auto_slippage_uses_latest_confirmed_fill_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "old" / "slippage_calibration.json"
            latest = Path(directory) / "latest" / "slippage_calibration.json"
            old.parent.mkdir()
            latest.parent.mkdir()
            old.write_text(
                json.dumps({"confirmed_fills": 2, "recommended_backtest_slippage_bps": 4}),
                encoding="utf-8",
            )
            latest.write_text(
                json.dumps({"confirmed_fills": 3, "recommended_backtest_slippage_bps": 9}),
                encoding="utf-8",
            )
            os.utime(old, (1_700_000_000, 1_700_000_000))
            os.utime(latest, (1_800_000_000, 1_800_000_000))

            value, source = resolve_slippage_bps("auto", directory)

        self.assertEqual(value, 9)
        self.assertIn("slippage_calibration.json", source)

    def test_entry_uses_completed_five_minute_atr_and_next_executable_bar(self):
        item = stock(
            "ATR-EQ",
            [
                ("09:55", 106, 40_000),
                ("10:00", 108, 20_000),
                ("10:01", 109, 20_000),
                ("10:02", 110, 20_000),
            ],
        )
        item.intraday_candles[-2] = bar("10:01", 109, 109.5, 108.5, 109, 20_000)
        item.breakout_candles = [
            bar("09:45", 100, 102, 99, 101, 100),
            bar("09:50", 101, 105, 100, 104, 100),
            bar("09:55", 104, 107, 103, 106, 100),
            # This large open candle must not affect ATR at 10:00.
            bar("10:00", 106, 140, 90, 120, 100),
        ]
        expected_atr = entry_atr_at(item, "10:00", 106)

        trade = simulate_trade(
            item,
            "10:00",
            10,
            0,
            3,
            0.8,
            BacktestConfig(
                slippage_bps=0,
                fees_bps_per_side=0,
                intraday_interval="ONE_MINUTE",
                entry_delay_minutes=1,
            ),
        )

        self.assertEqual(trade.signal_time, "10:00")
        self.assertEqual(trade.entry_time, "10:01")
        self.assertEqual(trade.entry_price, 109)
        self.assertEqual(trade.entry_atr, round(expected_atr, 6))

    def test_historical_restriction_is_applied_before_validation(self):
        blocked = stock(
            "BLOCKED-EQ",
            [
                ("09:15", 101, 15_000), ("09:20", 102, 15_000),
                ("09:25", 103, 15_000), ("09:30", 104, 15_000),
                ("09:35", 104.5, 15_000), ("09:55", 107, 40_000),
                ("10:00", 107, 20_000), ("10:01", 107, 20_000),
            ],
        )
        blocked.restricted_reason = "ASM"

        result = backtest_day(
            DAY,
            [blocked],
            bullish_market(),
            BacktestConfig(entry_delay_minutes=0),
        )

        self.assertEqual(result.trades, [])

    def test_explicit_dates_make_replay_reproducible(self):
        with patch.dict(
            "os.environ",
            {
                "BACKTEST_START_DATE": "2025-08-21",
                "BACKTEST_END_DATE": "2026-08-20",
            },
        ):
            start, end = resolve_backtest_dates(datetime(2030, 1, 1), 365)

        self.assertEqual(start.isoformat(), "2025-08-21")
        self.assertEqual(end.isoformat(), "2026-08-20")

    def test_logged_production_sizing_uses_leveraged_notional(self):
        config = BacktestConfig(
            capital=100_000,
            leverage=5,
            sizing_mode="logged_production_notional",
        )

        quantity = calculate_backtest_quantity(588.65, 576.88, config)

        self.assertEqual(quantity, 806)

    def test_report_files_reconcile_daily_and_trade_outputs(self):
        trade = BacktestTrade(
            date=DAY,
            symbol="TEST-EQ",
            entry_time="10:00",
            exit_time="10:05",
            entry_price=100.0,
            exit_price=101.0,
            quantity=10,
            gross_pnl=10.0,
            fees=2.0,
            pnl=8.0,
            exit_reason="MARKET_CLOSE",
            reentry_round=0,
            market_score=3,
            composite_score=0.75,
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"BACKTEST_REPORT_DIR": directory}):
                paths = write_report_files(
                    [trade], [BacktestDay(date=DAY, trades=[trade])]
                )

            self.assertIn(f"{DAY},8.00,1", paths["daily"].read_text())
            self.assertIn("TEST-EQ", paths["trades"].read_text())
            self.assertTrue(paths["manifest"].exists())

    def test_rate_limit_is_cooled_down_and_retried(self):
        class Broker:
            calls = 0

            def __init__(self, error):
                self.error = error

            def get_candle_data(self, *_args):
                self.calls += 1
                if self.calls == 1:
                    raise self.error
                return [["2026-01-05T09:15:00+05:30", 1, 1, 1, 1, 1]]

        errors = (
            HTTPError("status=403: exceeding access rate"),
            ValueError("Too many requests"),
        )
        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                broker = Broker(error)
                with tempfile.TemporaryDirectory() as directory:
                    store = CandleStore(broker, directory)
                    with (
                        patch.dict(
                            "os.environ",
                            {"BACKTEST_RATE_LIMIT_COOLDOWN_SECONDS": "0"},
                        ),
                        patch("production_backtest.time.sleep"),
                    ):
                        candles = store._fetch(
                            "1",
                            "FIVE_MINUTE",
                            "2026-01-05 09:15",
                            "2026-01-05 15:30",
                        )

                self.assertEqual(broker.calls, 2)
                self.assertEqual(len(candles), 1)

    def test_ambiguous_chunk_limit_is_returned_to_splitter(self):
        class Broker:
            def get_candle_data(self, *_args):
                raise ValueError("Too many requests")

        with tempfile.TemporaryDirectory() as directory:
            store = CandleStore(Broker(), directory)
            with self.assertRaisesRegex(ValueError, "Too many requests"):
                store._fetch(
                    "1",
                    "ONE_MINUTE",
                    "2026-01-05 09:15",
                    "2026-01-20 15:30",
                    retry_ambiguous_rate_limit=False,
                )


if __name__ == "__main__":
    unittest.main()

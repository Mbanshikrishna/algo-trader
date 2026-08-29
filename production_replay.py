"""Replay production decision snapshots and calibrate execution slippage."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from execution.entry_validator import (
    MIN_RANGE_POSITION,
    MIN_VOLUME_RATIO,
    _check_quote,
)
from monitor.risk_state import calculate_position_size
from strategy.market_scanner import (
    INDIA_VIX_TOKEN,
    NIFTY_50_TOKEN,
    NIFTYBEES_TOKEN,
    score_candidate_quote,
)


@dataclass(frozen=True)
class ReplayComparison:
    sequence: int
    event_type: str
    symbol: str
    recorded_decision: str
    replayed_decision: str
    matched: bool
    detail: str = ""


def _rows(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT sequence, recorded_at_utc, event_type, symbol, decision,
               reason, payload_json
        FROM decision_events
        WHERE run_id = ?
        ORDER BY sequence
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "sequence": row[0],
            "recorded_at_utc": row[1],
            "event_type": row[2],
            "symbol": row[3],
            "decision": row[4],
            "reason": row[5],
            "payload": json.loads(row[6]),
        }
        for row in rows
    ]


def latest_run_id(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT run_id FROM runs ORDER BY started_at_utc DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise ValueError("decision journal contains no runs")
    return str(row[0])


def _quote_token(quote: dict[str, Any]) -> str:
    return str(quote.get("symbolToken") or quote.get("_token") or "")


def _replay_market_gate(payload: dict[str, Any]) -> str:
    if payload.get("result", {}).get("is_holiday"):
        return "skip"
    market = {_quote_token(q): q for q in payload.get("market_quotes", [])}
    nifty = market.get(NIFTY_50_TOKEN, {})
    vix = market.get(INDIA_VIX_TOKEN, {})
    bees = market.get(NIFTYBEES_TOKEN, {})
    if not nifty:
        return "skip"

    nifty_pct = float(nifty.get("percentChange", 0) or 0)
    if nifty_pct == 0:
        ltp = float(nifty.get("ltp", 0) or 0)
        close = float(nifty.get("close", 0) or 0)
        nifty_pct = ((ltp - close) / close * 100) if ltp > 0 and close > 0 else 0
    index_pass = nifty_pct > 0

    advancing = declining = 0
    for quote in payload.get("breadth_quotes", []):
        pct = float(quote.get("percentChange", 0) or 0)
        if pct == 0:
            ltp = float(quote.get("ltp", 0) or 0)
            close = float(quote.get("close", 0) or 0)
            pct = ((ltp - close) / close * 100) if ltp > 0 and close > 0 else 0
        advancing += pct > 0
        declining += pct < 0
    ratio = advancing / declining if declining else (99.0 if advancing else 0.0)
    breadth_pass = ratio > 1.2

    nifty_ltp = float(nifty.get("ltp", 0) or 0)
    nifty_open = float(nifty.get("open", 0) or 0)
    if nifty_ltp <= 0 or nifty_open <= 0:
        return "skip"
    bees_ltp = float(bees.get("ltp", 0) or 0)
    bees_vwap = float(bees.get("avgPrice", 0) or 0)
    strength_pass = (
        nifty_ltp > nifty_open
        and (bees_ltp > bees_vwap if bees_vwap > 0 else True)
    )
    volatility_pass = float(vix.get("percentChange", 0) or 0) < 5.0
    score = sum((index_pass, breadth_pass, strength_pass, volatility_pass))
    return "trade" if score >= 2 else "skip"


def _replay_scan(payload: dict[str, Any], decision_date: str) -> list[dict[str, Any]]:
    daily = payload.get("daily_candles", {})
    scored: list[dict[str, Any]] = []
    for quote in payload.get("quotes", []):
        token = _quote_token(quote)
        completed = [
            candle for candle in daily.get(token, [])
            if str(candle[0])[:10] < decision_date
        ]
        candidate = score_candidate_quote(quote, completed)
        if candidate is not None:
            scored.append(candidate)
    scored.sort(key=lambda item: item["composite_score"], reverse=True)
    return scored[: int(payload.get("top_n", 0))]


def _replay_validation(payload: dict[str, Any], symbol: str) -> str:
    quote = payload.get("quote", {})
    if not quote:
        return "rejected"
    result = _check_quote(symbol, quote)
    if not result.valid:
        return "rejected"

    captured = payload.get("candles", {})
    if captured.get("breakout_error") or captured.get("volume_error"):
        return "rejected"
    near_breakout = (
        result.range_position >= MIN_RANGE_POSITION
        and 5.0 <= result.gain_pct < 7.0
    )
    breakout = captured.get("breakout_candles", [])
    if not near_breakout and breakout:
        comparison_bar = breakout[-2] if len(breakout) >= 2 else breakout[0]
        if result.live_price < float(comparison_bar[2]):
            return "rejected"

    volume = captured.get("volume_candles", [])
    if len(volume) < 3:
        return "rejected"
    prior = [float(candle[5]) for candle in volume[:-1]]
    average = sum(prior) / len(prior) if prior else 0.0
    ratio = float(volume[-1][5]) / average if average > 0 else 0.0
    return "accepted" if ratio >= MIN_VOLUME_RATIO else "rejected"


def compare_run(
    database: str | Path,
    run_id: str | None = None,
) -> tuple[str, list[ReplayComparison]]:
    """Recompute deterministic decisions from recorded production inputs."""
    connection = sqlite3.connect(database)
    try:
        selected_run = run_id or latest_run_id(connection)
        comparisons: list[ReplayComparison] = []
        for event in _rows(connection, selected_run):
            event_type = event["event_type"]
            recorded = event["decision"]
            replayed = "recorded_only"
            detail = "event has no deterministic replay handler"
            if event_type == "market_gate":
                replayed = _replay_market_gate(event["payload"])
                detail = "four-factor market gate"
            elif event_type == "scan_completed":
                ranked = _replay_scan(
                    event["payload"], event["recorded_at_utc"][:10]
                )
                actual_symbols = [
                    item.get("symbol")
                    for item in event["payload"].get("ranked_candidates", [])
                ]
                replay_symbols = [item.get("symbol") for item in ranked]
                replayed = "candidates_ranked" if ranked else "no_candidates"
                detail = (
                    f"recorded={actual_symbols}; replayed={replay_symbols}"
                )
                if actual_symbols != replay_symbols:
                    replayed = f"{replayed}:ranking_mismatch"
            elif event_type == "candidate_validation":
                replayed = _replay_validation(event["payload"], event["symbol"])
                detail = event["reason"]
            elif event_type == "position_sized":
                sizing = event["payload"]
                replay_quantity = calculate_position_size(
                    capital=float(sizing["capital"]),
                    entry_price=float(sizing["entry_price"]),
                    stop_price=float(sizing["stop_price"]),
                    risk_per_trade_pct=float(sizing["risk_per_trade_pct"]),
                    maximum_notional=float(sizing["maximum_notional"]),
                )
                replayed = str(replay_quantity)
                recorded = str(sizing["quantity"])
                detail = "risk-based position sizing"
            elif event_type in {
                "tradability_filter",
                "broker_tradability_result",
            }:
                available = event["payload"].get("tradable_candidates", [])
                replayed = "candidates_available" if available else "none"
                detail = "captured filter outcome"
            elif event_type == "position_evaluated":
                position = event["payload"].get("position_after", {})
                price = float(event["payload"].get("stop_check_price", 0) or 0)
                average = float(position.get("average_price", 0) or 0)
                stop = float(position.get("stop_loss", 0) or 0)
                hard_stop = float(position.get("hard_stop", 0) or 0)
                target_hit = (
                    average > 0 and (price - average) / average >= 0.15
                )
                replayed = (
                    "exit"
                    if target_hit or price <= stop or price <= hard_stop
                    else "hold"
                )
                detail = "target and post-trail stop decision"
            else:
                continue
            comparisons.append(
                ReplayComparison(
                    sequence=event["sequence"],
                    event_type=event_type,
                    symbol=event["symbol"],
                    recorded_decision=recorded,
                    replayed_decision=replayed,
                    matched=recorded == replayed,
                    detail=detail,
                )
            )
        return selected_run, comparisons
    finally:
        connection.close()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def calibrate_slippage(
    database: str | Path,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Measure adverse slippage using only live broker-confirmed fills."""
    connection = sqlite3.connect(database)
    try:
        selected_run = run_id or latest_run_id(connection)
        mode_row = connection.execute(
            "SELECT mode FROM runs WHERE run_id = ?", (selected_run,)
        ).fetchone()
        mode = str(mode_row[0]).lower() if mode_row else "unknown"
        events = _rows(connection, selected_run) if mode == "live" else []
    finally:
        connection.close()

    observations: list[dict[str, Any]] = []
    for event in events:
        if event["event_type"] != "confirmed_fill":
            continue
        payload = event["payload"]
        reference = float(payload.get("reference_price", 0) or 0)
        fill = float(payload.get("fill_price", 0) or 0)
        quantity = int(payload.get("filled_quantity", 0) or 0)
        side = str(payload.get("side", "")).upper()
        if reference <= 0 or fill <= 0 or quantity <= 0 or side not in {"BUY", "SELL"}:
            continue
        adverse_bps = (
            (fill - reference) / reference * 10_000
            if side == "BUY"
            else (reference - fill) / reference * 10_000
        )
        observations.append(
            {
                "symbol": event["symbol"],
                "side": side,
                "quantity": quantity,
                "reference_price": reference,
                "fill_price": fill,
                "adverse_bps": round(adverse_bps, 4),
            }
        )

    values = [row["adverse_bps"] for row in observations]
    weighted_denominator = sum(row["quantity"] for row in observations)
    weighted_mean = (
        sum(row["adverse_bps"] * row["quantity"] for row in observations)
        / weighted_denominator
        if weighted_denominator
        else 0.0
    )
    p75 = _percentile(values, 0.75)
    return {
        "run_id": selected_run,
        "mode": mode,
        "confirmed_fills": len(observations),
        "weighted_mean_adverse_bps": round(weighted_mean, 4),
        "median_adverse_bps": round(_percentile(values, 0.5), 4),
        "p75_adverse_bps": round(p75, 4),
        "p90_adverse_bps": round(_percentile(values, 0.9), 4),
        "recommended_backtest_slippage_bps": max(0, math.ceil(p75)),
        "observations": observations,
    }


def export_universe_snapshots(database: str | Path, output: str | Path) -> Path:
    """Export dated universes in the format consumed by production_backtest.py."""
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            """
            SELECT trading_date, instruments_json
            FROM universe_snapshots
            ORDER BY trading_date, sequence
            """
        ).fetchall()
    finally:
        connection.close()
    snapshots: dict[str, Any] = {}
    for trading_date, instruments_json in rows:
        snapshots[str(trading_date)] = json.loads(instruments_json)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshots, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def write_replay_report(
    output_dir: str | Path,
    run_id: str,
    comparisons: list[ReplayComparison],
    slippage: dict[str, Any],
) -> dict[str, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    comparison_path = directory / "decision_comparison.csv"
    with comparison_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(ReplayComparison.__dataclass_fields__))
        writer.writeheader()
        for comparison in comparisons:
            writer.writerow(asdict(comparison))
    slippage_path = directory / "slippage_calibration.json"
    slippage_path.write_text(json.dumps(slippage, indent=2) + "\n", encoding="utf-8")
    summary_path = directory / "summary.json"
    matched = sum(comparison.matched for comparison in comparisons)
    summary_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "replayable_decisions": len(comparisons),
                "matched_decisions": matched,
                "match_rate_pct": round(
                    matched / len(comparisons) * 100, 2
                ) if comparisons else 0.0,
                "slippage": {
                    key: value for key, value in slippage.items()
                    if key != "observations"
                },
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return {
        "comparison": comparison_path.resolve(),
        "slippage": slippage_path.resolve(),
        "summary": summary_path.resolve(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/decision_journal.sqlite3")
    parser.add_argument("--run-id")
    parser.add_argument("--output-dir", default="data/replay_reports/latest")
    parser.add_argument("--export-universe", default="data/universe_snapshots.json")
    args = parser.parse_args()

    run_id, comparisons = compare_run(args.db, args.run_id)
    slippage = calibrate_slippage(args.db, run_id)
    paths = write_replay_report(args.output_dir, run_id, comparisons, slippage)
    universe = export_universe_snapshots(args.db, args.export_universe)
    matched = sum(comparison.matched for comparison in comparisons)
    print(f"Run: {run_id}")
    print(f"Decision matches: {matched}/{len(comparisons)}")
    print(
        "Recommended BACKTEST_SLIPPAGE_BPS: "
        f"{slippage['recommended_backtest_slippage_bps']}"
    )
    for name, path in paths.items():
        print(f"{name.title()}: {path}")
    print(f"Universe: {universe.resolve()}")


if __name__ == "__main__":
    main()

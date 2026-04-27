from __future__ import annotations

import argparse
import sys
import pandas as pd

from broker.angelone_client import AngelOneClient
from config.instruments import default_watchlist, watchlist_from_xlsx
from config.settings import load_settings
from data.market_stream import MarketStream
from strategy.momentum_strategy import MomentumStrategy


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan stocks for momentum signals.")
    parser.add_argument(
        "--excel-path",
        help="Optional xlsx file whose Equity sheet symbols should be scanned instead of the default watchlist.",
    )
    return parser.parse_args(argv)


def run_scanner(
    excel_path: str | None = None,
) -> tuple[list[dict], list[tuple[str, str]]]:
    settings = load_settings()
    stocks = watchlist_from_xlsx(excel_path) if excel_path else default_watchlist()
    broker = AngelOneClient.login(
        api_key=settings.api_key,
        client_id=settings.client_id,
        pin=settings.pin,
        totp_secret=settings.totp_secret,
    )
    stream = MarketStream(angel_client=broker, interval="5m", period="1d")
    strategy = MomentumStrategy()

    matches: list[dict] = []
    failures: list[tuple[str, str]] = []
    for instrument in stocks:
        try:
            resolved = stream.resolve_instrument(instrument)
            df = stream.fetch_ohlcv(resolved)
            signal = strategy.build_signal(instrument.symbol, df)
            if signal:
                matches.append({
                    "Stock": instrument.symbol,
                    "Price": signal["price"],
                    "StopLoss": signal["stop_loss"],
                })
        except Exception as exc:
            failures.append((instrument.symbol, str(exc)))
    return matches, failures


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    results, failures = run_scanner(excel_path=args.excel_path)
    if results:
        print("\nUptrend Intraday Stocks Found:\n")
        print(pd.DataFrame(results))
    else:
        print("\nNo strong uptrend stocks found currently.")
    if failures:
        print(f"\nSkipped {len(failures)} symbols due to lookup/data issues.")
        print(pd.DataFrame(failures, columns=["Stock", "Reason"]).head(20))

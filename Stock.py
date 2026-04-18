from __future__ import annotations  # Lets Python postpone evaluation of type annotations.

import argparse  # Imports argparse so the scanner can optionally load symbols from a workbook.
import sys  # Imports sys so command-line arguments can be passed into the scanner.
import pandas as pd  # Imports pandas to display scan results in tabular form.

from broker.angelone_client import AngelOneClient  # Imports the broker client used for Angel One market-data resolution.
from config.instruments import default_watchlist, watchlist_from_xlsx  # Imports the watchlist helpers used by the scanner.
from config.settings import load_settings  # Imports runtime settings so the scanner can pick the data provider.
from data.market_stream import MarketStream  # Imports the component that fetches stock candle data.
from strategy.momentum_strategy import MomentumStrategy  # Imports the strategy used to detect momentum signals.


def _parse_args(argv: list[str]) -> argparse.Namespace:  # Parses command-line options for choosing the stock source.
    parser = argparse.ArgumentParser(description="Scan stocks for momentum signals.")
    parser.add_argument(
        "--excel-path",
        help="Optional xlsx file whose Equity sheet symbols should be scanned instead of the default watchlist.",
    )
    parser.add_argument(
        "--provider",
        choices=("yfinance", "angelone"),
        help="Optional market-data provider override for this scan.",
    )
    return parser.parse_args(argv)


def run_scanner(
    excel_path: str | None = None,
    provider: str | None = None,
) -> tuple[list[dict], list[tuple[str, str]]]:  # Defines a helper that scans stocks and returns matching signals plus skipped symbols.
    settings = load_settings()  # Loads the runtime settings so the scanner can use the configured market data provider.
    stocks = watchlist_from_xlsx(excel_path) if excel_path else default_watchlist()  # Builds the stock list from the workbook when one is provided.
    data_provider = (provider or settings.market_data_provider).strip().lower()  # Lets one-off scans override the configured provider without editing .env.
    broker = AngelOneClient.login(  # Authenticates and builds the broker client with a fresh access token.
        api_key=settings.api_key,
        client_id=settings.client_id,
        pin=settings.pin,
        totp_secret=settings.totp_secret,
    )
    stream = MarketStream(  # Configures the data stream for 5-minute candles over one day.
        interval="5m",
        period="1d",
        data_provider=data_provider,
        angel_client=broker,
    )
    strategy = MomentumStrategy()  # Creates the strategy instance that will generate signals.

    matches: list[dict] = []  # Prepares a list to store stocks that meet the strategy criteria.
    failures: list[tuple[str, str]] = []  # Tracks symbols that could not be resolved or downloaded so one bad ticker does not stop the scan.
    for instrument in stocks:  # Loops through each stock instrument.
        try:
            resolved_instrument = stream.resolve_instrument(instrument) if data_provider == "angelone" else instrument  # Resolves Angel One tradingsymbol/token metadata when required.
            df = stream.fetch_ohlcv(resolved_instrument)  # Fetches OHLCV data for the current instrument.
            signal = strategy.build_signal(instrument.symbol, df)  # Builds a signal for the current symbol from the fetched data.
            if signal:  # Checks whether the strategy found a valid setup.
                matches.append(  # Adds the matching stock details to the result list.
                    {
                        "Stock": instrument.symbol,  # Stores the stock symbol for display.
                        "Price": signal["price"],  # Stores the signal price for display.
                        "StopLoss": signal["stop_loss"],  # Stores the stop-loss suggested by the strategy.
                    }
                )  # Finishes adding one stock match.
        except Exception as exc:
            failures.append((instrument.symbol, str(exc)))
    return matches, failures  # Returns all matching stock setups plus skipped symbols.


if __name__ == "__main__":  # Runs the scanner only when this file is executed directly.
    args = _parse_args(sys.argv[1:])  # Parses command-line arguments before running the scan.
    results, failures = run_scanner(excel_path=args.excel_path, provider=args.provider)  # Executes the scan and stores both matches and skipped symbols.
    if results:  # Checks whether any stocks matched the strategy.
        print("\nUptrend Intraday Stocks Found:\n")  # Prints a heading before the result table.
        print(pd.DataFrame(results))  # Displays the matches as a pandas DataFrame.
    else:  # Handles the case where no stocks matched.
        print("\nNo strong uptrend stocks found currently.")  # Prints a fallback message when there are no matches.
    if failures:
        print(f"\nSkipped {len(failures)} symbols due to lookup/data issues.")
        print(pd.DataFrame(failures, columns=["Stock", "Reason"]).head(20))

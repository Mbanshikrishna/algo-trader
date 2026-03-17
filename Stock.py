from __future__ import annotations  # Lets Python postpone evaluation of type annotations.

import pandas as pd  # Imports pandas to display scan results in tabular form.

from data.market_stream import MarketStream  # Imports the component that fetches stock candle data.
from strategy.momentum_strategy import MomentumStrategy  # Imports the strategy used to detect momentum signals.

STOCKS = [  # Defines the list of symbols the scanner will check.
    "RELIANCE.NS",  # Adds Reliance to the scan list.
    "HDFCBANK.NS",  # Adds HDFC Bank to the scan list.
    "ICICIBANK.NS",  # Adds ICICI Bank to the scan list.
    "INFY.NS",  # Adds Infosys to the scan list.
    "TCS.NS",  # Adds Tata Consultancy Services to the scan list.
    "SBIN.NS",  # Adds State Bank of India to the scan list.
    "AXISBANK.NS",  # Adds Axis Bank to the scan list.
    "ITC.NS",  # Adds ITC to the scan list.
    "LT.NS",  # Adds Larsen & Toubro to the scan list.
    "JUBLFOOD.NS",  # Adds Jubilant FoodWorks to the scan list.
]  # Ends the stock symbol list.


def run_scanner() -> list[dict]:  # Defines a helper that scans stocks and returns matching signals.
    stream = MarketStream(interval="5m", period="1d")  # Configures the data stream for 5-minute candles over one day.
    strategy = MomentumStrategy()  # Creates the strategy instance that will generate signals.

    matches: list[dict] = []  # Prepares a list to store stocks that meet the strategy criteria.
    for symbol in STOCKS:  # Loops through each stock symbol in the scan list.
        df = stream.fetch_ohlcv(symbol)  # Fetches OHLCV data for the current symbol.
        signal = strategy.build_signal(symbol, df)  # Builds a signal for the current symbol from the fetched data.
        if signal:  # Checks whether the strategy found a valid setup.
            matches.append(  # Adds the matching stock details to the result list.
                {
                    "Stock": symbol,  # Stores the stock symbol for display.
                    "Price": signal["price"],  # Stores the signal price for display.
                    "StopLoss": signal["stop_loss"],  # Stores the stop-loss suggested by the strategy.
                }
            )  # Finishes adding one stock match.
    return matches  # Returns all matching stock setups.


if __name__ == "__main__":  # Runs the scanner only when this file is executed directly.
    results = run_scanner()  # Executes the scanner and stores the result list.
    if results:  # Checks whether any stocks matched the strategy.
        print("\nUptrend Intraday Stocks Found:\n")  # Prints a heading before the result table.
        print(pd.DataFrame(results))  # Displays the matches as a pandas DataFrame.
    else:  # Handles the case where no stocks matched.
        print("\nNo strong uptrend stocks found currently.")  # Prints a fallback message when there are no matches.

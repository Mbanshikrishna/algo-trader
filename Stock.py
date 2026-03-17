from __future__ import annotations

import pandas as pd

from data.market_stream import MarketStream
from strategy.momentum_strategy import MomentumStrategy

STOCKS = [
    "RELIANCE.NS",
    "HDFCBANK.NS",
    "ICICIBANK.NS",
    "INFY.NS",
    "TCS.NS",
    "SBIN.NS",
    "AXISBANK.NS",
    "ITC.NS",
    "LT.NS",
    "JUBLFOOD.NS",
]


def run_scanner() -> list[dict]:
    stream = MarketStream(interval="5m", period="1d")
    strategy = MomentumStrategy()

    matches: list[dict] = []
    for symbol in STOCKS:
        df = stream.fetch_ohlcv(symbol)
        signal = strategy.build_signal(symbol, df)
        if signal:
            matches.append(
                {
                    "Stock": symbol,
                    "Price": signal["price"],
                    "StopLoss": signal["stop_loss"],
                }
            )
    return matches


if __name__ == "__main__":
    results = run_scanner()
    if results:
        print("\n🔥 Uptrend Intraday Stocks Found:\n")
        print(pd.DataFrame(results))
    else:
        print("\nNo strong uptrend stocks found currently.")

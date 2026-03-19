from __future__ import annotations  # Lets Python treat type hints as postponed string annotations.

import time  # Imports time utilities for repeated scan delays.

from broker.angelone_client import AngelOneClient  # Imports the Angel One broker client wrapper.
from config.settings import load_settings  # Imports the function that reads app configuration values.
from data.market_stream import MarketStream  # Imports the market data fetcher for OHLCV candles.
from db.trade_db import TradeDB  # Imports the database helper used to store executed trades.
from execution.order_manager import OrderManager  # Imports the order execution layer.
from monitor.position_tracker import PositionTracker  # Imports the tracker for open positions.
from risk.risk_manager import RiskManager  # Imports the risk management component for sizing trades.
from strategy.momentum_strategy import MomentumStrategy  # Imports the strategy that creates buy/sell signals.
from utils.logger import setup_logger  # Imports the logger setup function.
from utils.telegram_alert import send_telegram_message  # Imports the Telegram notifier.

WATCHLIST = [  # Defines the symbols the bot will scan in each run.
    "RELIANCE.NS",  # Adds Reliance to the watchlist.
    "HDFCBANK.NS",  # Adds HDFC Bank to the watchlist.
    "ICICIBANK.NS",  # Adds ICICI Bank to the watchlist.
    "INFY.NS",  # Adds Infosys to the watchlist.
    "TCS.NS",  # Adds Tata Consultancy Services to the watchlist.
    "SBIN.NS",  # Adds State Bank of India to the watchlist.
    "AXISBANK.NS",  # Adds Axis Bank to the watchlist.
    "ITC.NS",  # Adds ITC to the watchlist.
    "LT.NS",  # Adds Larsen & Toubro to the watchlist.
    "JUBLFOOD.NS",  # Adds Jubilant FoodWorks to the watchlist.
]  # Ends the list of tracked symbols.


def _notify_status(message: str, logger: object, send_alert: bool) -> None:  # Logs a status update and optionally sends it to Telegram.
    logger.info(message)  # Writes the status message to the logs.
    if send_alert:  # Checks whether this status should also be sent to Telegram.
        send_telegram_message(message)  # Sends the status message to Telegram when enabled.


def run_once() -> None:  # Defines a repeated trading cycle across the watchlist.
    settings = load_settings()  # Loads runtime settings such as API keys and risk parameters.
    logger = setup_logger()  # Creates a configured logger for console/file logging.

    if not settings.paper_trade and not (settings.api_key and settings.client_id and settings.access_token):  # Validates credentials when live trading is enabled.
        raise ValueError("Live trading requires ANGELONE_API_KEY, ANGELONE_CLIENT_ID, and ANGELONE_ACCESS_TOKEN")  # Stops execution if live credentials are missing.

    broker = AngelOneClient(  # Builds the broker client used to place real or simulated orders.
        api_key=settings.api_key,  # Passes the configured API key into the broker client.
        client_id=settings.client_id,  # Passes the configured client identifier into the broker client.
        access_token=settings.access_token,  # Passes the current access token into the broker client.
    )  # Finishes broker client initialization.
    order_manager = OrderManager(broker_client=broker, paper_trade=settings.paper_trade)  # Creates the order manager around the broker client.
    risk_manager = RiskManager(capital=settings.capital, risk_per_trade_pct=settings.risk_per_trade_pct)  # Creates the risk manager with capital and risk settings.
    strategy = MomentumStrategy()  # Instantiates the momentum-based signal generator.
    stream = MarketStream(interval="5m", period="1d")  # Configures market data to use 5-minute candles from the last day.
    trade_db = TradeDB()  # Opens the trade logging database helper.
    position_tracker = PositionTracker()  # Starts the in-memory tracker for current positions.

    while True:  # Keeps scanning the watchlist on a repeating interval.
        for symbol in WATCHLIST:  # Loops through each stock in the watchlist.
            try:  # Wraps each symbol so one failure does not stop the whole scan.
                df = stream.fetch_ohlcv(symbol)  # Downloads recent OHLCV data for the current symbol.
                signal = strategy.build_signal(symbol, df)  # Generates a trading signal from the fetched data.
                if not signal:  # Checks whether the strategy found a valid setup.
                    _notify_status(f"No trade executed for {symbol}: no valid signal.", logger, settings.alert_every_check)  # Reports skipped symbols as explicit status updates.
                    time.sleep(settings.scan_interval_seconds)  # Waits before the next alert cycle.
                    continue  # Skips this symbol when there is no trade signal.

                qty = risk_manager.position_size(signal["price"], signal["stop_loss"])  # Calculates position size from entry and stop-loss.
                if qty <= 0:  # Guards against invalid or zero quantity recommendations.
                    _notify_status(f"No trade executed for {symbol}: calculated quantity was not positive.", logger, settings.alert_every_check)  # Reports invalid quantities as explicit status updates.
                    time.sleep(settings.scan_interval_seconds)  # Waits before the next alert cycle.
                    continue  # Moves to the next symbol without placing an order.

                order = order_manager.place_market_order(symbol, signal["side"], qty)  # Sends the market order using the chosen side and quantity.
                trade_db.log_trade(symbol, signal["side"], qty, signal["price"], order["status"])  # Stores the trade outcome in the database.
                position_tracker.update_buy(symbol, qty, signal["price"])  # Updates the tracked position after execution.

                _notify_status(f"Signal executed: {order}", logger, True)  # Reports executed orders to both logs and Telegram.
            except Exception as exc:  # Catches any error raised while processing one symbol.
                logger.exception("Error processing %s: %s", symbol, exc)  # Logs the symbol-level error with traceback details.
                if settings.alert_every_check:  # Checks whether errors should also be sent to Telegram.
                    send_telegram_message(f"Error processing {symbol}: {exc}")  # Sends the symbol-level error summary to Telegram.
            time.sleep(settings.scan_interval_seconds)  # Waits between symbol checks so alerts arrive at the requested interval.

        _notify_status(f"Open positions: {position_tracker.snapshot()}", logger, settings.alert_every_check)  # Logs the latest open-position snapshot after each full watchlist cycle.


if __name__ == "__main__":  # Runs the trading cycle only when this file is executed directly.
    run_once()  # Starts one end-to-end pass through the watchlist.

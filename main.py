from __future__ import annotations

from broker.kite_client import KiteClient
from config.settings import load_settings
from data.market_stream import MarketStream
from db.trade_db import TradeDB
from execution.order_manager import OrderManager
from monitor.position_tracker import PositionTracker
from risk.risk_manager import RiskManager
from strategy.momentum_strategy import MomentumStrategy
from utils.logger import setup_logger
from utils.telegram_alert import send_telegram_message

WATCHLIST = [
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


def run_once() -> None:
    settings = load_settings()
    logger = setup_logger()

    if not settings.paper_trade and not (settings.api_key and settings.api_secret and settings.access_token):
        raise ValueError("Live trading requires ZERODHA_API_KEY, ZERODHA_API_SECRET, and ZERODHA_ACCESS_TOKEN")

    broker = KiteClient(
        api_key=settings.api_key,
        api_secret=settings.api_secret,
        access_token=settings.access_token,
    )
    order_manager = OrderManager(broker_client=broker, paper_trade=settings.paper_trade)
    risk_manager = RiskManager(capital=settings.capital, risk_per_trade_pct=settings.risk_per_trade_pct)
    strategy = MomentumStrategy()
    stream = MarketStream(interval="5m", period="1d")
    trade_db = TradeDB()
    position_tracker = PositionTracker()

    for symbol in WATCHLIST:
        try:
            df = stream.fetch_ohlcv(symbol)
            signal = strategy.build_signal(symbol, df)
            if not signal:
                continue

            qty = risk_manager.position_size(signal["price"], signal["stop_loss"])
            if qty <= 0:
                logger.info("Skipping %s due to non-positive quantity", symbol)
                continue

            order = order_manager.place_market_order(symbol, signal["side"], qty)
            trade_db.log_trade(symbol, signal["side"], qty, signal["price"], order["status"])
            position_tracker.update_buy(symbol, qty, signal["price"])

            message = f"Signal executed: {order}"
            logger.info(message)
            send_telegram_message(message)
        except Exception as exc:
            logger.exception("Error processing %s: %s", symbol, exc)

    logger.info("Open positions: %s", position_tracker.snapshot())


if __name__ == "__main__":
    run_once()

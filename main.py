from __future__ import annotations  # Lets Python treat type hints as postponed string annotations.

import time  # Imports time utilities for repeated scan delays.

from broker.angelone_client import AngelOneClient  # Imports the Angel One broker client wrapper.
from config.instruments import default_watchlist  # Imports the default instrument watchlist.
from config.settings import load_settings  # Imports the function that reads app configuration values.
from data.market_stream import MarketStream  # Imports the market data fetcher for OHLCV candles.
from execution.order_manager import OrderManager  # Imports the order execution layer.
from monitor.position_tracker import PositionTracker  # Imports the tracker for open positions.
from risk.risk_manager import RiskManager  # Imports the risk management component for sizing trades.
from strategy.momentum_strategy import MomentumStrategy  # Imports the strategy that creates buy/sell signals.
from utils.logger import setup_logger  # Imports the logger setup function.
from utils.telegram_alert import send_telegram_message  # Imports the Telegram notifier.

WATCHLIST = default_watchlist()  # Defines the instruments the bot will scan in each run.


def _notify_status(message: str, logger: object, send_alert: bool) -> None:  # Logs a status update and optionally sends it to Telegram.
    logger.info(message)  # Writes the status message to the logs.
    if send_alert:  # Checks whether this status should also be sent to Telegram.
        send_telegram_message(message)  # Sends the status message to Telegram when enabled.


def run_loop() -> None:  # Runs the trading loop continuously across the watchlist.
    settings = load_settings()  # Loads runtime settings such as API keys and risk parameters.
    logger = setup_logger()  # Creates a configured logger for console/file logging.

    if not (settings.api_key and settings.client_id and settings.pin and settings.totp_secret):  # Validates credentials before any live broker access begins.
        raise ValueError("Live trading requires ANGELONE_API_KEY, ANGELONE_CLIENT_ID, ANGELONE_PIN, and ANGELONE_TOTP_SECRET")  # Stops execution if live credentials are missing.

    logger.info("Logging in to Angel One...")  # Logs the login attempt.
    broker = AngelOneClient.login(  # Authenticates and builds the broker client with a fresh access token.
        api_key=settings.api_key,
        client_id=settings.client_id,
        pin=settings.pin,
        totp_secret=settings.totp_secret,
    )
    logger.info("Angel One login successful.")  # Confirms the login succeeded.
    order_manager = OrderManager(  # Creates the order manager around the broker client.
        broker_client=broker,
        product_type=settings.order_product_type,
        variety=settings.order_variety,
    )
    risk_manager = RiskManager(capital=settings.capital, risk_per_trade_pct=settings.risk_per_trade_pct)  # Creates the risk manager with capital and risk settings.
    strategy = MomentumStrategy()  # Instantiates the momentum-based signal generator.
    stream = MarketStream(  # Configures market data to use the selected provider.
        interval="5m",
        period="1d",
        data_provider=settings.market_data_provider,
        angel_client=broker,
    )
    position_tracker = PositionTracker()  # Starts the in-memory tracker for current positions.

    # Cache resolved broker instruments so trailing-stop exits can reuse them.
    resolved_instruments: dict[str, object] = {}

    # Pre-resolve all instruments once at startup.
    for instrument in WATCHLIST:
        try:
            resolved_instruments[instrument.symbol] = stream.resolve_instrument(instrument)
        except Exception as exc:
            logger.warning("Could not resolve %s at startup: %s", instrument.symbol, exc)

    while True:  # Keeps scanning the watchlist on a repeating interval.
        scan_start = time.perf_counter()

        # --- Batch fetch: download all symbols in one call ---
        try:
            ohlcv_batch = stream.fetch_ohlcv_batch(WATCHLIST)
        except Exception as exc:
            logger.exception("Batch fetch failed: %s", exc)
            time.sleep(settings.scan_interval_seconds)
            continue

        for instrument in WATCHLIST:  # Loops through each stock instrument in the watchlist.
            try:  # Wraps each symbol so one failure does not stop the whole scan.
                broker_instrument = resolved_instruments.get(instrument.symbol)
                if broker_instrument is None:
                    broker_instrument = stream.resolve_instrument(instrument)
                    resolved_instruments[instrument.symbol] = broker_instrument

                df = ohlcv_batch.get(instrument.symbol, None)
                if df is None or df.empty:
                    continue

                current_price = float(df.iloc[-1]["Close"])  # Extracts the latest close price for trailing stop checks.

                # --- Trailing stop: update and check exit for open positions ---
                existing_position = position_tracker.snapshot().get(instrument.symbol)
                previous_stop_loss = existing_position.stop_loss if existing_position is not None else None
                position = position_tracker.update_trailing_stop(instrument.symbol, current_price)
                if position is not None:
                    if position_tracker.should_exit(instrument.symbol, current_price):
                        logger.info(
                            "TRAILING STOP EXIT %s: price=%.2f breached stop_loss=%.2f (entry=%.2f, high=%.2f)",
                            instrument.symbol, current_price, position.stop_loss,
                            position.average_price, position.highest_price,
                        )
                        exit_order = order_manager.place_exit_order(
                            instrument.symbol, position.quantity, instrument=broker_instrument,
                        )
                        position_tracker.update_sell(instrument.symbol, position.quantity)
                        _notify_status(
                            f"Trailing stop exit: {instrument.symbol} @ {current_price:.2f} "
                            f"(SL={position.stop_loss:.2f}, entry={position.average_price:.2f}, "
                            f"high={position.highest_price:.2f}) | {exit_order}",
                            logger, True,
                        )
                        continue  # Position closed; skip new entry for this symbol.
                    elif previous_stop_loss is not None and position.stop_loss > previous_stop_loss:
                        logger.info(
                            "TRAILING STOP UPDATE %s: SL %.2f -> %.2f (price=%.2f, high=%.2f)",
                            instrument.symbol, previous_stop_loss, position.stop_loss,
                            current_price, position.highest_price,
                        )

                # --- Entry logic: only enter if no open position for this symbol ---
                if instrument.symbol in position_tracker.snapshot():
                    continue  # Already holding this symbol; skip new entry.

                signal = strategy.build_signal(instrument.symbol, df)  # Generates a trading signal from the fetched data.
                if not signal:  # Checks whether the strategy found a valid setup.
                    _notify_status(f"No trade executed for {instrument.symbol}: no valid signal.", logger, settings.alert_every_check)
                    continue  # Skips this symbol when there is no trade signal.

                qty = risk_manager.position_size(signal["price"], signal["stop_loss"])  # Calculates position size from entry and stop-loss.
                if qty <= 0:  # Guards against invalid or zero quantity recommendations.
                    _notify_status(f"No trade executed for {instrument.symbol}: calculated quantity was not positive.", logger, settings.alert_every_check)
                    continue  # Moves to the next symbol without placing an order.

                order = order_manager.place_market_order(  # Sends the market order using the chosen side and quantity.
                    instrument.symbol,
                    signal["side"],
                    qty,
                    instrument=broker_instrument,
                )
                if signal["side"].upper() == "BUY":  # Updates the tracked position using the correct method for the signal direction.
                    position_tracker.update_buy(instrument.symbol, qty, signal["price"])
                else:
                    position_tracker.update_sell(instrument.symbol, qty)

                _notify_status(f"Signal executed: {order}", logger, True)  # Reports executed orders to both logs and Telegram.
            except Exception as exc:  # Catches any error raised while processing one symbol.
                logger.exception("Error processing %s: %s", instrument.symbol, exc)  # Logs the symbol-level error with traceback details.
                if settings.alert_every_check:  # Checks whether errors should also be sent to Telegram.
                    send_telegram_message(f"Error processing {instrument.symbol}: {exc}")  # Sends the symbol-level error summary to Telegram.

        scan_duration = time.perf_counter() - scan_start
        logger.info("Scan completed in %.3f seconds", scan_duration)
        _notify_status(f"Open positions: {position_tracker.snapshot()}", logger, settings.alert_every_check)  # Logs the latest open-position snapshot after each full watchlist cycle.
        time.sleep(settings.scan_interval_seconds)  # Waits between full scan cycles.


if __name__ == "__main__":  # Runs the trading cycle only when this file is executed directly.
    run_loop()  # Starts the continuous trading loop.

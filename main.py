from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from broker.angelone_client import AngelOneClient
from config.instruments import Instrument
from config.settings import load_settings
from execution.entry_validator import validate_entries_batch
from execution.order_manager import OrderManager, OrderRejectedError
from execution.tradability_filter import TradabilityFilter
from monitor.position_tracker import PositionTracker
from strategy.market_scanner import is_market_bullish, load_nse_equity_tokens, scan_top_gainers
from utils.logger import setup_logger
from utils.telegram_alert import send_telegram_message

IST = ZoneInfo("Asia/Kolkata")

# Market timing constants.
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 15
SCAN_HOUR, SCAN_MIN = 10, 0  # Scan for top gainers at 10:00 AM IST.
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 15, 15  # Exit all positions by 3:15 PM.
TOP_N = 2  # Number of top gainers to trade.
MAX_REENTRY_ROUNDS = 3  # Max times the bot can re-scan and re-enter after all positions close.
REENTRY_COOLDOWN_MINUTES = 15  # Wait this long after last exit before re-scanning.


def _notify(message: str, logger, send_alert: bool) -> None:
    logger.info(message)
    if send_alert:
        send_telegram_message(message)


def _place_slm_order(
    broker: AngelOneClient,
    symbol: str,
    token: str,
    quantity: int,
    trigger_price: float,
    logger,
) -> str | None:
    """Place a SL-M (Stop-Loss Market) SELL order on the exchange.

    This order sits on the exchange and triggers automatically when the price
    drops to the trigger level — no polling needed.
    Returns the order ID, or None if placement fails.
    """
    payload = {
        "variety": "STOPLOSS",
        "tradingsymbol": symbol,
        "symboltoken": str(token),
        "transactiontype": "SELL",
        "exchange": "NSE",
        "ordertype": "STOPLOSS_MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "quantity": str(quantity),
        "triggerprice": str(round(trigger_price, 2)),
        "squareoff": "0",
        "stoploss": "0",
        "price": "0",
    }
    try:
        result = broker.place_order(payload)
        resp = result.get("response", {})
        data = resp.get("data", {})
        order_id = data.get("orderid") if isinstance(data, dict) else str(data) if data else None
        if order_id:
            logger.info("SL-M order placed for %s: trigger=%.2f, orderid=%s", symbol, trigger_price, order_id)
        return order_id
    except Exception as exc:
        logger.warning("Failed to place SL-M order for %s: %s", symbol, exc)
        return None


def _update_slm_trigger(
    broker: AngelOneClient,
    order_id: str,
    symbol: str,
    token: str,
    quantity: int,
    new_trigger: float,
    logger,
) -> bool:
    """Modify an existing SL-M order's trigger price (trail it up)."""
    payload = {
        "variety": "STOPLOSS",
        "orderid": order_id,
        "tradingsymbol": symbol,
        "symboltoken": str(token),
        "transactiontype": "SELL",
        "exchange": "NSE",
        "ordertype": "STOPLOSS_MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "quantity": str(quantity),
        "triggerprice": str(round(new_trigger, 2)),
        "price": "0",
    }
    try:
        broker.modify_order(payload)
        logger.info("SL-M trigger updated for %s: new trigger=%.2f", symbol, new_trigger)
        return True
    except Exception as exc:
        logger.warning("Failed to update SL-M for %s: %s", symbol, exc)
        return False


def _cancel_slm_order(broker: AngelOneClient, order_id: str, symbol: str, logger) -> None:
    """Cancel an existing SL-M order before placing a software exit."""
    try:
        broker.cancel_order(order_id, "STOPLOSS")
        logger.info("SL-M order cancelled for %s: orderid=%s", symbol, order_id)
    except Exception as exc:
        logger.warning("Failed to cancel SL-M for %s (may have already triggered): %s", symbol, exc)


def _ist_now() -> datetime:
    return datetime.now(IST)


def _wait_until(hour: int, minute: int, logger) -> None:
    """Sleep until the target IST time. Returns immediately if already past."""
    while True:
        now = _ist_now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            return
        logger.info("Waiting until %02d:%02d IST (%.0f seconds)...", hour, minute, remaining)
        time.sleep(min(remaining, 30))  # Wake up every 30s to log progress.


def _allocate_capital(broker: AngelOneClient, settings, logger) -> float:
    """Fetch available capital from the broker account, fall back to config."""
    try:
        available = broker.get_available_capital()
        if available > 0:
            logger.info("Available capital from broker: %.2f", available)
            return available
    except Exception as exc:
        logger.warning("Could not fetch capital from broker: %s", exc)

    logger.info("Using configured capital: %.2f", settings.capital)
    return settings.capital


def run_loop() -> None:
    settings = load_settings()
    logger = setup_logger()

    if not (settings.api_key and settings.client_id and settings.pin and settings.totp_secret):
        raise ValueError(
            "Live trading requires ANGELONE_API_KEY, ANGELONE_CLIENT_ID, ANGELONE_PIN, and ANGELONE_TOTP_SECRET"
        )

    logger.info("Logging in to Angel One...")
    broker = AngelOneClient.login(
        api_key=settings.api_key,
        client_id=settings.client_id,
        pin=settings.pin,
        totp_secret=settings.totp_secret,
    )
    logger.info("Angel One login successful.")

    # Initialize tradability filter and load restricted stock lists.
    trad_filter = TradabilityFilter(safe_mode=settings.safe_mode)
    logger.info("Loading ASM/GSM/F&O restricted lists from NSE...")
    trad_filter.load_restricted_lists()

    order_manager = OrderManager(
        broker_client=broker,
        product_type=settings.order_product_type,
        variety=settings.order_variety,
        tradability_filter=trad_filter,
    )
    position_tracker = PositionTracker()

    # Load all NSE equity tokens once at startup.
    logger.info("Loading NSE equity scrip master...")
    nse_stocks = load_nse_equity_tokens(broker)
    logger.info("Loaded %d NSE equity stocks.", len(nse_stocks))

    while True:
        now = _ist_now()

        # Only run on weekdays.
        if now.weekday() >= 5:
            logger.info("Weekend — sleeping until Monday.")
            time.sleep(3600)
            continue

        # --- Phase 1: Wait for 10:00 AM scan window ---
        _wait_until(SCAN_HOUR, SCAN_MIN, logger)

        now = _ist_now()
        if now.hour >= MARKET_CLOSE_HOUR and now.minute >= MARKET_CLOSE_MIN:
            logger.info("Market closed for today. Sleeping until tomorrow.")
            time.sleep(3600)
            continue

        # --- Phase 2: Check if market is bullish (4-factor model) ---
        bullish, market_check = is_market_bullish(broker)
        report = market_check.format_report()
        _notify(report, logger, True)

        if not bullish:
            time.sleep(3600)
            continue

        consecutive_losses = 0  # Tracks consecutive losing trades across all rounds.
        traded_today: set[str] = set()  # All stocks traded today — never re-enter the same stock.
        reentry_round = 0

        # === Re-entry loop: scan → enter → monitor, up to MAX_REENTRY_ROUNDS ===
        while reentry_round <= MAX_REENTRY_ROUNDS:
            now = _ist_now()
            if now.hour > MARKET_CLOSE_HOUR or (now.hour == MARKET_CLOSE_HOUR and now.minute >= MARKET_CLOSE_MIN):
                break
            if consecutive_losses >= settings.max_consecutive_losses:
                _notify(
                    f"Stopping for the day: {consecutive_losses} consecutive losses.",
                    logger, True,
                )
                break

            if reentry_round > 0:
                _notify(
                    f"RE-ENTRY round {reentry_round}/{MAX_REENTRY_ROUNDS} — "
                    f"cooling down {REENTRY_COOLDOWN_MINUTES} minutes before re-scanning...",
                    logger, True,
                )
                time.sleep(REENTRY_COOLDOWN_MINUTES * 60)

                # Check market is still open after cooldown.
                now = _ist_now()
                if now.hour > MARKET_CLOSE_HOUR or (now.hour == MARKET_CLOSE_HOUR and now.minute >= MARKET_CLOSE_MIN):
                    break

                # Re-check if market is still bullish (full 4-factor model).
                bullish, market_check = is_market_bullish(broker)
                report = f"Re-entry {market_check.format_report()}"
                _notify(report, logger, True)
                if not bullish:
                    break

            # --- Phase 3: Scan all NSE stocks for top gainers ---
            scan_pool_size = TOP_N * 5
            round_label = f"[Round {reentry_round}] " if reentry_round > 0 else ""
            logger.info("%sScanning %d stocks for top %d candidates (pool=%d)...",
                        round_label, len(nse_stocks), TOP_N, scan_pool_size)
            scan_start = time.perf_counter()
            raw_candidates = scan_top_gainers(broker, nse_stocks, top_n=scan_pool_size)
            scan_duration = time.perf_counter() - scan_start
            logger.info("Scan completed in %.1f seconds. Found %d raw candidates.", scan_duration, len(raw_candidates))

            if not raw_candidates:
                _notify(f"{round_label}No qualifying gainers found.", logger, True)
                break

            # --- Phase 3b: Filter for tradability (ASM/GSM/circuit limits) ---
            tradable, skipped = trad_filter.filter_candidates(
                raw_candidates, fno_only=settings.fno_only,
            )

            if skipped:
                skip_msg = "Skipped by ASM/GSM/circuit filter:\n" + "\n".join(
                    f"  {sym}: {reason}" for sym, reason in skipped
                )
                logger.info(skip_msg)

            if not tradable:
                _notify(
                    f"{round_label}No tradable stocks after ASM/GSM filtering ({len(skipped)} skipped).",
                    logger, True,
                )
                break

            # --- Phase 3c: Probe broker tradability (catches Angel One cautionary list) ---
            logger.info("Probing %d candidates for broker tradability...", len(tradable))
            probe_start = time.perf_counter()
            probed_tradable, probe_skipped = trad_filter.probe_candidates(
                broker, tradable, max_workers=4,
            )
            probe_duration = time.perf_counter() - probe_start
            logger.info(
                "Probe completed in %.1f seconds: %d tradable, %d rejected.",
                probe_duration, len(probed_tradable), len(probe_skipped),
            )

            if probe_skipped:
                probe_msg = "Skipped by broker probe (cautionary):\n" + "\n".join(
                    f"  {sym}: {reason}" for sym, reason in probe_skipped
                )
                _notify(probe_msg, logger, True)

            # Exclude stocks that already hit stop-loss today.
            if traded_today:
                probed_tradable = [c for c in probed_tradable if c["symbol"] not in traded_today]

            all_skipped = skipped + probe_skipped
            top_gainers = probed_tradable[:TOP_N]

            if not top_gainers:
                _notify(
                    f"{round_label}No tradable stocks after filtering "
                    f"({len(all_skipped)} skipped, {len(traded_today)} already traded today).",
                    logger, True,
                )
                break

            gainers_msg = (
                f"{round_label}Top {len(top_gainers)} tradable stocks selected "
                f"(from {len(raw_candidates)} scanned, {len(all_skipped)} filtered):\n"
                + "\n".join(
                    f"  {g['symbol']}: {g['pct_change']:+.2f}% @ {g['ltp']:.2f}\n"
                    f"    Score={g['composite_score']:.3f} | Vol={g['relative_volume']:.1f}x | "
                    f"Momentum={g['momentum_score']:.2f} | BuyPressure={g['buy_pressure']:.2f} | "
                    f"Stability={g['stability_score']:.2f} | PrevTrend={g['prev_day_score']:.2f}"
                    for g in top_gainers
                )
            )
            _notify(gainers_msg, logger, True)

            # --- Phase 4: Validate entries in real-time and allocate capital ---
            # Build fallback queue: top_gainers first, then remaining probed candidates.
            fallback_queue = list(top_gainers)
            for candidate in probed_tradable[TOP_N:]:
                if candidate not in fallback_queue:
                    fallback_queue.append(candidate)

            # Step 4a: Validate candidates with live data in parallel.
            # Filter out already-traded stocks before validation.
            validation_queue = [g for g in fallback_queue if g["symbol"] not in traded_today]

            logger.info(
                "%sValidating %d candidates in parallel...",
                round_label, len(validation_queue),
            )
            validation_start = time.perf_counter()
            validated_pairs = validate_entries_batch(
                validation_queue, broker, max_valid=TOP_N, max_workers=4,
            )
            validation_duration = time.perf_counter() - validation_start
            logger.info(
                "Validation completed in %.1f seconds: %d/%d passed.",
                validation_duration, len(validated_pairs), len(validation_queue),
            )

            # Extract validated candidates with live prices.
            validated: list[dict] = []
            for gainer, vr in validated_pairs:
                gainer["live_price"] = vr.live_price
                validated.append(gainer)

            # Notify rejections (from the validation log, not re-iterated here).
            if not validated:
                _notify(
                    f"{round_label}No candidates passed real-time validation. Skipping cycle.",
                    logger, True,
                )
                break

            # Step 4b: Flexible capital allocation.
            total_capital = _allocate_capital(broker, settings, logger)
            leveraged_capital = total_capital * settings.intraday_leverage

            if len(validated) == 1:
                # Single stock — use 50% of buying power (never 100% on one stock).
                capital_per_stock = leveraged_capital * 0.5
            else:
                # 2 stocks — split equally.
                capital_per_stock = leveraged_capital / len(validated)

            logger.info(
                "Capital: %.2f x %.1fx leverage = %.2f buying power | "
                "%d validated stocks | %.2f per stock",
                total_capital, settings.intraday_leverage, leveraged_capital,
                len(validated), capital_per_stock,
            )

            # Step 4c: Execute entries using real-time validated prices.
            entered_symbols: list[str] = []
            token_map: dict[str, str] = {}
            slm_orders: dict[str, str] = {}
            slm_triggers: dict[str, float] = {}

            for gainer in validated:
                symbol = gainer["symbol"]
                live_price = gainer["live_price"]

                try:
                    instrument = Instrument(
                        symbol=symbol,
                        exchange="NSE",
                        tradingsymbol=symbol,
                        symboltoken=gainer["token"],
                    )

                    qty = int(capital_per_stock // live_price)
                    if qty <= 0:
                        _notify(
                            f"Skipping {symbol}: live price {live_price:.2f} exceeds "
                            f"allocated capital {capital_per_stock:.2f}",
                            logger, True,
                        )
                        continue

                    order = order_manager.place_market_order(
                        symbol, "BUY", qty, instrument=instrument,
                        current_price=live_price,
                    )
                    pos = position_tracker.update_buy(symbol, qty, live_price)
                    entered_symbols.append(symbol)
                    traded_today.add(symbol)
                    token_map[symbol] = gainer["token"]

                    # Place server-side SL-M order at the hard stop.
                    slm_id = _place_slm_order(
                        broker, symbol, gainer["token"], qty, pos.hard_stop, logger,
                    )
                    if slm_id:
                        slm_orders[symbol] = slm_id
                        slm_triggers[symbol] = pos.hard_stop

                    _notify(
                        f"{round_label}ENTRY: {symbol} — {qty} shares @ {live_price:.2f} "
                        f"(capital={capital_per_stock:.2f}, gain={gainer['pct_change']:+.2f}%, "
                        f"score={gainer['composite_score']:.3f}, SL-M={pos.hard_stop:.2f}) | {order}",
                        logger, True,
                    )
                except OrderRejectedError as exc:
                    _notify(
                        f"SKIPPED {symbol}: {exc.reason} — trying next candidate.",
                        logger, True,
                    )
                    continue
                except Exception as exc:
                    logger.exception("Failed to enter %s: %s", symbol, exc)
                    send_telegram_message(f"Entry failed for {symbol}: {exc}")

            if not entered_symbols:
                _notify(f"{round_label}No trades could be executed.", logger, True)
                break

            # Add token mappings for monitoring.
            for g in top_gainers:
                if g["symbol"] not in token_map:
                    token_map[g["symbol"]] = g["token"]

            # --- Phase 5: Monitor positions with trailing stop until market close ---
            logger.info("%sMonitoring %d positions with trailing stop...",
                        round_label, len(position_tracker.snapshot()))

            positions_closed_by_sl = False  # Track if monitoring ended due to all SL exits.

            while True:
                now = _ist_now()
                if now.hour > MARKET_CLOSE_HOUR or (now.hour == MARKET_CLOSE_HOUR and now.minute >= MARKET_CLOSE_MIN):
                    break

                if consecutive_losses >= settings.max_consecutive_losses:
                    _notify(
                        f"Stopping: {consecutive_losses} consecutive losing trades. "
                        f"Force-closing remaining positions.",
                        logger, True,
                    )
                    break

                positions = position_tracker.snapshot()
                if not positions:
                    positions_closed_by_sl = True
                    logger.info("All positions closed by stop-loss.")
                    break

                # Batch-fetch current prices for all open positions.
                open_tokens = [token_map[s] for s in positions if s in token_map]
                if not open_tokens:
                    break

                try:
                    result = broker.get_market_data("LTP", {"NSE": open_tokens})
                    fetched = {str(q.get("symbolToken", "")): float(q.get("ltp", 0)) for q in result.get("fetched", [])}
                except Exception as exc:
                    logger.warning("Price fetch failed: %s", exc)
                    time.sleep(settings.monitor_interval_seconds)
                    continue

                for symbol, pos in list(positions.items()):
                    token = token_map.get(symbol)
                    if not token:
                        continue
                    current_price = fetched.get(token, 0)
                    if current_price <= 0:
                        continue

                    try:
                        old_sl = pos.stop_loss
                        position_tracker.update_trailing_stop(symbol, current_price)

                        if pos.stop_loss > old_sl:
                            logger.info(
                                "TRAILING STOP UPDATE %s: SL %.2f -> %.2f (price=%.2f, high=%.2f)",
                                symbol, old_sl, pos.stop_loss, current_price, pos.highest_price,
                            )

                            # Update the server-side SL-M trigger to match the new trailing stop.
                            slm_id = slm_orders.get(symbol)
                            if slm_id and pos.stop_loss > slm_triggers.get(symbol, 0):
                                ok = _update_slm_trigger(
                                    broker, slm_id, symbol, token,
                                    pos.quantity, pos.stop_loss, logger,
                                )
                                if ok:
                                    slm_triggers[symbol] = pos.stop_loss

                        if position_tracker.should_exit(symbol, current_price):
                            exit_reason = "HARD STOP (2% max loss)" if current_price <= pos.hard_stop else "TRAILING STOP"

                            # Cancel the server-side SL-M before placing software exit.
                            slm_id = slm_orders.pop(symbol, None)
                            if slm_id:
                                _cancel_slm_order(broker, slm_id, symbol, logger)
                            slm_triggers.pop(symbol, None)

                            broker_instrument = Instrument(
                                symbol=symbol, exchange="NSE",
                                tradingsymbol=symbol, symboltoken=token,
                            )
                            exit_order = order_manager.place_exit_order(
                                symbol, pos.quantity, instrument=broker_instrument,
                                current_price=current_price,
                            )
                            pnl = (current_price - pos.average_price) * pos.quantity
                            position_tracker.update_sell(symbol, pos.quantity)

                            # Track consecutive losses.
                            if pnl < 0:
                                consecutive_losses += 1
                            else:
                                consecutive_losses = 0  # Reset on a winning trade.

                            _notify(
                                f"{exit_reason} EXIT: {symbol} — {pos.quantity} shares @ {current_price:.2f} "
                                f"(entry={pos.average_price:.2f}, high={pos.highest_price:.2f}, "
                                f"SL={pos.stop_loss:.2f}, hard_stop={pos.hard_stop:.2f}, PnL={pnl:+.2f}, "
                                f"consecutive_losses={consecutive_losses}/{settings.max_consecutive_losses}, "
                                f"round={reentry_round}/{MAX_REENTRY_ROUNDS}) | {exit_order}",
                                logger, True,
                            )
                    except Exception as exc:
                        logger.exception("Error monitoring %s: %s", symbol, exc)

                time.sleep(settings.monitor_interval_seconds)

            # Check if we should re-enter or stop for the day.
            if not positions_closed_by_sl:
                break  # Market close or max losses — no re-entry.
            if consecutive_losses >= settings.max_consecutive_losses:
                break  # Too many losses — stop.

            reentry_round += 1
            if reentry_round > MAX_REENTRY_ROUNDS:
                _notify(
                    f"Max re-entry rounds ({MAX_REENTRY_ROUNDS}) reached. Done for today.",
                    logger, True,
                )
                break

            # Loop back to scan → enter → monitor.

        # --- Phase 6: Force-close any remaining positions at market close ---
        remaining = position_tracker.snapshot()
        if remaining:
            _notify(f"Market closing — force-exiting {len(remaining)} positions.", logger, True)
            for symbol, pos in list(remaining.items()):
                try:
                    token = token_map.get(symbol)
                    if not token:
                        continue

                    # Cancel the server-side SL-M order before placing the exit.
                    slm_id = slm_orders.pop(symbol, None)
                    if slm_id:
                        _cancel_slm_order(broker, slm_id, symbol, logger)
                    slm_triggers.pop(symbol, None)

                    # Get final price for exit order and PnL.
                    try:
                        result = broker.get_market_data("LTP", {"NSE": [token]})
                        final_price = float(result.get("fetched", [{}])[0].get("ltp", pos.highest_price))
                    except Exception:
                        final_price = pos.highest_price

                    broker_instrument = Instrument(
                        symbol=symbol, exchange="NSE",
                        tradingsymbol=symbol, symboltoken=token,
                    )
                    exit_order = order_manager.place_exit_order(
                        symbol, pos.quantity, instrument=broker_instrument,
                        current_price=final_price,
                    )

                    pnl = (final_price - pos.average_price) * pos.quantity
                    position_tracker.update_sell(symbol, pos.quantity)
                    _notify(
                        f"MARKET CLOSE EXIT: {symbol} — {pos.quantity} shares @ {final_price:.2f} "
                        f"(entry={pos.average_price:.2f}, PnL={pnl:+.2f}) | {exit_order}",
                        logger, True,
                    )
                except Exception as exc:
                    logger.exception("Failed to close %s: %s", symbol, exc)
                    send_telegram_message(f"URGENT: Failed to close {symbol}: {exc}")

        _notify("Trading day complete. Sleeping until tomorrow.", logger, True)
        time.sleep(3600)


if __name__ == "__main__":
    run_loop()

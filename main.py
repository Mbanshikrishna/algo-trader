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
from strategy.market_scanner import (
    clear_daily_candle_cache, is_market_bullish, load_nse_equity_tokens, scan_top_gainers,
)
from utils.atr import fetch_entry_atr
from utils.logger import setup_logger
from utils.telegram_alert import (
    send_critical_alert, send_daily_loss_alert, send_exit_failure_alert,
    send_telegram_message,
)

IST = ZoneInfo("Asia/Kolkata")

# Market timing constants.
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 15
SCAN_START_HOUR, SCAN_START_MIN = 9, 45   # Start scanning at 9:45 AM IST.
SCAN_END_HOUR, SCAN_END_MIN = 12, 30      # Stop scanning at 12:30 PM IST.
SCAN_RETRY_SECONDS = 120                   # Wait 2 minutes between scan attempts.
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 15, 15  # Exit all positions by 3:15 PM.
TOP_N = 2  # Number of top gainers to trade.
MAX_REENTRY_ROUNDS = 3  # Max times the bot can re-scan and re-enter after all positions close.
REENTRY_COOLDOWN_MINUTES = 15  # Wait this long after last exit before re-scanning.
REENTRY_SCAN_ATTEMPTS = 3  # Number of scan+validate attempts per re-entry round.
REENTRY_SCAN_DELAY = 60  # Seconds between re-entry scan attempts.
MAX_DAILY_LOSS_PCT = 0.05  # Stop new trades if daily loss exceeds 5% of capital.
_EXIT_MAX_RETRIES = 3  # Max retries for exit orders.
_EXIT_RETRY_DELAY = 1.0  # Seconds between exit retries.


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


def _check_slm_executed(broker: AngelOneClient, slm_order_id: str, symbol: str, logger) -> bool:
    """Check if a SL-M order has already been executed by the exchange.

    Returns True if the SL-M is COMPLETE/TRIGGERED (shares already sold).
    Returns False if still pending or if the check fails (safe default).
    """
    try:
        order_book = broker.get_order_book()
        orders = order_book.get("data") or []
        for order in orders:
            if str(order.get("orderid", "")) == str(slm_order_id):
                status = str(order.get("status", "")).upper()
                if status in ("COMPLETE", "TRIGGERED", "EXECUTED"):
                    logger.info(
                        "[exit_attempt] SL-M %s for %s already %s — skipping software exit.",
                        slm_order_id, symbol, status,
                    )
                    return True
                return False
    except Exception as exc:
        logger.warning("Failed to check SL-M status for %s: %s — proceeding with software exit.", symbol, exc)
    return False


def _safe_exit(
    broker: AngelOneClient,
    order_manager: OrderManager,
    symbol: str,
    token: str,
    quantity: int,
    current_price: float,
    logger,
) -> dict | None:
    """Place an exit order with retry and escalation.

    Retries up to _EXIT_MAX_RETRIES times. If all fail, sends a critical
    Telegram alert requiring manual intervention.
    Returns the exit order result, or None if all attempts failed.
    """
    last_exc: Exception | None = None
    broker_instrument = Instrument(
        symbol=symbol, exchange="NSE", tradingsymbol=symbol, symboltoken=token,
    )

    for attempt in range(_EXIT_MAX_RETRIES):
        try:
            result = order_manager.place_exit_order(
                symbol, quantity, instrument=broker_instrument, current_price=current_price,
            )
            logger.info(
                "[exit_attempt] Exit order placed for %s: %d shares @ %.2f (attempt %d/%d)",
                symbol, quantity, current_price, attempt + 1, _EXIT_MAX_RETRIES,
            )
            return result
        except Exception as exc:
            last_exc = exc
            logger.error(
                "[exit_attempt] Exit order FAILED for %s: %s (attempt %d/%d)",
                symbol, exc, attempt + 1, _EXIT_MAX_RETRIES,
            )
            if attempt < _EXIT_MAX_RETRIES - 1:
                time.sleep(_EXIT_RETRY_DELAY)

    # All retries exhausted — send critical alert.
    reason = str(last_exc) if last_exc else "Unknown error"
    send_exit_failure_alert(symbol, quantity, reason)
    return None


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


def _within_scan_window() -> bool:
    """Return True if current time is within the scan window (9:45 AM - 12:30 PM)."""
    now = _ist_now()
    start = now.replace(hour=SCAN_START_HOUR, minute=SCAN_START_MIN, second=0)
    end = now.replace(hour=SCAN_END_HOUR, minute=SCAN_END_MIN, second=0)
    return start <= now <= end


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

        # --- Phase 0: Daily housekeeping ---
        clear_daily_candle_cache()  # Fresh candle data for each trading day.
        daily_pnl = 0.0  # Track cumulative P&L for the day.
        daily_loss_breached = False
        total_capital = _allocate_capital(broker, settings, logger)
        max_daily_loss = total_capital * MAX_DAILY_LOSS_PCT

        # Refresh session if stale (every 2 hours).
        try:
            broker.refresh_if_stale(max_age_seconds=7200)
        except Exception as exc:
            logger.error("[auth_refresh] Periodic session refresh failed: %s", exc)

        # --- Phase 1: Wait for scan window (9:45 AM) ---
        _wait_until(SCAN_START_HOUR, SCAN_START_MIN, logger)

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

            # --- Phase 3+4: Scan, filter, validate — retry within scan window ---
            scan_pool_size = TOP_N * 5
            round_label = f"[Round {reentry_round}] " if reentry_round > 0 else ""
            validated: list[dict] = []
            scan_attempt = 0

            while not validated:
                scan_attempt += 1

                # Check scan window: round 0 retries until 12:30 PM,
                # re-entry rounds get up to REENTRY_SCAN_ATTEMPTS tries.
                if scan_attempt > 1:
                    if reentry_round > 0:
                        if scan_attempt > REENTRY_SCAN_ATTEMPTS:
                            break
                        logger.info(
                            "%sRe-entry scan attempt %d/%d — retrying in %ds...",
                            round_label, scan_attempt, REENTRY_SCAN_ATTEMPTS, REENTRY_SCAN_DELAY,
                        )
                        time.sleep(REENTRY_SCAN_DELAY)
                    elif not _within_scan_window():
                        break
                    else:
                        logger.info(
                            "%sScan attempt %d — retrying in %ds (window open until %02d:%02d)...",
                            round_label, scan_attempt, SCAN_RETRY_SECONDS,
                            SCAN_END_HOUR, SCAN_END_MIN,
                        )
                        time.sleep(SCAN_RETRY_SECONDS)
                        if not _within_scan_window():
                            break

                logger.info("%sScanning %d stocks for top %d candidates (pool=%d)...",
                            round_label, len(nse_stocks), TOP_N, scan_pool_size)
                scan_start = time.perf_counter()
                raw_candidates = scan_top_gainers(broker, nse_stocks, top_n=scan_pool_size)
                scan_duration = time.perf_counter() - scan_start
                logger.info("Scan completed in %.1f seconds. Found %d raw candidates.", scan_duration, len(raw_candidates))

                if not raw_candidates:
                    _notify(f"{round_label}No qualifying gainers found.", logger, True)
                    continue

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
                    continue

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
                    continue

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
                for gainer, vr in validated_pairs:
                    gainer["live_price"] = vr.live_price
                    validated.append(gainer)

                if not validated:
                    _notify(
                        f"{round_label}No candidates passed real-time validation.",
                        logger, True,
                    )
                    continue  # Retry within scan window.

            if not validated:
                _notify(
                    f"{round_label}Scan window closed ({SCAN_START_HOUR:02d}:{SCAN_START_MIN:02d}"
                    f"-{SCAN_END_HOUR:02d}:{SCAN_END_MIN:02d}) with no valid entries.",
                    logger, True,
                )
                break

            # Check daily P&L limit before entering new trades.
            if daily_pnl <= -max_daily_loss:
                daily_loss_breached = True
                send_daily_loss_alert(daily_pnl, -max_daily_loss)
                _notify(
                    f"{round_label}Daily loss limit breached: ₹{daily_pnl:+,.2f}. No new trades.",
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

                    # Compute ATR from recent 5-min candles for adaptive stop sizing.
                    entry_atr = fetch_entry_atr(broker, gainer["token"], live_price)

                    prev_close = gainer.get("prev_close", live_price / (1 + gainer["pct_change"] / 100))
                    pos = position_tracker.update_buy(
                        symbol, qty, live_price, atr=entry_atr, prev_close=prev_close,
                    )
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

                    atr_pct = (entry_atr / live_price) * 100
                    _notify(
                        f"{round_label}ENTRY: {symbol} — {qty} shares @ {live_price:.2f} "
                        f"(capital={capital_per_stock:.2f}, gain={gainer['pct_change']:+.2f}%, "
                        f"score={gainer['composite_score']:.3f}, ATR={entry_atr:.4f} ({atr_pct:.2f}%), "
                        f"SL={pos.stop_loss:.2f}, hard_stop={pos.hard_stop:.2f}) | {order}",
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

                # Batch-fetch FULL quotes for all open positions.
                # FULL mode gives LTP + depth (best bid/ask) at the same API cost.
                open_tokens = [token_map[s] for s in positions if s in token_map]
                if not open_tokens:
                    break

                try:
                    result = broker.get_market_data("FULL", {"NSE": open_tokens})
                    fetched_quotes = {
                        str(q.get("symbolToken", "")): q for q in result.get("fetched", [])
                    }
                except Exception as exc:
                    logger.warning("Price fetch failed: %s", exc)
                    time.sleep(settings.monitor_interval_seconds)
                    continue

                # Refresh session periodically during monitoring.
                try:
                    broker.refresh_if_stale(max_age_seconds=7200)
                except Exception as exc:
                    logger.error("[auth_refresh] Session refresh during monitoring failed: %s", exc)

                for symbol, pos in list(positions.items()):
                    token = token_map.get(symbol)
                    if not token:
                        continue
                    quote = fetched_quotes.get(token, {})
                    ltp = float(quote.get("ltp", 0))
                    if ltp <= 0:
                        continue

                    # Use best bid for stop-loss comparison (more conservative —
                    # reflects the actual price we'd get on a market sell).
                    depth = quote.get("depth", {})
                    buy_depth = depth.get("buy", [{}])
                    best_bid = float(buy_depth[0].get("price", 0)) if buy_depth else 0
                    stop_check_price = best_bid if best_bid > 0 else ltp

                    try:
                        old_sl = pos.stop_loss
                        # Trail stop based on LTP (tracks the high).
                        position_tracker.update_trailing_stop(symbol, ltp)

                        if pos.stop_loss > old_sl:
                            profit_pct = (pos.highest_price - pos.average_price) / pos.average_price
                            intraday_pct = pos.intraday_gain_pct(ltp) * 100
                            logger.info(
                                "TRAILING STOP UPDATE %s: SL %.2f -> %.2f "
                                "(ltp=%.2f, bid=%.2f, high=%.2f, profit=%.1f%%, "
                                "intraday=%.1f%%, locked=%s, ATR=%.4f)",
                                symbol, old_sl, pos.stop_loss, ltp, best_bid,
                                pos.highest_price, profit_pct * 100,
                                intraday_pct, pos.profit_locked, pos.atr,
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

                        # Use best bid for exit decision (conservative).
                        if position_tracker.should_exit(symbol, stop_check_price):
                            exit_reason = position_tracker.get_exit_reason(symbol, stop_check_price)

                            # SL-M race condition check: if the exchange already
                            # executed our SL-M, skip the software exit.
                            slm_id = slm_orders.get(symbol)
                            if slm_id and _check_slm_executed(broker, slm_id, symbol, logger):
                                # SL-M already sold the shares. Clean up tracking.
                                slm_orders.pop(symbol, None)
                                slm_triggers.pop(symbol, None)
                                pnl = (stop_check_price - pos.average_price) * pos.quantity
                                daily_pnl += pnl
                                position_tracker.update_sell(symbol, pos.quantity)
                                if pnl < 0:
                                    consecutive_losses += 1
                                else:
                                    consecutive_losses = 0
                                _notify(
                                    f"{exit_reason} EXIT (SL-M executed): {symbol} — {pos.quantity} shares "
                                    f"(entry={pos.average_price:.2f}, PnL={pnl:+.2f}, daily={daily_pnl:+.2f})",
                                    logger, True,
                                )
                                continue

                            # Cancel the server-side SL-M before placing software exit.
                            if slm_id:
                                _cancel_slm_order(broker, slm_id, symbol, logger)
                            slm_orders.pop(symbol, None)
                            slm_triggers.pop(symbol, None)

                            exit_order = _safe_exit(
                                broker, order_manager, symbol, token,
                                pos.quantity, stop_check_price, logger,
                            )
                            pnl = (stop_check_price - pos.average_price) * pos.quantity
                            daily_pnl += pnl
                            position_tracker.update_sell(symbol, pos.quantity)

                            # Track consecutive losses.
                            if pnl < 0:
                                consecutive_losses += 1
                            else:
                                consecutive_losses = 0

                            _notify(
                                f"{exit_reason} EXIT: {symbol} — {pos.quantity} shares @ {stop_check_price:.2f} "
                                f"(entry={pos.average_price:.2f}, high={pos.highest_price:.2f}, "
                                f"SL={pos.stop_loss:.2f}, hard_stop={pos.hard_stop:.2f}, ATR={pos.atr:.4f}, "
                                f"locked={pos.profit_locked}, PnL={pnl:+.2f}, daily_pnl={daily_pnl:+.2f}, "
                                f"consecutive_losses={consecutive_losses}/{settings.max_consecutive_losses}, "
                                f"round={reentry_round}/{MAX_REENTRY_ROUNDS}) | {exit_order}",
                                logger, True,
                            )
                    except Exception as exc:
                        logger.exception("[critical_failure] Error monitoring %s: %s", symbol, exc)

                time.sleep(settings.monitor_interval_seconds)

            # Check if we should re-enter or stop for the day.
            if not positions_closed_by_sl:
                break  # Market close or max losses — no re-entry.
            if consecutive_losses >= settings.max_consecutive_losses:
                break  # Too many losses — stop.

            # Check daily P&L limit before re-entering.
            if daily_pnl <= -max_daily_loss:
                daily_loss_breached = True
                send_daily_loss_alert(daily_pnl, -max_daily_loss)
                _notify(
                    f"Daily loss limit breached: ₹{daily_pnl:+,.2f} (limit ₹{-max_daily_loss:,.2f}). "
                    f"No new trades.",
                    logger, True,
                )
                break

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

                    # SL-M race check: if exchange already executed SL-M, skip.
                    slm_id = slm_orders.get(symbol)
                    if slm_id and _check_slm_executed(broker, slm_id, symbol, logger):
                        slm_orders.pop(symbol, None)
                        slm_triggers.pop(symbol, None)
                        position_tracker.update_sell(symbol, pos.quantity)
                        logger.info("SL-M already executed for %s — skipping force close.", symbol)
                        continue

                    # Cancel the server-side SL-M order before placing the exit.
                    if slm_id:
                        _cancel_slm_order(broker, slm_id, symbol, logger)
                    slm_orders.pop(symbol, None)
                    slm_triggers.pop(symbol, None)

                    # Get final price for exit order and PnL.
                    try:
                        result = broker.get_market_data("FULL", {"NSE": [token]})
                        fetched_list = result.get("fetched", [{}])
                        final_price = float(fetched_list[0].get("ltp", pos.highest_price)) if fetched_list else pos.highest_price
                    except Exception:
                        final_price = pos.highest_price

                    exit_order = _safe_exit(
                        broker, order_manager, symbol, token,
                        pos.quantity, final_price, logger,
                    )

                    pnl = (final_price - pos.average_price) * pos.quantity
                    daily_pnl += pnl
                    position_tracker.update_sell(symbol, pos.quantity)
                    _notify(
                        f"MARKET CLOSE EXIT: {symbol} — {pos.quantity} shares @ {final_price:.2f} "
                        f"(entry={pos.average_price:.2f}, PnL={pnl:+.2f}, daily={daily_pnl:+.2f}) | {exit_order}",
                        logger, True,
                    )
                except Exception as exc:
                    logger.exception("[critical_failure] Failed to close %s: %s", symbol, exc)
                    send_exit_failure_alert(symbol, pos.quantity, str(exc))

        _notify(
            f"Trading day complete. Daily P&L: ₹{daily_pnl:+,.2f}. Sleeping until tomorrow.",
            logger, True,
        )
        time.sleep(3600)


if __name__ == "__main__":
    run_loop()

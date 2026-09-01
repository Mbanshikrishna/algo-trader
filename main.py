from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from broker.angelone_client import AngelOneClient
from broker.dhan_client import DhanClient
from broker.paper_client import PaperBrokerClient
from config.instruments import Instrument
from config.settings import load_settings
from execution.entry_validator import validate_entries_batch
from execution.order_manager import (
    OrderExecution,
    OrderManager,
    OrderRejectedError,
    OrderState,
)
from execution.tradability_filter import TradabilityFilter
from monitor.decision_journal import DecisionJournal
from monitor.position_tracker import Position, PositionTracker
from monitor.risk_state import DailyRiskState, calculate_position_size
from strategy.market_scanner import (
    clear_daily_candle_cache,
    is_market_bullish,
    load_nse_equity_tokens,
    scan_top_gainers,
)
from utils.atr import fetch_entry_atr
from utils.logger import setup_logger
from utils.telegram_alert import (
    send_critical_alert,
    send_daily_loss_alert,
    send_exit_failure_alert,
    send_telegram_message,
)
from utils.tick import tick_round

# Union type for broker clients — both implement the same interface.
BrokerClient = AngelOneClient | DhanClient | PaperBrokerClient

IST = ZoneInfo("Asia/Kolkata")
_decision_journal: DecisionJournal | None = None


def _record_decision(event_type: str, **kwargs) -> None:
    """Record an observation without allowing telemetry to alter trading."""
    if _decision_journal is None:
        return
    try:
        _decision_journal.record(event_type, **kwargs)
    except Exception:
        # Trading safety must not depend on an observability write. The logger
        # makes journal failures operationally visible without changing orders.
        logging.getLogger("algo_trader").exception(
            "Failed to persist decision snapshot %s", event_type
        )


def _write_replay_reports(logger) -> None:
    """Compare captured decisions and refresh slippage calibration."""
    if _decision_journal is None:
        return
    if os.getenv("AUTO_REPLAY_COMPARE", "true").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    try:
        from production_replay import (
            calibrate_slippage,
            compare_run,
            export_universe_snapshots,
            write_replay_report,
        )

        run_id, comparisons = compare_run(
            _decision_journal.path, _decision_journal.run_id
        )
        slippage = calibrate_slippage(_decision_journal.path, run_id)
        report_root = Path(
            os.getenv("REPLAY_REPORT_DIR", "data/replay_reports")
        ) / _ist_now().date().isoformat()
        paths = write_replay_report(
            report_root, run_id, comparisons, slippage
        )
        universe_path = export_universe_snapshots(
            _decision_journal.path,
            os.getenv(
                "BACKTEST_UNIVERSE_EXPORT", "data/universe_snapshots.json"
            ),
        )
        matched = sum(item.matched for item in comparisons)
        logger.info(
            "Replay comparison complete: %d/%d decisions matched; "
            "recommended slippage=%s bps; report=%s; universe=%s",
            matched,
            len(comparisons),
            slippage["recommended_backtest_slippage_bps"],
            paths["summary"],
            universe_path,
        )
    except Exception as exc:
        logger.exception("Automatic production replay comparison failed: %s", exc)

# Market timing constants.
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 15
SCAN_START_HOUR, SCAN_START_MIN = 10, 0   # Start scanning at 10:00 AM IST.
SCAN_END_HOUR, SCAN_END_MIN = 14, 0       # Stop scanning at 2:00 PM IST.
SCAN_RETRY_SECONDS = 120                   # Wait 2 minutes between scan attempts.
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 15, 5  # Exit all positions by 3:05 PM.
TOP_N = 1  # Number of top gainers to trade.
MAX_REENTRY_ROUNDS = 2  # Max times the bot can re-scan and re-enter after all positions close.
REENTRY_COOLDOWN_MINUTES = 15  # Wait this long after last exit before re-scanning.
REENTRY_SCAN_ATTEMPTS = 3  # Number of scan+validate attempts per re-entry round.
REENTRY_SCAN_DELAY = 60  # Seconds between re-entry scan attempts.
MAX_DAILY_LOSS_PCT = 0.05  # Stop new trades if daily loss exceeds 5% of capital.
_EXIT_MAX_RETRIES = 3  # Max retries for exit orders.
_EXIT_RETRY_DELAY = 1.0  # Seconds between exit retries.


class ExitState(str, Enum):
    CLOSED = "closed"
    ALREADY_CLOSED = "already_closed"
    FAILED = "failed"


@dataclass(frozen=True)
class ExitOutcome:
    state: ExitState
    execution: OrderExecution | None = None
    reason: str = ""

    @property
    def confirmed_closed(self) -> bool:
        return self.state in {ExitState.CLOSED, ExitState.ALREADY_CLOSED}


def _notify(message: str, logger, send_alert: bool) -> None:
    logger.info(message)
    if send_alert:
        send_telegram_message(message)


def _place_slm_order(
    broker: BrokerClient,
    symbol: str,
    token: str,
    quantity: int,
    trigger_price: float,
    logger,
    product_type: str = "INTRADAY",
) -> str | None:
    """Place a SL-M (Stop-Loss Market) SELL order on the exchange.

    This order sits on the exchange and triggers automatically when the price
    drops to the trigger level — no polling needed.
    Returns the order ID, or None if placement fails.
    """
    # Floor trigger to nearest tick — avoids rejection for non-tick-aligned prices.
    aligned_trigger = tick_round(trigger_price, "down")
    payload = {
        "variety": "STOPLOSS",
        "tradingsymbol": symbol,
        "symboltoken": str(token),
        "transactiontype": "SELL",
        "exchange": "NSE",
        "ordertype": "STOPLOSS_MARKET",
        "producttype": product_type,
        "duration": "DAY",
        "quantity": str(quantity),
        "triggerprice": str(round(aligned_trigger, 2)),
        "price": str(round(aligned_trigger, 2)),
        "squareoff": "0",
        "stoploss": "0",
    }
    _record_decision(
        "protective_order_requested",
        symbol=symbol,
        token=str(token),
        decision="submit",
        payload={"trigger_price": aligned_trigger, "quantity": quantity, "request": payload},
    )
    try:
        result = broker.place_order(payload)
        resp = result.get("response", {})
        data = resp.get("data", {})
        order_id = data.get("orderid") if isinstance(data, dict) else str(data) if data else None
        if order_id:
            _record_decision(
                "protective_order_submitted",
                symbol=symbol,
                token=str(token),
                decision="submitted",
                payload={
                    "order_id": str(order_id),
                    "trigger_price": aligned_trigger,
                    "quantity": quantity,
                    "response": result,
                },
            )
            logger.info("SL-M order placed for %s: trigger=%.2f, orderid=%s", symbol, trigger_price, order_id)
            for _ in range(10):
                try:
                    orders = broker.get_order_book().get("data") or []
                except Exception as exc:
                    send_critical_alert(
                        f"Protective order {order_id} for {symbol} was submitted but its status "
                        f"could not be verified: {exc}"
                    )
                    return order_id
                match = next((o for o in orders if str(o.get("orderid", "")) == str(order_id)), None)
                if match:
                    status = str(match.get("orderstatus") or match.get("status") or "").upper()
                    if status in {"OPEN", "PENDING", "TRIGGER PENDING", "TRIGGER_PENDING"}:
                        return order_id
                    if status in {"COMPLETE", "TRIGGERED", "EXECUTED", "FILLED"}:
                        return order_id
                    if status in {"REJECTED", "CANCELLED", "CANCELED", "EXPIRED"}:
                        logger.error("SL-M order %s for %s became %s", order_id, symbol, status)
                        return None
                time.sleep(0.25)
            send_critical_alert(
                f"Protective order {order_id} for {symbol} was submitted but remained unconfirmed."
            )
            return order_id
        return None
    except Exception as exc:
        logger.warning("Failed to place SL-M order for %s: %s", symbol, exc)
        return None


def _update_slm_trigger(
    broker: BrokerClient,
    order_id: str,
    symbol: str,
    token: str,
    quantity: int,
    new_trigger: float,
    logger,
    product_type: str = "INTRADAY",
) -> bool:
    """Modify an existing SL-M order's trigger price (trail it up)."""
    aligned_trigger = tick_round(new_trigger, "down")
    payload = {
        "variety": "STOPLOSS",
        "orderid": order_id,
        "tradingsymbol": symbol,
        "symboltoken": str(token),
        "transactiontype": "SELL",
        "exchange": "NSE",
        "ordertype": "STOPLOSS_MARKET",
        "producttype": product_type,
        "duration": "DAY",
        "quantity": str(quantity),
        "triggerprice": str(round(aligned_trigger, 2)),
        "price": str(round(aligned_trigger, 2)),
    }
    try:
        broker.modify_order(payload)
        _record_decision(
            "protective_order_replaced",
            symbol=symbol,
            token=str(token),
            decision="confirmed",
            payload={
                "order_id": order_id,
                "new_trigger": aligned_trigger,
                "quantity": quantity,
                "request": payload,
            },
        )
        logger.info("SL-M trigger updated for %s: new trigger=%.2f", symbol, new_trigger)
        return True
    except Exception as exc:
        logger.warning("Failed to update SL-M for %s: %s", symbol, exc)
        return False


def _cancel_slm_order(broker: BrokerClient, order_id: str, symbol: str, logger) -> bool:
    """Cancel a protective order and confirm its terminal broker state."""
    try:
        broker.cancel_order(order_id, "STOPLOSS")
    except Exception as exc:
        logger.warning("Failed to request SL-M cancellation for %s: %s", symbol, exc)

    for _ in range(10):
        try:
            orders = broker.get_order_book().get("data") or []
            match = next((o for o in orders if str(o.get("orderid", "")) == str(order_id)), None)
            if match:
                status = str(match.get("orderstatus") or match.get("status") or "").upper()
                if status in {"CANCELLED", "CANCELED", "REJECTED", "EXPIRED"}:
                    _record_decision(
                        "protective_order_cancelled",
                        symbol=symbol,
                        decision="confirmed",
                        reason=status,
                        payload={"order_id": order_id, "broker_order": match},
                    )
                    logger.info("SL-M order confirmed %s for %s: %s", status, symbol, order_id)
                    return True
                if status in {"COMPLETE", "TRIGGERED", "EXECUTED", "FILLED"}:
                    _record_decision(
                        "protective_order_cancelled",
                        symbol=symbol,
                        decision="not_cancelled",
                        reason=status,
                        payload={"order_id": order_id, "broker_order": match},
                    )
                    logger.critical("SL-M %s executed while cancelling for %s", order_id, symbol)
                    return False
        except Exception as exc:
            logger.warning("Could not verify SL-M cancellation for %s: %s", symbol, exc)
        time.sleep(0.25)
    send_critical_alert(f"Protective order {order_id} for {symbol} was not confirmed cancelled.")
    return False


def _check_slm_executed(broker: BrokerClient, slm_order_id: str, symbol: str, logger) -> bool:
    """Check if a SL-M order has already been executed by the exchange.

    Returns True if the SL-M is COMPLETE/TRIGGERED (shares already sold).
    Returns False if still pending or if the check fails (safe default).
    """
    try:
        order_book = broker.get_order_book()
        orders = order_book.get("data") or []
        for order in orders:
            if str(order.get("orderid", "")) == str(slm_order_id):
                # Angel One uses "status", DhanClient normalizes to "orderstatus".
                status = str(order.get("orderstatus") or order.get("status", "")).upper()
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


def _check_broker_position(broker: BrokerClient, symbol: str, logger) -> int:
    """Check the broker's position book for actual shares held.

    Returns the net quantity held for the symbol, or -1 if the check fails.
    A return of 0 means the position is already closed (e.g., SL-M executed).
    """
    try:
        positions = broker.get_positions()
        pos_list = positions.get("data") or []
        for pos in pos_list:
            ts = pos.get("tradingsymbol", "")
            if ts == symbol:
                net_qty = int(pos.get("netqty", 0))
                logger.info("[position_check] %s: netqty=%d", symbol, net_qty)
                _record_decision(
                    "broker_position_checked",
                    symbol=symbol,
                    decision="open" if net_qty else "closed",
                    payload={"net_quantity": net_qty, "broker_position": pos},
                )
                return net_qty
        # Symbol not in position book — no position.
        logger.info("[position_check] %s: not found in position book (already closed)", symbol)
        _record_decision(
            "broker_position_checked",
            symbol=symbol,
            decision="closed",
            reason="symbol absent from broker position book",
            payload={"net_quantity": 0},
        )
        return 0
    except Exception as exc:
        logger.warning("[position_check] Failed to check position for %s: %s", symbol, exc)
        return -1  # Unknown — proceed cautiously.


def _safe_exit(
    broker: BrokerClient,
    order_manager: OrderManager,
    symbol: str,
    token: str,
    quantity: int,
    current_price: float,
    logger,
    product_type: str = "INTRADAY",
) -> ExitOutcome:
    """Place an exit order with retry and escalation.

    Before each retry, checks the broker's position book. If the position
    is already closed (e.g., SL-M executed between retries), stops retrying.
    A position is closed only after the broker confirms the fill or reports
    zero quantity. Unconfirmed submitted orders are not duplicated.
    """
    last_exc: Exception | None = None
    broker_instrument = Instrument(
        symbol=symbol, exchange="NSE", tradingsymbol=symbol, symboltoken=token,
    )

    for attempt in range(_EXIT_MAX_RETRIES):
        # Before retrying, verify we still hold the position.
        if attempt >= 0:
            actual_qty = _check_broker_position(broker, symbol, logger)
            if actual_qty == 0:
                logger.info(
                    "[exit_attempt] %s: position already closed (SL-M likely executed). "
                    "Skipping retry %d/%d.",
                    symbol, attempt + 1, _EXIT_MAX_RETRIES,
                )
                return ExitOutcome(ExitState.ALREADY_CLOSED)
            if actual_qty > 0 and actual_qty != quantity:
                logger.warning(
                    "[exit_attempt] %s: broker shows %d shares but we expected %d. "
                    "Using broker quantity.",
                    symbol, actual_qty, quantity,
                )
                quantity = actual_qty

        try:
            result = order_manager.place_exit_order(
                symbol, quantity, instrument=broker_instrument, current_price=current_price,
                product_type=product_type,
            )
            execution = order_manager.wait_for_fill(result, symbol, quantity, timeout=20.0)
            if execution.is_filled:
                logger.info(
                    "[exit_attempt] Exit confirmed for %s: %d shares @ %.2f",
                    symbol, execution.filled_quantity, execution.average_price,
                )
                return ExitOutcome(ExitState.CLOSED, execution)
            if execution.state in {OrderState.UNKNOWN, OrderState.PARTIALLY_FILLED}:
                reason = f"exit order {execution.order_id} is {execution.state.value}"
                send_exit_failure_alert(symbol, quantity, reason)
                return ExitOutcome(ExitState.FAILED, execution, reason)
            last_exc = RuntimeError(execution.reason or execution.state.value)
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
    return ExitOutcome(ExitState.FAILED, reason=reason)


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


def _within_scan_window(end_hour: int = SCAN_END_HOUR, end_min: int = SCAN_END_MIN) -> bool:
    """Return True if current time is within the scan window."""
    now = _ist_now()
    start = now.replace(hour=SCAN_START_HOUR, minute=SCAN_START_MIN, second=0)
    end = now.replace(hour=end_hour, minute=end_min, second=0)
    return start <= now <= end


def _seconds_until_next_scan(now: datetime | None = None) -> float:
    """Return a bounded sleep until the next weekday scan window."""
    current = now or _ist_now()
    target = (current + timedelta(days=1)).replace(
        hour=SCAN_START_HOUR,
        minute=SCAN_START_MIN,
        second=0,
        microsecond=0,
    )
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return max((target - current).total_seconds(), 60)


def _allocate_capital(broker: BrokerClient, settings, logger) -> float:
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


def _login_angelone(settings, logger) -> AngelOneClient:
    """Login to Angel One and return the client."""
    if not (settings.api_key and settings.client_id and settings.pin and settings.totp_secret):
        raise ValueError(
            "Angel One requires ANGELONE_API_KEY, ANGELONE_CLIENT_ID, ANGELONE_PIN, and ANGELONE_TOTP_SECRET"
        )
    logger.info("Logging in to Angel One...")
    client = AngelOneClient.login(
        api_key=settings.api_key,
        client_id=settings.client_id,
        pin=settings.pin,
        totp_secret=settings.totp_secret,
    )
    logger.info("Angel One login successful.")
    return client


def _login_dhan(settings, logger) -> DhanClient:
    """Login to Dhan and return the client."""
    if not settings.dhan_client_id:
        raise ValueError("Dhan requires DHAN_CLIENT_ID")
    logger.info("Logging in to Dhan...")
    client = DhanClient.login(
        client_id=settings.dhan_client_id,
        access_token=settings.dhan_access_token,
    )
    logger.info("Dhan login successful.")
    return client


def run_loop() -> None:
    global _decision_journal
    settings = load_settings()
    logger = setup_logger()
    snapshots_enabled = os.getenv(
        "DECISION_SNAPSHOTS_ENABLED", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if snapshots_enabled:
        _decision_journal = DecisionJournal(
            os.getenv("DECISION_JOURNAL_PATH", "data/decision_journal.sqlite3"),
            snapshot_dir=os.getenv(
                "UNIVERSE_SNAPSHOT_DIR", "data/universe_snapshots"
            ),
            mode="paper" if settings.paper_trade else "live",
            broker=settings.broker,
            config=asdict(settings),
        )
        logger.info("Decision snapshots enabled: run_id=%s", _decision_journal.run_id)

    # --- Broker setup ---
    # When BROKER=dhan, Dhan handles orders/positions/capital.
    # Angel One is always needed for market data (scanning, quotes, candles)
    # because Dhan's market data API requires a separate subscription.
    use_dhan = settings.broker == "dhan"

    # Angel One is always required — for market data at minimum.
    angelone = _login_angelone(settings, logger)

    if settings.paper_trade:
        # Paper trading: simulate orders, use Angel One for market data.
        paper = PaperBrokerClient(data_broker=angelone, capital=settings.capital)
        broker: BrokerClient = paper
        data_broker: BrokerClient = angelone
        logger.info("Broker mode: PAPER TRADING (simulated orders) + Angel One (market data)")
    elif use_dhan:
        dhan = _login_dhan(settings, logger)
        # Dhan for orders; Angel One for market data.
        broker = dhan
        data_broker = angelone
        logger.info("Broker mode: DHAN (orders) + Angel One (market data)")
    else:
        broker = angelone
        data_broker = angelone
        logger.info("Broker mode: Angel One (all)")

    # Initialize tradability filter and load restricted stock lists.
    trad_filter = TradabilityFilter(
        safe_mode=settings.safe_mode,
        fno_required=settings.fno_only,
    )
    logger.info("Loading ASM/GSM/F&O restricted lists from NSE...")
    trad_filter.load_restricted_lists()

    order_manager = OrderManager(
        broker_client=broker,
        product_type=settings.order_product_type,
        variety=settings.order_variety,
        tradability_filter=trad_filter,
        event_sink=_record_decision,
    )
    position_tracker = PositionTracker(settings.runtime_state_path)
    risk_state = DailyRiskState.load(settings.daily_risk_state_path)

    # Load scrip master from Angel One (canonical symbol/token source).
    # When using Dhan, also load Dhan's scrip master for symbol→security_id mapping.
    logger.info("Loading NSE equity scrip master...")
    nse_stocks = load_nse_equity_tokens(angelone)
    logger.info("Loaded %d NSE equity stocks.", len(nse_stocks))

    # Reconcile persisted positions with the broker before any new order is allowed.
    try:
        reconciliation = position_tracker.reconcile(broker.get_positions())
        _record_decision(
            "startup_reconciliation",
            decision="reconciled",
            payload={
                "changes": reconciliation,
                "positions": {
                    symbol: asdict(position)
                    for symbol, position in position_tracker.snapshot().items()
                },
            },
        )
        for change in reconciliation:
            logger.warning("[reconciliation] %s", change)
    except Exception as exc:
        raise RuntimeError(f"Startup position reconciliation failed; refusing to trade: {exc}") from exc

    # Reconcile exchange-side protection before scanning for any new trade.
    restored = position_tracker.snapshot()
    if restored:
        try:
            broker_orders = broker.get_order_book().get("data") or []
        except Exception as exc:
            raise RuntimeError(f"Startup order reconciliation failed; refusing to trade: {exc}") from exc
        order_status = {
            str(order.get("orderid", "")): str(
                order.get("orderstatus") or order.get("status") or ""
            ).upper()
            for order in broker_orders
        }
        active_statuses = {"OPEN", "PENDING", "TRIGGER PENDING", "TRIGGER_PENDING"}
        for symbol, pos in restored.items():
            protected = (
                bool(pos.protective_order_id)
                and order_status.get(pos.protective_order_id, "") in active_statuses
            )
            if protected:
                continue
            if not pos.token:
                raise RuntimeError(f"Cannot restore protection for {symbol}: broker token is missing")
            replacement = _place_slm_order(
                broker, symbol, pos.token, pos.quantity, pos.hard_stop, logger,
                product_type=pos.product_type,
            )
            if not replacement:
                raise RuntimeError(f"Cannot restore exchange protection for {symbol}; refusing to trade")
            position_tracker.set_protective_order(symbol, replacement)

    token_map = {symbol: pos.token for symbol, pos in position_tracker.snapshot().items() if pos.token}
    product_type_map = {
        symbol: pos.product_type for symbol, pos in position_tracker.snapshot().items()
    }
    slm_orders = {
        symbol: pos.protective_order_id for symbol, pos in position_tracker.snapshot().items()
        if pos.protective_order_id
    }
    slm_triggers = {
        symbol: pos.stop_loss for symbol, pos in position_tracker.snapshot().items()
        if pos.protective_order_id
    }

    if use_dhan and not settings.paper_trade:
        logger.info("Loading Dhan scrip master for symbol mapping...")
        dhan.load_scrip_master()
        logger.info("Dhan scrip master loaded.")

    last_universe_snapshot_date = ""
    while True:
        now = _ist_now()

        # Only run on weekdays.
        if now.weekday() >= 5:
            logger.info("Weekend — sleeping until Monday.")
            time.sleep(3600)
            continue

        # --- Phase 0: Daily housekeeping ---
        trading_date = now.date().isoformat()
        if trading_date != last_universe_snapshot_date:
            nse_stocks = load_nse_equity_tokens(angelone)
            if _decision_journal is not None:
                _decision_journal.snapshot_universe(
                    trading_date, trad_filter.annotate_universe(nse_stocks)
                )
            last_universe_snapshot_date = trading_date
        clear_daily_candle_cache()  # Fresh candle data for each trading day.
        risk_state.rollover_if_needed(now.date().isoformat())
        daily_pnl = risk_state.realized_pnl
        total_capital = _allocate_capital(broker, settings, logger)
        max_daily_loss = total_capital * settings.max_daily_loss_pct / 100.0

        # --- Phase 1: Wait for scan window (10:00 AM) ---
        _wait_until(SCAN_START_HOUR, SCAN_START_MIN, logger)

        now = _ist_now()
        if now.hour > MARKET_CLOSE_HOUR or (now.hour == MARKET_CLOSE_HOUR and now.minute >= MARKET_CLOSE_MIN):
            _write_replay_reports(logger)
            sleep_secs = _seconds_until_next_scan(now)
            logger.info(
                "Market closed for today. Sleeping %.1fh until the next scan window.",
                sleep_secs / 3600,
            )
            time.sleep(sleep_secs)
            continue

        # Restriction downloads can fail transiently at startup. Retry before
        # doing an expensive full-universe scan, but never bypass safe mode.
        if not trad_filter.ready:
            logger.warning(
                "Tradability data is not ready; refreshing restriction lists before scanning."
            )
            trad_filter.load_restricted_lists()
            if not trad_filter.ready:
                logger.error(
                    "Required tradability data is still unavailable; retrying in %ds.",
                    SCAN_RETRY_SECONDS,
                )
                time.sleep(SCAN_RETRY_SECONDS)
                continue
            if _decision_journal is not None:
                _decision_journal.snapshot_universe(
                    trading_date, trad_filter.annotate_universe(nse_stocks)
                )

        # Refresh sessions right before market check — not earlier, because
        # Angel One tokens expire in ~1-2 hours and the wait above can be 3+ hours.
        try:
            data_broker.refresh_session()
            logger.info("[auth_refresh] Angel One session refreshed.")
            if use_dhan and not settings.paper_trade:
                broker.refresh_if_stale(max_age_seconds=7200)
        except Exception as exc:
            logger.error("[auth_refresh] Session refresh failed: %s", exc)

        # --- Phase 2: Check if market is bullish (4-factor model) ---
        bullish, market_check = is_market_bullish(
            data_broker, event_sink=_record_decision
        )
        report = market_check.format_report()

        # Contra-momentum mode: score=2 trades with stricter rules.
        contra_mode = market_check.score == 2 and bullish
        if contra_mode:
            report += "\n⚡ CONTRA-MOMENTUM MODE: Score=2 — entry before 12:00 only, no re-entry."

        _notify(report, logger, True)

        if not bullish:
            time.sleep(3600)
            continue

        consecutive_losses = risk_state.consecutive_losses
        traded_today: set[str] = set(risk_state.traded_symbols)
        reentry_round = 0

        # Contra mode: no re-entry allowed.
        max_rounds_today = 0 if contra_mode else MAX_REENTRY_ROUNDS

        # === Re-entry loop: scan → enter → monitor, up to max_rounds_today ===
        while reentry_round <= max_rounds_today:
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
                bullish, market_check = is_market_bullish(
                    data_broker, event_sink=_record_decision
                )
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
                cycle_id = (
                    f"{_ist_now().date().isoformat()}-r{reentry_round}-"
                    f"a{scan_attempt}-{uuid.uuid4().hex[:8]}"
                )
                if _decision_journal is not None:
                    _decision_journal.set_cycle(cycle_id)

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
                    else:
                        # Contra mode: scan window closes at 12:00 instead of 14:00.
                        scan_end_h = 12 if contra_mode else SCAN_END_HOUR
                        scan_end_m = 0 if contra_mode else SCAN_END_MIN
                        if not _within_scan_window(scan_end_h, scan_end_m):
                            break
                        logger.info(
                            "%sScan attempt %d — retrying in %ds (window open until %02d:%02d)...",
                            round_label, scan_attempt, SCAN_RETRY_SECONDS,
                            scan_end_h, scan_end_m,
                        )
                        time.sleep(SCAN_RETRY_SECONDS)
                        if not _within_scan_window(scan_end_h, scan_end_m):
                            break

                logger.info("%sScanning %d stocks for top %d candidates (pool=%d)...",
                            round_label, len(nse_stocks), TOP_N, scan_pool_size)
                scan_start = time.perf_counter()
                raw_candidates = scan_top_gainers(
                    data_broker,
                    nse_stocks,
                    top_n=scan_pool_size,
                    event_sink=_record_decision,
                )
                scan_duration = time.perf_counter() - scan_start
                logger.info("Scan completed in %.1f seconds. Found %d raw candidates.", scan_duration, len(raw_candidates))

                if not raw_candidates:
                    _notify(f"{round_label}No qualifying gainers found.", logger, True)
                    continue

                # --- Phase 3b: Filter for tradability (ASM/GSM/circuit limits) ---
                tradable, skipped = trad_filter.filter_candidates(
                    raw_candidates, fno_only=settings.fno_only,
                )
                _record_decision(
                    "tradability_filter",
                    decision="candidates_available" if tradable else "none",
                    reason="ASM/GSM/circuit and configured universe filters",
                    payload={
                        "input_candidates": raw_candidates,
                        "tradable_candidates": tradable,
                        "skipped": skipped,
                        "fno_only": settings.fno_only,
                    },
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

                # --- Phase 3c: Probe broker tradability (catches cautionary list / other rejections) ---
                logger.info("Probing %d candidates for broker tradability...", len(tradable))
                probe_start = time.perf_counter()
                probed_tradable, probe_skipped = trad_filter.probe_candidates(
                    broker, tradable, max_workers=4,
                )
                _record_decision(
                    "broker_tradability_result",
                    decision="candidates_available" if probed_tradable else "none",
                    payload={
                        "input_candidates": tradable,
                        "tradable_candidates": probed_tradable,
                        "skipped": probe_skipped,
                    },
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
                    validation_queue,
                    data_broker,
                    max_valid=TOP_N,
                    max_workers=4,
                    event_sink=_record_decision,
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
                end_h = 12 if contra_mode else SCAN_END_HOUR
                end_m = 0 if contra_mode else SCAN_END_MIN
                mode_label = " [CONTRA]" if contra_mode else ""
                _notify(
                    f"{round_label}Scan window closed ({SCAN_START_HOUR:02d}:{SCAN_START_MIN:02d}"
                    f"-{end_h:02d}:{end_m:02d}){mode_label} with no valid entries.",
                    logger, True,
                )
                break

            # Check daily P&L limit before entering new trades.
            if daily_pnl <= -max_daily_loss:
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
                # Single stock — deploy 95% of buying power.
                capital_per_stock = leveraged_capital * 0.95
            else:
                # Multiple stocks — split equally.
                capital_per_stock = leveraged_capital / len(validated)

            logger.info(
                "Capital: %.2f x %.1fx leverage = %.2f buying power | "
                "%d validated stocks | %.2f per stock",
                total_capital, settings.intraday_leverage, leveraged_capital,
                len(validated), capital_per_stock,
            )

            # Step 4c: Execute entries using real-time validated prices.
            entered_symbols: list[str] = []
            for gainer in validated:
                symbol = gainer["symbol"]
                live_price = gainer["live_price"]
                stock_product_type = gainer.get("_product_type", "INTRADAY")

                try:
                    # Re-check available margin before each order to avoid
                    # insufficient-funds rejections when placing multiple orders.
                    try:
                        current_margin = broker.get_available_capital()
                        if stock_product_type == "DELIVERY":
                            # CNC: no leverage — use raw cash only.
                            effective_capital = min(capital_per_stock / settings.intraday_leverage, current_margin)
                        else:
                            effective_capital = min(
                                capital_per_stock,
                                current_margin * settings.intraday_leverage,
                            )
                    except Exception as margin_exc:
                        logger.warning("Margin check failed, using planned capital: %s", margin_exc)
                        if stock_product_type == "DELIVERY":
                            effective_capital = capital_per_stock / settings.intraday_leverage
                        else:
                            effective_capital = capital_per_stock

                    instrument = Instrument(
                        symbol=symbol,
                        exchange="NSE",
                        tradingsymbol=symbol,
                        symboltoken=gainer["token"],
                    )

                    # Fetch ATR before submission so size is based on the actual
                    # loss at the initial stop, not on available leverage alone.
                    entry_atr = fetch_entry_atr(data_broker, gainer["token"], live_price)
                    prev_close = gainer.get("prev_close", live_price / (1 + gainer["pct_change"] / 100))
                    sizing_position = Position(
                        symbol=symbol, quantity=1, average_price=live_price,
                        atr=entry_atr, prev_close=prev_close,
                    )
                    qty = calculate_position_size(
                        capital=total_capital,
                        entry_price=live_price,
                        stop_price=sizing_position.hard_stop,
                        risk_per_trade_pct=settings.risk_per_trade_pct,
                        maximum_notional=effective_capital,
                    )
                    _record_decision(
                        "position_sized",
                        symbol=symbol,
                        token=str(gainer["token"]),
                        decision=str(qty),
                        reason="risk-based sizing",
                        payload={
                            "capital": total_capital,
                            "entry_price": live_price,
                            "stop_price": sizing_position.hard_stop,
                            "risk_per_trade_pct": settings.risk_per_trade_pct,
                            "maximum_notional": effective_capital,
                            "quantity": qty,
                        },
                    )
                    if qty <= 0:
                        _notify(
                            f"Skipping {symbol}: live price {live_price:.2f} exceeds "
                            f"available capital {effective_capital:.2f} "
                            f"(planned={capital_per_stock:.2f}, product={stock_product_type})",
                            logger, True,
                        )
                        continue

                    order = order_manager.place_market_order(
                        symbol, "BUY", qty, instrument=instrument,
                        current_price=live_price,
                        product_type=stock_product_type,
                    )

                    execution = order_manager.wait_for_fill(order, symbol, qty, timeout=20.0)
                    if not execution.is_filled:
                        # Never infer a fill from a timeout. A partial fill is
                        # adopted and protected; unknown state blocks this entry.
                        if execution.filled_quantity <= 0:
                            send_critical_alert(
                                f"Entry {execution.order_id or '(no id)'} for {symbol} is "
                                f"{execution.state.value}; no further action until reconciled."
                            )
                            try:
                                position_tracker.reconcile(broker.get_positions())
                            except Exception as reconcile_exc:
                                logger.critical("Entry reconciliation failed for %s: %s", symbol, reconcile_exc)
                            continue
                        logger.warning(
                            "Partial entry fill for %s: requested=%d filled=%d",
                            symbol, qty, execution.filled_quantity,
                        )
                        qty = execution.filled_quantity

                    fill_price = execution.average_price or live_price

                    if qty <= 0:
                        _notify(
                            f"SKIPPED {symbol}: broker did not confirm a positive filled quantity.",
                            logger, True,
                        )
                        continue

                    pos = position_tracker.update_buy(
                        symbol, qty, fill_price, atr=entry_atr, prev_close=prev_close,
                        product_type=stock_product_type, token=gainer["token"],
                    )
                    token_map[symbol] = gainer["token"]
                    product_type_map[symbol] = stock_product_type

                    # Place server-side SL-M order at the hard stop.
                    # SL-M uses the same product type as the entry.
                    slm_id = _place_slm_order(
                        broker, symbol, gainer["token"], qty, pos.hard_stop, logger,
                        product_type=stock_product_type,
                    )
                    if slm_id:
                        slm_orders[symbol] = slm_id
                        slm_triggers[symbol] = pos.hard_stop
                        position_tracker.set_protective_order(symbol, slm_id)
                    else:
                        send_critical_alert(f"No protective order for {symbol}; attempting immediate exit.")
                        emergency = _safe_exit(
                            broker, order_manager, symbol, gainer["token"], qty,
                            fill_price, logger, product_type=stock_product_type,
                        )
                        if emergency.confirmed_closed:
                            position_tracker.update_sell(symbol, qty)
                            continue

                    _record_decision(
                        "position_opened",
                        symbol=symbol,
                        token=str(gainer["token"]),
                        decision="protected" if slm_id else "unprotected",
                        reason="broker-confirmed entry fill",
                        payload={
                            "position": asdict(pos),
                            "entry_execution": asdict(execution),
                            "protective_order_id": slm_id or "",
                        },
                    )

                    entered_symbols.append(symbol)
                    traded_today.add(symbol)
                    risk_state.record_entry(symbol)

                    atr_pct = (entry_atr / live_price) * 100
                    pt_label = "CNC" if stock_product_type == "DELIVERY" else "MIS"
                    _notify(
                        f"{round_label}ENTRY: {symbol} [{pt_label}] — {qty} shares @ {fill_price:.2f} "
                        f"(capital={effective_capital:.2f}, gain={gainer['pct_change']:+.2f}%, "
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
            last_exit_reason = ""          # Track exit reason for smart re-entry gates.
            round_pnl = 0.0               # Track P&L for this round's trades.

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
                    result = data_broker.get_market_data("FULL", {"NSE": open_tokens})
                    fetched_quotes = {
                        str(q.get("symbolToken", "")): q for q in result.get("fetched", [])
                    }
                except Exception as exc:
                    logger.warning("Price fetch failed: %s", exc)
                    time.sleep(settings.monitor_interval_seconds)
                    continue

                # Refresh session periodically during monitoring.
                try:
                    data_broker.refresh_if_stale(max_age_seconds=7200)
                    if use_dhan and not settings.paper_trade:
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
                        position_before = asdict(pos)
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
                                    product_type=product_type_map.get(symbol, "INTRADAY"),
                                )
                                if ok:
                                    slm_triggers[symbol] = pos.stop_loss

                        # Use best bid for exit decision (conservative).
                        should_exit = position_tracker.should_exit(
                            symbol, stop_check_price
                        )
                        exit_reason = (
                            position_tracker.get_exit_reason(symbol, stop_check_price)
                            if should_exit
                            else ""
                        )
                        shadow_stop = position_tracker.shadow_staged_stop(pos)
                        _record_decision(
                            "position_evaluated",
                            symbol=symbol,
                            token=str(token),
                            decision="exit" if should_exit else "hold",
                            reason=exit_reason,
                            exchange_timestamp=str(quote.get("exchFeedTime", "")),
                            payload={
                                "quote": quote,
                                "stop_check_price": stop_check_price,
                                "position_before": position_before,
                                "position_after": asdict(pos),
                                "shadow_staged_protection": {
                                    "policy": "mfe_1_2_3_v1",
                                    "stop": shadow_stop,
                                    "decision": (
                                        "exit"
                                        if shadow_stop is not None
                                        and stop_check_price <= shadow_stop
                                        else "hold"
                                    ),
                                    "active": shadow_stop is not None,
                                },
                            },
                        )
                        if should_exit:

                            # SL-M race condition check: if the exchange already
                            # executed our SL-M, skip the software exit.
                            slm_id = slm_orders.get(symbol)
                            if slm_id and _check_slm_executed(broker, slm_id, symbol, logger):
                                # SL-M already sold the shares. Clean up tracking.
                                slm_execution = order_manager.wait_for_fill(
                                    {"orderid": slm_id}, symbol, pos.quantity, timeout=2.0,
                                )
                                exit_price = slm_execution.average_price or stop_check_price
                                slm_orders.pop(symbol, None)
                                slm_triggers.pop(symbol, None)
                                pnl = (exit_price - pos.average_price) * pos.quantity
                                _record_decision(
                                    "confirmed_fill",
                                    symbol=symbol,
                                    token=str(token),
                                    decision="filled",
                                    reason="protective order broker-confirmed fill",
                                    payload={
                                        "order_id": slm_id,
                                        "side": "SELL",
                                        "reference_price": pos.stop_loss,
                                        "fill_price": exit_price,
                                        "filled_quantity": pos.quantity,
                                    },
                                )
                                risk_state.record_exit(pnl)
                                daily_pnl = risk_state.realized_pnl
                                round_pnl += pnl
                                last_exit_reason = exit_reason
                                position_tracker.update_sell(symbol, pos.quantity)
                                _record_decision(
                                    "position_closed",
                                    symbol=symbol,
                                    token=str(token),
                                    decision="closed",
                                    reason=f"{exit_reason}: protective order filled",
                                    payload={
                                        "quantity": pos.quantity,
                                        "entry_price": pos.average_price,
                                        "exit_price": exit_price,
                                        "pnl": pnl,
                                        "order_id": slm_id,
                                    },
                                )
                                consecutive_losses = risk_state.consecutive_losses
                                _notify(
                                    f"{exit_reason} EXIT (SL-M executed): {symbol} — {pos.quantity} shares "
                                    f"(entry={pos.average_price:.2f}, PnL={pnl:+.2f}, daily={daily_pnl:+.2f})",
                                    logger, True,
                                )
                                continue

                            # Double-check: verify we actually hold shares before selling.
                            # This catches the case where SL-M executed but order book
                            # status hasn't updated yet (race condition).
                            actual_qty = _check_broker_position(broker, symbol, logger)
                            if actual_qty == 0:
                                logger.info(
                                    "[exit_skip] %s: position already closed (SL-M executed). "
                                    "Skipping software exit.",
                                    symbol,
                                )
                                slm_orders.pop(symbol, None)
                                slm_triggers.pop(symbol, None)
                                pnl = (stop_check_price - pos.average_price) * pos.quantity
                                risk_state.record_exit(pnl)
                                daily_pnl = risk_state.realized_pnl
                                round_pnl += pnl
                                last_exit_reason = exit_reason
                                position_tracker.update_sell(symbol, pos.quantity)
                                _record_decision(
                                    "position_closed",
                                    symbol=symbol,
                                    token=str(token),
                                    decision="broker_already_closed",
                                    reason=exit_reason,
                                    payload={
                                        "quantity": pos.quantity,
                                        "entry_price": pos.average_price,
                                        "estimated_exit_price": stop_check_price,
                                        "pnl": pnl,
                                    },
                                )
                                consecutive_losses = risk_state.consecutive_losses
                                _notify(
                                    f"{exit_reason} EXIT (position already closed): {symbol} — "
                                    f"{pos.quantity} shares (entry={pos.average_price:.2f}, "
                                    f"PnL={pnl:+.2f}, daily={daily_pnl:+.2f})",
                                    logger, True,
                                )
                                continue

                            # Use actual broker quantity if it differs from our tracking.
                            sell_qty = actual_qty if actual_qty > 0 else pos.quantity

                            exit_outcome = _safe_exit(
                                broker, order_manager, symbol, token,
                                sell_qty, stop_check_price, logger,
                                product_type=product_type_map.get(symbol, "INTRADAY"),
                            )
                            if not exit_outcome.confirmed_closed:
                                logger.critical(
                                    "Exit for %s is not confirmed; retaining position and protective order",
                                    symbol,
                                )
                                if exit_outcome.execution and exit_outcome.execution.filled_quantity > 0:
                                    position_tracker.reconcile(broker.get_positions())
                                continue

                            # Keep exchange protection active until the replacement
                            # exit is confirmed, then cancel the now-redundant stop.
                            if slm_id:
                                _cancel_slm_order(broker, slm_id, symbol, logger)
                            slm_orders.pop(symbol, None)
                            slm_triggers.pop(symbol, None)

                            exit_price = (
                                exit_outcome.execution.average_price
                                if exit_outcome.execution and exit_outcome.execution.average_price > 0
                                else stop_check_price
                            )
                            pnl = (exit_price - pos.average_price) * pos.quantity
                            risk_state.record_exit(pnl)
                            daily_pnl = risk_state.realized_pnl
                            round_pnl += pnl
                            last_exit_reason = exit_reason
                            position_tracker.update_sell(symbol, pos.quantity)
                            _record_decision(
                                "position_closed",
                                symbol=symbol,
                                token=str(token),
                                decision="closed",
                                reason=exit_reason,
                                payload={
                                    "quantity": pos.quantity,
                                    "entry_price": pos.average_price,
                                    "exit_price": exit_price,
                                    "pnl": pnl,
                                    "execution": (
                                        asdict(exit_outcome.execution)
                                        if exit_outcome.execution
                                        else None
                                    ),
                                },
                            )
                            consecutive_losses = risk_state.consecutive_losses

                            _notify(
                                f"{exit_reason} EXIT: {symbol} — {pos.quantity} shares @ {exit_price:.2f} "
                                f"(entry={pos.average_price:.2f}, high={pos.highest_price:.2f}, "
                                f"SL={pos.stop_loss:.2f}, hard_stop={pos.hard_stop:.2f}, ATR={pos.atr:.4f}, "
                                f"locked={pos.profit_locked}, PnL={pnl:+.2f}, daily_pnl={daily_pnl:+.2f}, "
                                f"consecutive_losses={consecutive_losses}/{settings.max_consecutive_losses}, "
                                f"round={reentry_round}/{max_rounds_today}) | {exit_outcome}",
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

            # --- Smart re-entry gates ---
            # Gate 1: If last exit was HARD STOP, market is rejecting momentum — stop.
            if "HARD STOP" in last_exit_reason:
                _notify(
                    "Smart gate: last exit was HARD STOP — skipping re-entry.",
                    logger, True,
                )
                break

            # Gate 2: Only re-enter if this round was profitable.
            if round_pnl < 0:
                _notify(
                    f"Smart gate: round P&L was ₹{round_pnl:+,.2f} — skipping re-entry.",
                    logger, True,
                )
                break

            # Gate 3: No re-entry after 12:30.
            now_check = _ist_now()
            if now_check.hour > 12 or (now_check.hour == 12 and now_check.minute >= 30):
                _notify(
                    "Smart gate: past 12:30 — skipping re-entry.",
                    logger, True,
                )
                break

            # Gate 4: Only re-enter after PROFIT_LOCK — confirms strong momentum day.
            if "PROFIT LOCK" not in last_exit_reason:
                _notify(
                    f"Smart gate: last exit was {last_exit_reason}, not PROFIT LOCK — skipping re-entry.",
                    logger, True,
                )
                break

            # Check daily P&L limit before re-entering.
            if daily_pnl <= -max_daily_loss:
                send_daily_loss_alert(daily_pnl, -max_daily_loss)
                _notify(
                    f"Daily loss limit breached: ₹{daily_pnl:+,.2f} (limit ₹{-max_daily_loss:,.2f}). "
                    f"No new trades.",
                    logger, True,
                )
                break

            reentry_round += 1
            if reentry_round > max_rounds_today:
                _notify(
                    f"Max re-entry rounds ({max_rounds_today}) reached. Done for today.",
                    logger, True,
                )
                break

            # Loop back to scan → enter → monitor.

        # --- Phase 6: Force-close any remaining positions at market close ---
        # Refresh sessions before force-close to avoid stale-token failures.
        try:
            data_broker.refresh_if_stale(max_age_seconds=300)
            if use_dhan and not settings.paper_trade:
                broker.refresh_if_stale(max_age_seconds=300)
        except Exception as exc:
            logger.error("[force_close] Session refresh failed: %s", exc)

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
                        slm_execution = order_manager.wait_for_fill(
                            {"orderid": slm_id}, symbol, pos.quantity, timeout=2.0,
                        )
                        slm_exit_price = slm_execution.average_price or pos.stop_loss
                        pnl = (slm_exit_price - pos.average_price) * pos.quantity
                        _record_decision(
                            "confirmed_fill",
                            symbol=symbol,
                            token=str(token),
                            decision="filled",
                            reason="protective order broker-confirmed fill",
                            payload={
                                "order_id": slm_id,
                                "side": "SELL",
                                "reference_price": pos.stop_loss,
                                "fill_price": slm_exit_price,
                                "filled_quantity": pos.quantity,
                            },
                        )
                        risk_state.record_exit(pnl)
                        daily_pnl = risk_state.realized_pnl
                        slm_orders.pop(symbol, None)
                        slm_triggers.pop(symbol, None)
                        position_tracker.update_sell(symbol, pos.quantity)
                        logger.info(
                            "SL-M already executed for %s @ %.2f; P&L %.2f",
                            symbol, slm_exit_price, pnl,
                        )
                        continue

                    # Verify we actually hold shares before force-closing.
                    actual_qty = _check_broker_position(broker, symbol, logger)
                    if actual_qty == 0:
                        logger.info("Position already closed for %s — skipping force close.", symbol)
                        slm_orders.pop(symbol, None)
                        slm_triggers.pop(symbol, None)
                        position_tracker.update_sell(symbol, pos.quantity)
                        continue

                    # Get final price for exit order and PnL.
                    try:
                        result = data_broker.get_market_data("FULL", {"NSE": [token]})
                        fetched_list = result.get("fetched", [{}])
                        final_price = float(fetched_list[0].get("ltp", 0)) if fetched_list else 0.0
                    except Exception:
                        final_price = 0.0

                    sell_qty = actual_qty if actual_qty > 0 else pos.quantity
                    exit_outcome = _safe_exit(
                        broker, order_manager, symbol, token,
                        sell_qty, final_price, logger,
                        product_type=pos.product_type,
                    )
                    if not exit_outcome.confirmed_closed:
                        logger.critical(
                            "Market-close exit for %s is not confirmed; position remains tracked",
                            symbol,
                        )
                        if exit_outcome.execution and exit_outcome.execution.filled_quantity > 0:
                            position_tracker.reconcile(broker.get_positions())
                        continue

                    if slm_id:
                        _cancel_slm_order(broker, slm_id, symbol, logger)
                    slm_orders.pop(symbol, None)
                    slm_triggers.pop(symbol, None)

                    exit_price = (
                        exit_outcome.execution.average_price
                        if exit_outcome.execution and exit_outcome.execution.average_price > 0
                        else final_price
                    )
                    pnl = (exit_price - pos.average_price) * pos.quantity
                    risk_state.record_exit(pnl)
                    daily_pnl = risk_state.realized_pnl
                    position_tracker.update_sell(symbol, pos.quantity)
                    pt_label = "CNC" if pos.product_type == "DELIVERY" else "MIS"
                    _notify(
                        f"MARKET CLOSE EXIT [{pt_label}]: {symbol} — {pos.quantity} shares @ {exit_price:.2f} "
                        f"(entry={pos.average_price:.2f}, PnL={pnl:+.2f}, daily={daily_pnl:+.2f}) | {exit_outcome}",
                        logger, True,
                    )
                except Exception as exc:
                    logger.exception("[critical_failure] Failed to close %s: %s", symbol, exc)
                    send_exit_failure_alert(symbol, pos.quantity, str(exc))

        # Print paper trading summary if in simulation mode.
        if settings.paper_trade and isinstance(broker, PaperBrokerClient):
            summary = broker.print_summary()
            send_telegram_message(summary)
            broker.reset_daily()

        _write_replay_reports(logger)

        # Sleep until next day's scan window instead of a fixed 1 hour.
        # This prevents the outer loop from restarting while the market is
        # still open (which caused double trading cycles).
        now_end = _ist_now()
        sleep_secs = _seconds_until_next_scan(now_end)
        _notify(
            f"Trading day complete. Daily P&L: ₹{daily_pnl:+,.2f}. "
            f"Sleeping {sleep_secs/3600:.1f}h until tomorrow.",
            logger, True,
        )
        time.sleep(sleep_secs)


if __name__ == "__main__":
    run_loop()

"""Wide trailing stop designed for holding momentum stocks for big gains.

Uses fixed-percentage trailing stops that stay wide to let winners run.
Targets 13-18% gains per trade instead of many small trades.
Profit lock activates at 12% intraday gain with a 2% trail.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# --- Stop configuration ---
INITIAL_ATR_MULT = 3.0       # Initial stop distance: 3x ATR below entry.
HARD_MAX_LOSS_PCT = 0.02     # Absolute max loss per stock: 2% below entry.
MIN_STOP_DISTANCE_PCT = 0.005  # Stop never tighter than 0.5% (avoid instant stop-out).

# Fixed-percentage trailing stops: wide enough to survive normal pullbacks.
# As profit grows, the trail stays wide so the position can breathe.
# (min_profit_pct, trail_pct_from_high)
TRAIL_TIERS = [
    (0.10, 0.025),  # >10% profit: trail 2.5% below high — protect big gains.
    (0.06, 0.030),  # 6-10% profit: trail 3% below high — let it run.
    (0.03, 0.030),  # 3-6% profit: trail 3% below high.
    (0.02, 0.030),  # 2-3% profit: trail 3% below high — tighter to reduce loss on reversal.
    (0.00, 0.040),  # <2% profit: trail 4% below high — very wide, give room.
]

# Target exit: take profit at this level (from entry price).
TARGET_PROFIT_PCT = 0.15     # Exit at 15% profit from entry.

# Time-based tightening: after this hour (IST), reduce trail distances.
LATE_SESSION_HOUR = 14
LATE_SESSION_MIN = 30
LATE_SESSION_TRAIL_REDUCTION = 0.005  # Tighten trail by 0.5% in late session.

# --- Intraday gain-based profit lock ---
# Triggered by the stock's gain from previous close, NOT profit from entry.
# Example: enter at +5%, stock hits +12% for the day → lock activates.
INTRADAY_LOCK_THRESHOLD = 0.12   # Lock when stock's intraday gain >= 12%.
INTRADAY_LOCK_FLOOR_PCT = 0.10   # Stop never below prev_close * 1.10 once locked.
INTRADAY_LOCK_TRAIL_PCT = 0.02   # Trail at 2% of highest price inside lock zone.

# Shadow-only observation fixture. It is recorded for later replay comparison
# and never replaces the active production stop.
SHADOW_STAGED_STOP_FLOORS = (
    (0.01, -0.0125),
    (0.02, -0.0035),
    (0.03, 0.01),
)


@dataclass
class Position:
    """One tracked position with ATR-adaptive stop."""

    symbol: str
    quantity: int
    average_price: float       # Our entry price.
    atr: float                 # 5-minute ATR at entry time (fixed for the trade).
    prev_close: float          # Stock's previous day close — used for intraday gain calc.
    product_type: str = "INTRADAY"  # "INTRADAY" (MIS, 5x) or "DELIVERY" (CNC, 1x).
    highest_price: float = 0.0
    stop_loss: float = 0.0
    hard_stop: float = 0.0
    profit_locked: bool = False  # True once intraday gain crosses lock_threshold.
    lock_threshold: float = INTRADAY_LOCK_THRESHOLD   # Dynamic: 14% for 8-10% entry stocks.
    lock_floor_pct: float = INTRADAY_LOCK_FLOOR_PCT   # Dynamic: 12% for 8-10% entry stocks.
    token: str = ""
    protective_order_id: str = ""

    def __post_init__(self) -> None:
        if self.highest_price == 0.0:
            self.highest_price = self.average_price
        if self.stop_loss == 0.0:
            self._set_initial_stop()
        if self.hard_stop == 0.0:
            self._set_hard_stop()

    def _set_initial_stop(self) -> None:
        """Set initial stop at entry - INITIAL_ATR_MULT x ATR, clamped."""
        atr_distance = self.atr * INITIAL_ATR_MULT
        pct_distance = self.average_price * HARD_MAX_LOSS_PCT
        min_distance = self.average_price * MIN_STOP_DISTANCE_PCT

        distance = max(min(atr_distance, pct_distance), min_distance)
        self.stop_loss = round(self.average_price - distance, 2)

    def _set_hard_stop(self) -> None:
        """Hard stop: tighter of ATR-based and percentage-based."""
        atr_hard = self.average_price - (self.atr * INITIAL_ATR_MULT)
        pct_hard = self.average_price * (1 - HARD_MAX_LOSS_PCT)
        self.hard_stop = round(max(atr_hard, pct_hard), 2)

    def intraday_gain_pct(self, current_price: float) -> float:
        """Stock's intraday gain from previous close."""
        if self.prev_close <= 0:
            return 0.0
        return (current_price - self.prev_close) / self.prev_close


class PositionTracker:
    """Persistent position tracker reconciled against the broker at startup."""

    def __init__(self, state_path: str | Path | None = None) -> None:
        self._positions: dict[str, Position] = {}
        self._state_path = Path(state_path) if state_path else None
        self._load()

    def _load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            for raw in payload.get("positions", []):
                pos = Position(**raw)
                self._positions[pos.symbol] = pos
        except (OSError, ValueError, TypeError):
            # Corrupt state must not silently create guessed positions. Broker
            # reconciliation will repopulate real holdings before trading.
            self._positions = {}

    def _persist(self) -> None:
        if not self._state_path:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        payload = {
            "version": 1,
            "updated_at": datetime.now(IST).isoformat(),
            "positions": [asdict(pos) for pos in self._positions.values()],
        }
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self._state_path)

    def update_buy(
        self, symbol: str, quantity: int, price: float,
        atr: float, prev_close: float, product_type: str = "INTRADAY",
        lock_threshold: float = INTRADAY_LOCK_THRESHOLD,
        lock_floor_pct: float = INTRADAY_LOCK_FLOOR_PCT,
        token: str = "",
    ) -> Position:
        """Record a buy. ATR and prev_close are computed at entry time."""
        existing = self._positions.get(symbol)
        if not existing:
            pos = Position(
                symbol=symbol, quantity=quantity,
                average_price=price, atr=atr, prev_close=prev_close,
                product_type=product_type,
                lock_threshold=lock_threshold,
                lock_floor_pct=lock_floor_pct,
                token=token,
            )
            self._positions[symbol] = pos
            self._persist()
            return pos

        # Scale-in: recalculate weighted average, keep original ATR and prev_close.
        new_qty = existing.quantity + quantity
        weighted_avg = (
            (existing.average_price * existing.quantity) + (price * quantity)
        ) / new_qty
        existing.quantity = new_qty
        existing.average_price = weighted_avg
        existing.highest_price = max(existing.highest_price, price)

        new_stop = self._compute_trail_stop(existing)
        if new_stop > existing.stop_loss:
            existing.stop_loss = new_stop
        existing._set_hard_stop()
        if token:
            existing.token = token
        self._persist()
        return existing

    def set_protective_order(self, symbol: str, order_id: str) -> None:
        position = self._positions.get(symbol)
        if not position:
            raise KeyError(f"No tracked position for {symbol}")
        position.protective_order_id = order_id
        self._persist()

    def update_sell(self, symbol: str, quantity: int) -> Position | None:
        """Record a sell (full exit)."""
        existing = self._positions.get(symbol)
        if not existing:
            return None
        if quantity > existing.quantity:
            raise ValueError(
                f"Cannot sell {quantity} shares of {symbol}: only {existing.quantity} held"
            )
        existing.quantity -= quantity
        if existing.quantity == 0:
            del self._positions[symbol]
            self._persist()
            return None
        self._persist()
        return existing

    def update_trailing_stop(self, symbol: str, current_price: float) -> Position | None:
        """Update trailing stop based on current price, ATR tiers, and intraday lock."""
        existing = self._positions.get(symbol)
        if not existing:
            return None

        if current_price > existing.highest_price:
            existing.highest_price = current_price

        new_stop = self._compute_trail_stop(existing)
        if new_stop > existing.stop_loss:
            existing.stop_loss = new_stop

        self._persist()
        return existing

    def should_exit(self, symbol: str, current_price: float) -> bool:
        """Check if price has breached trailing stop, hard stop, or hit target."""
        existing = self._positions.get(symbol)
        if not existing:
            return False
        # Target exit: take profit at TARGET_PROFIT_PCT from entry.
        profit_pct = (current_price - existing.average_price) / existing.average_price
        if profit_pct >= TARGET_PROFIT_PCT:
            return True
        return current_price <= existing.stop_loss or current_price <= existing.hard_stop

    def get_exit_reason(self, symbol: str, current_price: float) -> str:
        """Return a descriptive exit reason for logging."""
        existing = self._positions.get(symbol)
        if not existing:
            return "NO_POSITION"
        # Check target first.
        profit_pct = (current_price - existing.average_price) / existing.average_price
        if profit_pct >= TARGET_PROFIT_PCT:
            return f"TARGET HIT ({profit_pct * 100:.1f}% profit)"
        if current_price <= existing.hard_stop:
            return "HARD STOP (max loss)"
        if current_price <= existing.stop_loss:
            if existing.profit_locked:
                intraday = existing.intraday_gain_pct(current_price) * 100
                return f"PROFIT LOCK EXIT (stock at +{intraday:.1f}% intraday)"
            entry_profit = (existing.highest_price - existing.average_price) / existing.average_price
            tier_label = self._get_tier_label(entry_profit)
            return f"TRAILING STOP ({tier_label})"
        return "NO_EXIT"

    @staticmethod
    def shadow_staged_stop(position: Position) -> float | None:
        """Return the observation stop without mutating production state."""
        if position.average_price <= 0:
            return None
        mfe = (
            position.highest_price - position.average_price
        ) / position.average_price
        floor = None
        for trigger, relative_floor in SHADOW_STAGED_STOP_FLOORS:
            if mfe >= trigger:
                floor = position.average_price * (1 + relative_floor)
        return round(floor, 2) if floor is not None else None

    def snapshot(self) -> dict[str, Position]:
        return dict(self._positions)

    def reconcile(self, broker_positions: dict[str, Any]) -> list[str]:
        """Make local holdings match the broker's positive net positions."""
        rows = broker_positions.get("data") or []
        live: dict[str, dict[str, Any]] = {}
        for row in rows:
            symbol = str(row.get("tradingsymbol") or "")
            try:
                quantity = int(float(row.get("netqty", 0)))
            except (TypeError, ValueError):
                continue
            if symbol and quantity > 0:
                live[symbol] = {**row, "_quantity": quantity}

        changes: list[str] = []
        for symbol in list(self._positions):
            if symbol not in live:
                del self._positions[symbol]
                changes.append(f"removed stale local position {symbol}")

        for symbol, row in live.items():
            quantity = row["_quantity"]
            try:
                average = float(
                    row.get("avgnetprice") or row.get("buyavgprice")
                    or row.get("averageprice") or row.get("ltp") or 0
                )
            except (TypeError, ValueError):
                average = 0.0
            if average <= 0:
                raise ValueError(f"Broker position {symbol} has no usable average price")
            existing = self._positions.get(symbol)
            if existing:
                if existing.quantity != quantity or abs(existing.average_price - average) > 0.01:
                    changes.append(f"updated {symbol} from broker quantity/average")
                existing.quantity = quantity
                existing.average_price = average
                existing.token = str(row.get("symboltoken") or existing.token)
                existing.product_type = str(row.get("producttype") or existing.product_type)
                existing._set_hard_stop()
            else:
                self._positions[symbol] = Position(
                    symbol=symbol, quantity=quantity, average_price=average,
                    atr=max(average * 0.005, 0.01), prev_close=average,
                    product_type=str(row.get("producttype") or "INTRADAY"),
                    token=str(row.get("symboltoken") or ""),
                )
                changes.append(f"adopted untracked broker position {symbol}")

        self._persist()
        return changes

    # --- Internal helpers ---

    def _compute_trail_stop(self, pos: Position) -> float:
        """Compute trailing stop using fixed-percentage trails.

        Uses wide percentage-based trails to let winners run.
        Switches to profit lock mode when intraday gain hits 12%.
        """
        intraday_gain = pos.intraday_gain_pct(pos.highest_price)

        if intraday_gain >= pos.lock_threshold:
            pos.profit_locked = True
            # Floor: stock's price at lock_floor_pct from prev close.
            lock_floor = pos.prev_close * (1 + pos.lock_floor_pct)
            # Trail: 2% below highest price — wide enough to ride to 15-18%.
            lock_trail = pos.highest_price * (1 - INTRADAY_LOCK_TRAIL_PCT)
            return round(max(lock_floor, lock_trail), 2)

        # Fixed-percentage trailing based on profit from entry.
        profit_pct = (pos.highest_price - pos.average_price) / pos.average_price

        trail_pct = self._get_trail_pct(profit_pct)
        trail_distance = pos.highest_price * trail_pct
        min_distance = pos.highest_price * MIN_STOP_DISTANCE_PCT
        trail_distance = max(trail_distance, min_distance)

        return round(pos.highest_price - trail_distance, 2)

    @staticmethod
    def _get_trail_pct(profit_pct: float) -> float:
        """Look up the fixed trail percentage for the current profit level."""
        now = datetime.now(IST)
        late_session = (
            now.hour > LATE_SESSION_HOUR
            or (now.hour == LATE_SESSION_HOUR and now.minute >= LATE_SESSION_MIN)
        )

        for threshold, trail in TRAIL_TIERS:
            if profit_pct >= threshold:
                if late_session:
                    trail = max(trail - LATE_SESSION_TRAIL_REDUCTION, 0.01)
                return trail

        base = TRAIL_TIERS[-1][1]
        if late_session:
            base = max(base - LATE_SESSION_TRAIL_REDUCTION, 0.01)
        return base

    @staticmethod
    def _get_tier_label(profit_pct: float) -> str:
        """Human-readable label for the current trail tier."""
        if profit_pct >= 0.10:
            return "2.5% trail, >10% profit"
        if profit_pct >= 0.06:
            return "3% trail, 6-10% profit"
        if profit_pct >= 0.03:
            return "3% trail, 3-6% profit"
        if profit_pct >= 0.02:
            return "3% trail, 2-3% profit"
        return "4% trail, <2% profit"

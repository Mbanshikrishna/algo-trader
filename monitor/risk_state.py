from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class DailyRiskState:
    """Persistent daily loss controls that survive process restarts."""

    path: Path
    trading_date: str = ""
    realized_pnl: float = 0.0
    traded_symbols: set[str] = field(default_factory=set)
    consecutive_losses: int = 0

    @classmethod
    def load(cls, path: str | Path) -> "DailyRiskState":
        state = cls(path=Path(path))
        try:
            raw = json.loads(state.path.read_text(encoding="utf-8"))
            state.trading_date = str(raw.get("trading_date", ""))
            state.realized_pnl = float(raw.get("realized_pnl", 0))
            state.traded_symbols = set(raw.get("traded_symbols", []))
            state.consecutive_losses = int(raw.get("consecutive_losses", 0))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass
        state.rollover_if_needed()
        return state

    def rollover_if_needed(self, trading_date: str | None = None) -> None:
        today = trading_date or datetime.now(IST).date().isoformat()
        if self.trading_date != today:
            self.trading_date = today
            self.realized_pnl = 0.0
            self.traded_symbols.clear()
            self.consecutive_losses = 0
            self.save()

    def record_entry(self, symbol: str) -> None:
        self.traded_symbols.add(symbol)
        self.save()

    def record_exit(self, pnl: float) -> None:
        self.realized_pnl += pnl
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({
            "version": 1,
            "trading_date": self.trading_date,
            "realized_pnl": self.realized_pnl,
            "traded_symbols": sorted(self.traded_symbols),
            "consecutive_losses": self.consecutive_losses,
        }, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def loss_limit_breached(self, capital: float, max_daily_loss_pct: float) -> bool:
        return self.realized_pnl <= -(capital * max_daily_loss_pct / 100.0)


def calculate_position_size(
    capital: float,
    entry_price: float,
    stop_price: float,
    risk_per_trade_pct: float,
    maximum_notional: float,
) -> int:
    """Size by stop distance, capped by available buying power."""
    risk_per_share = entry_price - stop_price
    if capital <= 0 or entry_price <= 0 or risk_per_share <= 0 or maximum_notional <= 0:
        return 0
    risk_budget = capital * risk_per_trade_pct / 100.0
    by_risk = int(risk_budget // risk_per_share)
    by_capital = int(maximum_notional // entry_price)
    return max(min(by_risk, by_capital), 0)

from __future__ import annotations


class RiskManager:
    """Position sizing based on max capital risk per trade."""

    def __init__(self, capital: float, risk_per_trade_pct: float = 1.0) -> None:
        self.capital = capital
        self.risk_per_trade_pct = risk_per_trade_pct

    def position_size(self, entry_price: float, stop_loss: float) -> int:
        risk_per_share = max(entry_price - stop_loss, 0)
        if risk_per_share <= 0:
            return 0

        risk_amount = self.capital * (self.risk_per_trade_pct / 100)
        qty = int(risk_amount // risk_per_share)
        return max(qty, 0)

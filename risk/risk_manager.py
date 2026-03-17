from __future__ import annotations  # Lets Python postpone evaluation of type annotations.


class RiskManager:  # Defines logic for sizing positions based on allowed trade risk.
    """Position sizing based on max capital risk per trade."""

    def __init__(self, capital: float, risk_per_trade_pct: float = 1.0) -> None:  # Initializes the risk model with account capital and per-trade risk.
        self.capital = capital  # Stores the total capital available for trading.
        self.risk_per_trade_pct = risk_per_trade_pct  # Stores what percentage of capital can be risked on a single trade.

    def position_size(self, entry_price: float, stop_loss: float) -> int:  # Calculates the number of shares allowed for a trade.
        risk_per_share = max(entry_price - stop_loss, 0)  # Calculates risk per share while preventing negative values.
        if risk_per_share <= 0:  # Handles invalid setups where entry is not above stop-loss.
            return 0  # Returns zero so no trade is taken.

        risk_amount = self.capital * (self.risk_per_trade_pct / 100)  # Converts the configured risk percentage into a currency amount.
        qty = int(risk_amount // risk_per_share)  # Uses floor division to compute a whole-share quantity within the risk budget.
        return max(qty, 0)  # Returns the quantity, protecting against negative outputs.

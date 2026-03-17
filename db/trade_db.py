from __future__ import annotations

import sqlite3
from pathlib import Path


class TradeDB:
    def __init__(self, db_path: str = "trades.db") -> None:
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def log_trade(self, symbol: str, side: str, quantity: int, price: float, status: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO trades (symbol, side, quantity, price, status) VALUES (?, ?, ?, ?, ?)",
                (symbol, side, quantity, price, status),
            )

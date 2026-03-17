from __future__ import annotations  # Lets Python postpone evaluation of type hints.

import sqlite3  # Imports SQLite support for local database operations.
from pathlib import Path  # Imports Path for cleaner file path handling.


class TradeDB:  # Defines a helper class for creating and writing to the trades database.
    def __init__(self, db_path: str = "trades.db") -> None:  # Initializes the database helper with an optional file path.
        self.db_path = Path(db_path)  # Converts the database path string into a Path object.
        self._init_db()  # Ensures the database schema exists before logging trades.

    def _init_db(self) -> None:  # Creates the trades table if it does not already exist.
        with sqlite3.connect(self.db_path) as conn:  # Opens a connection to the SQLite database file.
            conn.execute(  # Runs the SQL statement that creates the trades table.
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
            )  # Finishes the schema creation command.

    def log_trade(self, symbol: str, side: str, quantity: int, price: float, status: str) -> None:  # Inserts one trade record into the database.
        with sqlite3.connect(self.db_path) as conn:  # Opens a database connection for the insert operation.
            conn.execute(  # Executes the parameterized insert statement.
                "INSERT INTO trades (symbol, side, quantity, price, status) VALUES (?, ?, ?, ?, ?)",  # Defines the SQL query used to store a trade.
                (symbol, side, quantity, price, status),  # Supplies the actual trade values to the query safely.
            )  # Finishes the insert command.

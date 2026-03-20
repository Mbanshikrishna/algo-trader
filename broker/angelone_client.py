from __future__ import annotations  # Lets Python postpone evaluation of type annotations.

import requests


class AngelOneClient:  # Defines a lightweight wrapper around a future Angel One SmartAPI integration.
    """Angel One SmartAPI integration.

    All methods in this wrapper are safe to call in paper trade mode by first checking `paper_trade`.
    """

    BASE_URL = "https://apiconnect.angelbroking.com/rest/secure/angeltrade/v1"

    def __init__(
        self,
        api_key: str,
        client_id: str,
        access_token: str,
        paper_trade: bool = True,
    ) -> None:  # Stores the broker credentials needed for API access.
        self.api_key = api_key
        self.client_id = client_id
        self.access_token = access_token
        self.paper_trade = paper_trade
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "X-Client-Code": self.client_id,
            "X-API-Key": self.api_key,
            "Authorization": f"Bearer {self.access_token}",
        })

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.BASE_URL}{path}"
        response = self.session.get(url, params=params or {})
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.BASE_URL}{path}"
        response = self.session.post(url, json=body)
        response.raise_for_status()
        return response.json()

    def place_order(self, order_payload: dict) -> dict:
        if self.paper_trade:
            return {"broker": "angelone", "status": "PAPER_FILLED", **order_payload}

        # SmartAPI order placement endpoint may vary by plan; adjust as needed.
        return self._post("/order/placeOrder", order_payload)

    def get_order_book(self) -> dict:
        """Fetches current active orders for the account."""
        if self.paper_trade:
            return {"broker": "angelone", "orders": []}
        return self._get("/order/book")

    def get_trade_book(self) -> dict:
        """Fetches executed trade history (fills) for the account."""
        if self.paper_trade:
            return {"broker": "angelone", "trades": []}
        return self._get("/trade/book")

    def get_holdings(self) -> dict:
        """Fetches current holdings from the account."""
        if self.paper_trade:
            return {"broker": "angelone", "holdings": []}
        return self._get("/portfolio/holdings")

    def get_historical_orders(self, from_date: str = "", to_date: str = "") -> dict:
        """Fetches historical orders between dates (yyyy-mm-dd)."""
        if self.paper_trade:
            return {"broker": "angelone", "historical_orders": []}

        # Some SmartAPI versions use query params for from/to.
        params = {}
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date

        return self._get("/order/history", params=params)

    def get_historical_trades(self, from_date: str = "", to_date: str = "") -> dict:
        """Fetches historical trades between dates (yyyy-mm-dd)."""
        if self.paper_trade:
            return {"broker": "angelone", "historical_trades": []}

        params = {}
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date

        return self._get("/trade/history", params=params)


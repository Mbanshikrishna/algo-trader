from __future__ import annotations


class KiteClient:
    """Minimal Zerodha (Kite) client wrapper placeholder.

    Replace methods with real `kiteconnect` integration once credentials/session flow is ready.
    """

    def __init__(self, api_key: str, api_secret: str, access_token: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token

    def place_order(self, order_payload: dict) -> dict:
        # Stubbed response to keep project runnable without live credentials.
        return {"broker": "zerodha", "status": "SENT", **order_payload}

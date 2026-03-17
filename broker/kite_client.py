from __future__ import annotations  # Lets Python postpone evaluation of type annotations.


class KiteClient:  # Defines a lightweight wrapper around a future Zerodha Kite integration.
    """Minimal Zerodha (Kite) client wrapper placeholder.

    Replace methods with real `kiteconnect` integration once credentials/session flow is ready.
    """

    def __init__(self, api_key: str, api_secret: str, access_token: str) -> None:  # Stores the broker credentials needed for API access.
        self.api_key = api_key  # Saves the Zerodha API key on the client instance.
        self.api_secret = api_secret  # Saves the Zerodha API secret on the client instance.
        self.access_token = access_token  # Saves the session access token on the client instance.

    def place_order(self, order_payload: dict) -> dict:  # Accepts an order payload and returns a broker-style response.
        # Stubbed response to keep project runnable without live credentials.
        return {"broker": "zerodha", "status": "SENT", **order_payload}  # Returns a fake successful order response merged with the original payload.

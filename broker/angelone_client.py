from __future__ import annotations  # Lets Python postpone evaluation of type annotations.


class AngelOneClient:  # Defines a lightweight wrapper around a future Angel One SmartAPI integration.
    """Minimal Angel One client wrapper placeholder.

    Replace methods with a real SmartAPI integration once credentials/session flow is ready.
    """

    def __init__(self, api_key: str, client_id: str, access_token: str) -> None:  # Stores the broker credentials needed for API access.
        self.api_key = api_key  # Saves the Angel One API key on the client instance.
        self.client_id = client_id  # Saves the Angel One client identifier on the client instance.
        self.access_token = access_token  # Saves the session access token on the client instance.

    def place_order(self, order_payload: dict) -> dict:  # Accepts an order payload and returns a broker-style response.
        # Stubbed response to keep project runnable without live credentials.
        return {"broker": "angelone", "status": "SENT", **order_payload}  # Returns a fake successful order response merged with the original payload.

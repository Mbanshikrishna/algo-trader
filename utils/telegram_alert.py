from __future__ import annotations  # Lets Python postpone evaluation of type hints.

import os  # Imports access to environment variables.
from urllib.parse import quote_plus  # Imports URL encoding for safe message transmission.
from urllib.request import urlopen  # Imports a simple HTTP client for Telegram requests.


def send_telegram_message(message: str) -> bool:  # Sends a Telegram message when bot credentials are configured.
    """Send a Telegram alert if bot token/chat id are configured."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")  # Reads the Telegram bot token from the environment.
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")  # Reads the Telegram target chat ID from the environment.
    if not bot_token or not chat_id:  # Checks whether Telegram credentials are missing.
        return False  # Returns False when alerts cannot be sent.

    encoded = quote_plus(message)  # URL-encodes the message text so it can be sent safely in the query string.
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage?chat_id={chat_id}&text={encoded}"  # Builds the Telegram Bot API request URL.
    with urlopen(url, timeout=10) as response:  # nosec B310 - fixed Telegram endpoint
        return response.status == 200  # Returns True only if Telegram responds with HTTP 200.

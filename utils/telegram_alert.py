from __future__ import annotations

import os
from urllib.parse import quote_plus
from urllib.request import urlopen


def send_telegram_message(message: str) -> bool:
    """Send a Telegram alert if bot token/chat id are configured."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return False

    encoded = quote_plus(message)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage?chat_id={chat_id}&text={encoded}"
    with urlopen(url, timeout=10) as response:  # nosec B310 - fixed Telegram endpoint
        return response.status == 200

from __future__ import annotations  # Lets Python postpone evaluation of type hints.

import logging  # Imports logging so alert failures can be reported without crashing the bot.
import os  # Imports access to environment variables.
from urllib.error import HTTPError, URLError  # Imports URL-related errors so Telegram failures can be handled safely.
from urllib.parse import quote_plus  # Imports URL encoding for safe message transmission.
from urllib.request import urlopen  # Imports a simple HTTP client for Telegram requests.

LOGGER = logging.getLogger("algo_trader.telegram")  # Reuses the project logger namespace for Telegram warning messages.
_PLACEHOLDER_VALUES = {"your_bot_token", "your_chat_id"}  # Treats copied example values as effectively unconfigured.
_TELEGRAM_MESSAGE_LIMIT = 4000  # Keeps each request safely below Telegram's message-length limit.


def _is_configured(value: str) -> bool:  # Checks whether a Telegram setting is present and not still using an example placeholder.
    normalized = value.strip()
    return bool(normalized) and normalized not in _PLACEHOLDER_VALUES


def _split_message(message: str, limit: int = _TELEGRAM_MESSAGE_LIMIT) -> list[str]:  # Splits long Telegram messages into line-preserving chunks that fit the API limit.
    normalized = message.strip()
    if not normalized:
        return [""]
    if len(normalized) <= limit:
        return [normalized]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_length = 0
    for line in normalized.splitlines():
        candidate_length = len(line) if not current_lines else current_length + 1 + len(line)
        if current_lines and candidate_length > limit:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_length = len(line)
            continue
        if len(line) > limit:  # Falls back to hard slicing for unusually long single-line payloads.
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_length = 0
            for start in range(0, len(line), limit):
                chunks.append(line[start : start + limit])
            continue
        current_lines.append(line)
        current_length = candidate_length

    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks


def send_telegram_message(message: str) -> bool:  # Sends a Telegram message when bot credentials are configured.
    """Send a Telegram alert if bot token/chat id are configured."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")  # Reads the Telegram bot token from the environment.
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")  # Reads the Telegram target chat ID from the environment.
    if not _is_configured(bot_token) or not _is_configured(chat_id):  # Checks whether Telegram credentials are missing or still set to example values.
        return False  # Returns False when alerts cannot be sent.

    try:
        for chunk in _split_message(message):
            encoded = quote_plus(chunk)  # URL-encodes the message text so it can be sent safely in the query string.
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage?chat_id={chat_id}&text={encoded}"  # Builds the Telegram Bot API request URL.
            with urlopen(url, timeout=10) as response:  # nosec B310 - fixed Telegram endpoint
                if response.status != 200:
                    return False
        return True  # Returns True only if all message chunks were accepted by Telegram.
    except (HTTPError, URLError, OSError) as exc:  # Prevents alert-delivery problems from interrupting trading or scanning.
        LOGGER.warning("Telegram alert failed: %s", exc)
        return False

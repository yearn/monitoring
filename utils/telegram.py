import os

import requests
from dotenv import load_dotenv

from utils.logging import get_logger

load_dotenv()

logger = get_logger("utils.telegram")

# Maximum message length allowed by Telegram API
MAX_MESSAGE_LENGTH = 4096


def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram Markdown V1.

    Telegram Markdown V1 treats _ * ` [ as formatting characters.
    This function escapes them so they render as literal text.
    """
    for ch in r"\_*`[":
        text = text.replace(ch, f"\\{ch}")
    return text


class TelegramError(Exception):
    """Exception raised for errors in Telegram API interactions."""

    pass


def send_telegram_message(
    message: str,
    protocol: str,
    disable_notification: bool = False,
    plain_text: bool = False,
) -> None:
    """
    Send a message to a Telegram chat using a bot.

    Args:
        message: The message to send
        protocol: Protocol identifier used to select bot token and chat ID
        disable_notification: If True, sends the message silently

    Raises:
        TelegramError: If the message fails to send
    """
    logger.debug("Sending telegram message:\n%s", message)

    if os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG":
        logger.debug("Skipping Telegram send (LOG_LEVEL=DEBUG)")
        return

    # Truncate long messages; disable Markdown to avoid broken entities
    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[: MAX_MESSAGE_LENGTH - 3] + "..."
        plain_text = True

    # Check if this protocol has a topic ID configured (forum-style group)
    topic_id = os.getenv(f"TELEGRAM_TOPIC_ID_{protocol.upper()}")

    if topic_id:
        # Topics always use the default bot and the shared topics chat
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN_DEFAULT")
        chat_id = os.getenv("TELEGRAM_CHAT_ID_TOPICS")
    else:
        # Legacy per-protocol chat routing
        bot_token = os.getenv(f"TELEGRAM_BOT_TOKEN_{protocol.upper()}")
        if not bot_token:
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN_DEFAULT")
        chat_id = os.getenv(f"TELEGRAM_CHAT_ID_{protocol.upper()}")

    if not bot_token or not chat_id:
        logger.warning("Missing Telegram credentials for %s", protocol)
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown" if not plain_text else None,
        "disable_notification": disable_notification,
    }
    if topic_id:
        payload["message_thread_id"] = int(topic_id)

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        raise TelegramError(f"Failed to send telegram message: {e}")

    if response.status_code != 200:
        raise TelegramError(f"Failed to send telegram message: {response.status_code} - {response.text}")


def get_github_run_url() -> str:
    """Build a GitHub Actions run URL from environment variables, if available."""
    run_url = os.getenv("GITHUB_RUN_URL", "")
    if not run_url:
        server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
        repo = os.getenv("GITHUB_REPOSITORY", "")
        run_id = os.getenv("GITHUB_RUN_ID", "")
        if repo and run_id:
            run_url = f"{server}/{repo}/actions/runs/{run_id}"
    return run_url


def send_telegram_message_with_fallback(
    message: str,
    protocol: str,
    fallback_message: str,
    max_length: int = 3000,
) -> None:
    """Send a Telegram message, falling back to a shorter message with a log link if too long.

    Args:
        message: The full message to send.
        protocol: Protocol identifier used to select bot token and chat ID.
        fallback_message: Short message to send if the full message exceeds max_length.
            A link to the GitHub Actions run will be appended if available.
        max_length: Maximum character length before switching to fallback_message.
    """
    if len(message) > max_length:
        run_url = get_github_run_url()
        message = fallback_message
        if run_url:
            message += f"\n[Check the full logs]({run_url})"

    send_telegram_message(message, protocol)

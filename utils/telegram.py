import os
import re

import requests
from dotenv import load_dotenv

from utils.logging import get_logger

load_dotenv()

logger = get_logger("utils.telegram")

# Maximum message length allowed by Telegram API
MAX_MESSAGE_LENGTH = 4096

# Matches `bot<digits>:<token>` in Telegram API URLs. Used to scrub the bot
# token out of exception messages — `requests.HTTPError.__str__()` includes
# the full URL, so without this the token leaks into any log or alert that
# surfaces the error. GitHub Actions auto-masks secrets in workflow logs,
# but local runs and any Telegram crash-alert do not.
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def _redact_bot_token(text: str) -> str:
    return _BOT_TOKEN_RE.sub("bot***", text)


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


def _post_message(
    bot_token: str,
    chat_id: str,
    message: str,
    plain_text: bool,
    disable_notification: bool,
    topic_id: str | None = None,
) -> None:
    """Send a single message to Telegram, raising TelegramError on failure."""
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
        # Telegram's response body carries the real failure reason
        # (e.g. "can't parse entities", invalid message_thread_id). Surface it
        # so callers don't have to debug from just an HTTP status.
        body = ""
        err_response = getattr(e, "response", None)
        if err_response is not None:
            try:
                body = f" body={err_response.text}"
            except Exception:
                pass
        raise TelegramError(_redact_bot_token(f"Failed to send telegram message: {e}{body}"))

    if response.status_code != 200:
        raise TelegramError(
            _redact_bot_token(f"Failed to send telegram message: {response.status_code} - {response.text}")
        )


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

    # Test/staging override: route every message to one chat for comparison runs.
    # Set TELEGRAM_TEST_CHAT_ID to a dummy group id; unset to restore prod routing.
    # The DEFAULT bot must be a member of that group. The per-protocol label is
    # prepended so the merged feed shows which monitor produced each message, and
    # no topic threading is applied (the dummy group has no per-protocol topics).
    test_chat_id = os.getenv("TELEGRAM_TEST_CHAT_ID")
    if test_chat_id:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN_DEFAULT")
        if not bot_token:
            logger.warning("TELEGRAM_TEST_CHAT_ID set but TELEGRAM_BOT_TOKEN_DEFAULT missing")
            return
        _post_message(bot_token, test_chat_id, f"[{protocol}] {message}", plain_text, disable_notification)
        return

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

    _post_message(bot_token, chat_id, message, plain_text, disable_notification, topic_id)


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

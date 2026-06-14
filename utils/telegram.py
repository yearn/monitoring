import os
import re
from collections.abc import Iterable, Sequence
from html import escape as html_escape

import requests
from dotenv import load_dotenv

from utils.logging import get_logger

load_dotenv()

logger = get_logger("utils.telegram")

# Maximum message length allowed by Telegram API
MAX_MESSAGE_LENGTH = 4096

# Telegram Bot API 10.1 rich messages allow substantially larger structured
# messages than sendMessage. Keep this explicit so rich-message callers can
# fail over before handing Telegram malformed/truncated HTML.
MAX_RICH_MESSAGE_LENGTH = 32768

# Channel key for operational errors/diagnostics (GraphQL/fetch failures, retries,
# crashes). Routed to a dedicated chat so transient noise doesn't spam the
# per-protocol alert groups. Resolves via the same env-var scheme as any other
# channel: TELEGRAM_TOPIC_ID_ERRORS (topics group) or TELEGRAM_CHAT_ID_ERRORS +
# optional TELEGRAM_BOT_TOKEN_ERRORS (falls back to the DEFAULT bot).
ERROR_CHANNEL = "errors"

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


def escape_rich_html(text: object) -> str:
    """Escape text for Telegram rich-message HTML."""
    return html_escape(str(text), quote=True)


def format_rich_table(
    headers: Sequence[object],
    rows: Iterable[Sequence[object]],
    caption: object | None = None,
    alignments: Sequence[str] | None = None,
    bordered: bool = True,
    striped: bool = True,
) -> str:
    """Render a simple Telegram rich-message HTML table.

    The returned string is meant for ``send_telegram_rich_message``. Cell
    content is escaped as plain text; callers that need inline rich formatting
    should build the table HTML themselves.
    """
    column_count = len(headers)
    if column_count == 0:
        raise ValueError("Telegram rich tables need at least one column")
    if column_count > 20:
        raise ValueError("Telegram rich tables support at most 20 columns")

    alignments = alignments or ()
    if len(alignments) > column_count:
        raise ValueError("Table alignments cannot exceed the number of columns")

    normalized_alignments: list[str | None] = []
    for alignment in alignments:
        if alignment not in {"left", "center", "right"}:
            raise ValueError("Table alignment must be one of: left, center, right")
        normalized_alignments.append(alignment)
    normalized_alignments.extend([None] * (column_count - len(normalized_alignments)))

    table_rows = [tuple(row) for row in rows]
    for row in table_rows:
        if len(row) != column_count:
            raise ValueError("All Telegram rich table rows must have the same number of columns as headers")

    attrs = []
    if bordered:
        attrs.append("bordered")
    if striped:
        attrs.append("striped")
    table_tag = "<table" + (f" {' '.join(attrs)}" if attrs else "") + ">"

    html_parts = [table_tag]
    if caption is not None:
        html_parts.append(f"<caption>{escape_rich_html(caption)}</caption>")

    def _cell(tag: str, value: object, alignment: str | None) -> str:
        align_attr = f' align="{alignment}"' if alignment else ""
        return f"<{tag}{align_attr}>{escape_rich_html(value)}</{tag}>"

    html_parts.append("<tr>")
    html_parts.extend(_cell("th", value, normalized_alignments[index]) for index, value in enumerate(headers))
    html_parts.append("</tr>")

    for row in table_rows:
        html_parts.append("<tr>")
        html_parts.extend(_cell("td", value, normalized_alignments[index]) for index, value in enumerate(row))
        html_parts.append("</tr>")

    html_parts.append("</table>")
    return "".join(html_parts)


class TelegramError(Exception):
    """Exception raised for errors in Telegram API interactions."""

    pass


def _telegram_destination(protocol: str) -> tuple[str | None, str | None, str | None]:
    """Resolve protocol routing to bot token, chat id, and optional topic id."""
    topic_id = os.getenv(f"TELEGRAM_TOPIC_ID_{protocol.upper()}")

    if topic_id:
        return (
            os.getenv("TELEGRAM_BOT_TOKEN_DEFAULT"),
            os.getenv("TELEGRAM_CHAT_ID_TOPICS"),
            topic_id,
        )

    bot_token = os.getenv(f"TELEGRAM_BOT_TOKEN_{protocol.upper()}")
    if not bot_token:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN_DEFAULT")
    return bot_token, os.getenv(f"TELEGRAM_CHAT_ID_{protocol.upper()}"), None


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
        "disable_notification": disable_notification,
    }
    if not plain_text:
        payload["parse_mode"] = "Markdown"
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


def _post_rich_message(
    bot_token: str,
    chat_id: str,
    rich_message: dict[str, object],
    disable_notification: bool,
    topic_id: str | None = None,
) -> None:
    """Send a rich message to Telegram, raising TelegramError on failure."""
    url = f"https://api.telegram.org/bot{bot_token}/sendRichMessage"
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "rich_message": rich_message,
        "disable_notification": disable_notification,
    }
    if topic_id:
        payload["message_thread_id"] = int(topic_id)

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        body = ""
        err_response = getattr(e, "response", None)
        if err_response is not None:
            try:
                body = f" body={err_response.text}"
            except Exception:
                pass
        raise TelegramError(_redact_bot_token(f"Failed to send telegram rich message: {e}{body}"))

    if response.status_code != 200:
        raise TelegramError(
            _redact_bot_token(f"Failed to send telegram rich message: {response.status_code} - {response.text}")
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
        # Escape the label for Markdown sends — protocol names contain `_`
        # (e.g. yearn_timelock) and the brackets are link syntax in V1, either of
        # which would trip a 400 parse error and drop the comparison alert. The
        # message body is left as-is (it's already valid Markdown or plain).
        label = f"[{protocol}] "
        if not plain_text:
            label = escape_markdown(label)
        _post_message(bot_token, test_chat_id, f"{label}{message}", plain_text, disable_notification)
        return

    bot_token, chat_id, topic_id = _telegram_destination(protocol)

    if not bot_token or not chat_id:
        logger.warning("Missing Telegram credentials for %s", protocol)
        return

    _post_message(bot_token, chat_id, message, plain_text, disable_notification, topic_id)


def send_telegram_rich_message(
    html: str,
    protocol: str,
    disable_notification: bool = False,
    fallback_message: str | None = None,
    skip_entity_detection: bool = True,
) -> None:
    """Send Telegram Bot API 10.1 rich-message HTML.

    Use this for structured content like tables. If the Bot API rejects the rich
    payload and ``fallback_message`` is provided, the fallback is sent as plain
    text through the existing ``sendMessage`` path.
    """
    logger.debug("Sending telegram rich message:\n%s", html)

    if os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG":
        logger.debug("Skipping Telegram rich send (LOG_LEVEL=DEBUG)")
        return

    if len(html) > MAX_RICH_MESSAGE_LENGTH:
        if fallback_message is not None:
            send_telegram_message(fallback_message, protocol, disable_notification, plain_text=True)
            return
        raise TelegramError(f"Telegram rich message exceeds {MAX_RICH_MESSAGE_LENGTH} characters")

    rich_message: dict[str, object] = {"html": html, "skip_entity_detection": skip_entity_detection}

    test_chat_id = os.getenv("TELEGRAM_TEST_CHAT_ID")
    if test_chat_id:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN_DEFAULT")
        if not bot_token:
            logger.warning("TELEGRAM_TEST_CHAT_ID set but TELEGRAM_BOT_TOKEN_DEFAULT missing")
            return
        labelled_html = f"<p>[{escape_rich_html(protocol)}]</p>{html}"
        rich_message = {"html": labelled_html, "skip_entity_detection": skip_entity_detection}
        _post_rich_message(bot_token, test_chat_id, rich_message, disable_notification)
        return

    bot_token, chat_id, topic_id = _telegram_destination(protocol)
    if not bot_token or not chat_id:
        logger.warning("Missing Telegram credentials for %s", protocol)
        return

    try:
        _post_rich_message(bot_token, chat_id, rich_message, disable_notification, topic_id)
    except TelegramError:
        if fallback_message is None:
            raise
        logger.exception("Failed to send Telegram rich message for %s; sending fallback", protocol)
        send_telegram_message(fallback_message, protocol, disable_notification, plain_text=True)


def _error_channel_configured() -> bool:
    """Return True if a dedicated errors destination (topic or chat id) is set."""
    return bool(os.getenv("TELEGRAM_TOPIC_ID_ERRORS") or os.getenv("TELEGRAM_CHAT_ID_ERRORS"))


def send_error_message(message: str, protocol: str, disable_notification: bool = True) -> None:
    """Route an operational error/diagnostic to the dedicated errors channel.

    Keeps transient failures (GraphQL/fetch errors, retries, crashes) out of the
    per-protocol alert groups. The originating ``protocol`` is prefixed as a
    ``[label]`` so the merged errors feed shows which monitor produced each line.

    Falls back to the protocol's own channel when no errors destination is
    configured, so error visibility is never silently lost. Always sent as plain
    text (error strings routinely contain Markdown-breaking characters) and
    silently by default.

    Args:
        message: The error/diagnostic text.
        protocol: Originating protocol/channel, used as the ``[label]`` prefix
            and as the fallback channel when no errors destination is configured.
        disable_notification: If True (default), send silently.
    """
    if _error_channel_configured():
        send_telegram_message(f"[{protocol}] {message}", ERROR_CHANNEL, disable_notification, plain_text=True)
    else:
        send_telegram_message(message, protocol, disable_notification, plain_text=True)


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

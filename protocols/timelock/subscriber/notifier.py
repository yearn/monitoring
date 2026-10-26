"""Best-effort Telegram notices for the subscriber pilot, kept off the ingestion path.

The worker only calls ``Notifier.post``, which never blocks: messages go onto a
bounded queue and a background task sends them. Messages queued while a send is
in flight are coalesced into one Telegram message, so a backlog does not become a
burst. A failed send is logged and dropped. Ingestion never waits on Telegram.
"""

import asyncio
from collections.abc import Callable

from utils.logger import get_logger
from utils.telegram import MAX_MESSAGE_LENGTH, error_channel_configured, send_error_message

_logger = get_logger("timelock.subscriber.notifier")

# Label shown as "[timelock-subscriber]" in the errors chat.
NOTIFY_LABEL = "timelock-subscriber"
# Alert-history key: internal-only, so pilot notices stay off public protocol pages.
ALERT_PROTOCOL = "yearn-internal"
NOTIFY_MODES = ("none", "errors")

_SEPARATOR = "\n\n"
# Room for the "[timelock-subscriber] " prefix and Markdown escaping of the text.
_MAX_BATCH_CHARS = MAX_MESSAGE_LENGTH // 2

SendFn = Callable[[str], None]


def send_to_errors_chat(text: str) -> None:
    """Send one plain-text notice to the internal errors chat, silently."""
    send_error_message(text, NOTIFY_LABEL, source="timelock_subscriber", alert_protocol=ALERT_PROTOCOL)


class Notifier:
    """Queue of notices drained by one background sender task."""

    def __init__(self, send: SendFn = send_to_errors_chat, max_queue: int = 200, min_interval: float = 1.0) -> None:
        self.send = send
        self.min_interval = min_interval
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue)
        self.sent = 0
        self.dropped = 0
        self.failed = 0
        # A queued notice that did not fit the previous batch; it starts the next one.
        self._carry: str | None = None

    def post(self, text: str) -> None:
        """Queue a notice without blocking; drop it if the queue is full."""
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            self.dropped += 1
            _logger.warning("notice queue full; dropped: %s", text[:120])

    def _batch(self, first: str) -> str:
        """Join ``first`` with whatever else is queued, up to one message's budget."""
        parts = [first]
        size = len(first)
        while not self.queue.empty():
            nxt = self.queue.get_nowait()
            if size + len(_SEPARATOR) + len(nxt) > _MAX_BATCH_CHARS:
                self._carry = nxt
                break
            parts.append(nxt)
            size += len(_SEPARATOR) + len(nxt)
        return _SEPARATOR.join(parts)

    async def run(self) -> None:
        """Send queued notices until cancelled."""
        while True:
            first = self._carry if self._carry is not None else await self.queue.get()
            self._carry = None
            text = self._batch(first[:_MAX_BATCH_CHARS])
            try:
                await asyncio.to_thread(self.send, text)
                self.sent += 1
            except Exception:  # noqa: BLE001 - notices are best-effort
                self.failed += 1
                _logger.warning("failed to send subscriber notice", exc_info=True)
            await asyncio.sleep(self.min_interval)


def build_notifier(mode: str) -> Notifier | None:
    """Return a notifier for ``mode``, or None when notices are off or cannot be routed safely.

    ``errors`` requires a configured errors chat: ``send_error_message`` would
    otherwise fall back to a protocol channel, and pilot notices must never reach one.
    """
    if mode not in NOTIFY_MODES:
        raise ValueError(f"notify mode must be one of {NOTIFY_MODES}, got {mode!r}")
    if mode == "none":
        return None
    if not error_channel_configured():
        _logger.warning("TIMELOCK_SUBSCRIBER_NOTIFY=errors but no errors chat is configured; notices disabled")
        return None
    return Notifier()

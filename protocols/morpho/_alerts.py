"""Per-vault alert grouping shared by the V1 and V2 Morpho governance monitors.

Both monitors used to send one Telegram message per finding, so a vault with
several simultaneous governance changes produced a burst of near-identical
messages that repeated the vault name and chain in every one. They now buffer
findings into a :class:`VaultDiff` and flush it as a single message per vault:
one header, one severity (the highest of the group), sections separated by
``---``.

The buffer also holds the cache writes that record "we alerted on this", so they
can be committed only once delivery succeeds — writing them during the diff pass
marks a change as alerted even when Telegram failed, and neither monitor retries
a message it has already recorded.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any, Callable, List

from utils.alert import Alert, AlertSeverity, send_alert
from utils.file_lock import file_lock
from utils.telegram import MAX_MESSAGE_LENGTH

# Ascending severity — a grouped alert is sent at the highest of its sections.
_SEVERITY_ORDER = (AlertSeverity.LOW, AlertSeverity.MEDIUM, AlertSeverity.HIGH, AlertSeverity.CRITICAL)

SECTION_SEPARATOR = "\n\n---\n\n"

# Telegram truncates past MAX_MESSAGE_LENGTH (and drops Markdown with it), so a
# large batch would silently lose its tail. We split into "(i/N)" parts instead.
# The slack covers the emoji ``send_alert`` prepends, the part suffix, and the
# blank line after the header.
_MESSAGE_OVERHEAD = 64


# Header prefix for findings that can write off vault funds (market removal,
# adapter removal, share burn). They are sent as their own message so they never
# drown in a routine grouped diff.
LOSS_PREFIX = "🚨 POTENTIAL VAULT LOSS"

# Pending loss operations are re-alerted once when their execution time is this close.
REMINDER_WINDOW_SECONDS = 24 * 60 * 60


def governance_lock(vault_address: str) -> AbstractContextManager[None]:
    """Lock one vault's governance check → send → cache-write sequence across processes.

    The daily jobs and the live consumer's production mode read and write the
    same cache keys; holding this lock around the whole sequence keeps them from
    both treating one change as new and alerting it twice.
    """
    return file_lock(f"morpho_governance_{vault_address.lower()}")


@dataclass(frozen=True)
class LossNotice:
    """A potential-loss alert that is due for one pending operation."""

    reminder: bool  # render as the near-deadline reminder rather than the first alert
    in_window: bool  # the deadline is inside the reminder window, so mark the reminder as sent too


def due_loss_notice(deadline: int, loss_alerted_at: int, reminded_at: int) -> LossNotice | None:
    """Decide whether a pending operation that puts funds at risk needs a loss alert now.

    The loss alert is tracked separately from the "pending operation seen" cursor,
    so an operation first seen with nothing at risk (sent at a lower severity) still
    escalates once funds arrive, instead of waiting for the reminder window.

    Args:
        deadline: When the operation becomes executable; also the cache value.
        loss_alerted_at: Cached deadline of the last loss alert sent for it.
        reminded_at: Cached deadline of the last reminder sent for it.

    Returns:
        The notice to send, or None if both the alert and the reminder already went out.
    """
    in_window = int(datetime.now().timestamp()) >= deadline - REMINDER_WINDOW_SECONDS
    if loss_alerted_at != deadline:
        return LossNotice(reminder=False, in_window=in_window)
    if in_window and reminded_at != deadline:
        return LossNotice(reminder=True, in_window=True)
    return None


@dataclass(frozen=True)
class LiveExpectation:
    """The indexed event a daily finding implies, which the live path should have decided.

    ``args`` holds the ABI-named arguments that identify the occurrence. Findings
    with no expectation (reminders, resolutions, guardian, sentinel, and allocator
    changes) are not audited. When present, ``lines`` identifies the zero-based
    lines in a grouped section that render this event.
    """

    event: str
    args: dict[str, Any] = field(default_factory=dict)
    transaction_hash: str = ""
    lines: range | None = field(default=None, compare=False)

    def matches(self, event: str, args: dict[str, Any], transaction_hash: str) -> bool:
        """Return whether this event carries the expected arguments and transaction."""
        return (
            self.event == event
            and (not self.transaction_hash or self.transaction_hash.lower() == transaction_hash.lower())
            and all(name in args and _arg_matches(args[name], expected) for name, expected in self.args.items())
        )


def _arg_matches(value: Any, expected: Any) -> bool:
    """Compare ABI arguments across hex casing and JSON integer encodings."""
    if type(expected) is int:
        if not isinstance(value, str):
            return type(value) is int and value == expected
        try:
            base = 16 if value.lower().startswith("0x") else 10
            return int(value, base) == expected
        except ValueError:
            return False
    return str(value).lower().removeprefix("0x") == str(expected).lower().removeprefix("0x")


@dataclass
class VaultAlert:
    """One section of a vault's grouped Telegram message.

    ``standalone`` sections are sent as their own message with ``LOSS_PREFIX``
    instead of being grouped with the vault's other findings.
    """

    severity: AlertSeverity
    body: str
    standalone: bool = False
    expected: tuple[LiveExpectation, ...] = ()
    source_ids: tuple[str, ...] = ()
    line_source_ids: dict[int, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class RenderedAlert:
    """A Telegram message and the indexed events behind its sections."""

    severity: AlertSeverity
    message: str
    source_ids: tuple[str, ...]


@dataclass
class VaultDiff:
    """Buffered findings and cache writes for one vault's diff pass."""

    alerts: List[VaultAlert] = field(default_factory=list)
    writes: List[Callable[[], None]] = field(default_factory=list)

    def alert(self, severity: AlertSeverity, body: str, expected: tuple[LiveExpectation, ...] = ()) -> None:
        """Buffer one section of the vault's grouped message."""
        self.alerts.append(VaultAlert(severity, body, expected=expected))

    def loss(self, body: str, expected: tuple[LiveExpectation, ...] = ()) -> None:
        """Buffer a HIGH potential-loss finding, sent as its own message."""
        self.alerts.append(VaultAlert(AlertSeverity.HIGH, body, standalone=True, expected=expected))

    def defer(self, write: Callable[..., Any], *args: Any) -> None:
        """Buffer a cache write to apply once the alert is delivered."""
        self.writes.append(partial(write, *args))

    def commit(self) -> None:
        """Persist every buffered cache write."""
        for write in self.writes:
            write()


def _wrap_line(line: str, budget: int) -> List[str]:
    """Break one line longer than ``budget``, at a space when one falls in its second half."""
    pieces: List[str] = []
    while len(line) > budget:
        cut = line.rfind(" ", budget // 2, budget)
        if cut <= 0:
            cut = budget
        pieces.append(line[:cut])
        line = line[cut:].lstrip(" ")
    pieces.append(line)
    return pieces


def split_body(body: str, budget: int) -> List[str]:
    """Split one oversized section so nothing is truncated.

    A single section can exceed the budget on its own — a batched multicall
    submit renders one bullet per operation, and 30 of them do not fit. Splitting
    between lines keeps every operation intact; a single line longer than the
    whole budget is wrapped, because Telegram would otherwise cut its tail while
    the send still counts as delivered.
    """
    return [text for text, _ in _split_section(body, budget, {})]


def _split_section(body: str, budget: int, line_source_ids: dict[int, list[str]]) -> list[tuple[str, tuple[str, ...]]]:
    """Split a section and keep each line's provenance with its message part."""
    if len(body) <= budget:
        return [(body, tuple(dict.fromkeys(event_id for ids in line_source_ids.values() for event_id in ids)))]
    chunks: list[tuple[str, tuple[str, ...]]] = []
    current: list[str] = []
    sources: dict[str, None] = {}
    size = 0
    for index, raw in enumerate(body.split("\n")):
        for line in _wrap_line(raw, budget):
            cost = len(line) + 1
            if current and size + cost > budget:
                chunks.append(("\n".join(current), tuple(sources)))
                current = []
                sources = {}
                size = 0
            current.append(line)
            sources.update(dict.fromkeys(line_source_ids.get(index, ())))
            size += cost
    if current:
        chunks.append(("\n".join(current), tuple(sources)))
    return chunks


def split_into_messages(alerts: List[VaultAlert], budget: int) -> List[List[tuple[str, tuple[str, ...]]]]:
    """Pack section bodies and their sources into groups that fit within ``budget`` chars."""
    parts: List[List[tuple[str, tuple[str, ...]]]] = [[]]
    size = 0
    for alert in alerts:
        for body, line_ids in _split_section(alert.body, budget, alert.line_source_ids):
            cost = len(body) + len(SECTION_SEPARATOR)
            if parts[-1] and size + cost > budget:
                parts.append([])
                size = 0
            parts[-1].append((body, alert.source_ids + line_ids))
            size += cost
    return parts


def render_vault_alerts(header: str, alerts: List[VaultAlert]) -> List[tuple[AlertSeverity, str]]:
    """Render buffered sections into the ``(severity, message)`` pairs ``send_vault_alerts`` sends."""
    return [(part.severity, part.message) for part in render_vault_alert_parts(header, alerts)]


def render_vault_alert_parts(header: str, alerts: List[VaultAlert]) -> list[RenderedAlert]:
    """Render buffered sections and keep their source IDs on each message.

    Standalone (potential-loss) sections come first, one message each, under a
    ``LOSS_PREFIX`` header; the remaining sections are grouped under ``header`` at
    their highest severity and split into "(i/N)" parts if long.
    """
    messages: list[RenderedAlert] = []
    for alert in alerts:
        if alert.standalone:
            messages += _render_group(f"{LOSS_PREFIX} — {header}", [alert])
    messages += _render_group(header, [a for a in alerts if not a.standalone])
    return messages


def send_vault_alerts(header: str, alerts: List[VaultAlert], protocol: str) -> None:
    """Send the buffered sections as one Telegram message, or "(i/N)" parts if long.

    No-op when ``alerts`` is empty so callers don't have to guard. Every part
    carries the same header and the highest severity of the whole group, so a
    LOW section bundled with an owner change still pings the channel. See
    ``render_vault_alerts`` for the message layout.
    """
    for severity, message in render_vault_alerts(header, alerts):
        send_alert(Alert(severity, message, protocol))


def _render_group(header: str, alerts: List[VaultAlert]) -> list[RenderedAlert]:
    """Render sections under one header at their highest severity, split into parts if long."""
    if not alerts:
        return []
    severity = max((a.severity for a in alerts), key=_SEVERITY_ORDER.index)
    parts = split_into_messages(alerts, MAX_MESSAGE_LENGTH - _MESSAGE_OVERHEAD - len(header))
    total = len(parts)
    messages = []
    for index, sections in enumerate(parts, start=1):
        suffix = f" ({index}/{total})" if total > 1 else ""
        body = SECTION_SEPARATOR.join(text for text, _ in sections)
        sources = tuple(dict.fromkeys(event_id for _, ids in sections for event_id in ids))
        messages.append(RenderedAlert(severity, f"{header}{suffix}\n\n{body}", sources))
    return messages

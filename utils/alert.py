"""Structured alert system with severity levels and hook support.

Usage::

    from utils.alert import Alert, AlertSeverity, send_alert

    # Informational — sent silently
    send_alert(Alert(AlertSeverity.LOW, "New governance proposal", "aave"))

    # Warning — sent silently
    send_alert(Alert(AlertSeverity.MEDIUM, "Failed to fetch data", "morpho"), plain_text=True)

    # Urgent — sends with notification sound
    send_alert(Alert(AlertSeverity.HIGH, "UR at 95%", "aave"))

    # System failure — sends with notification sound
    send_alert(Alert(AlertSeverity.CRITICAL, "No vaults data found", "morpho"))

Severity guide:
    LOW       — Governance proposals, informational updates (silent)
    MEDIUM    — Fetch errors, stale data, degraded state (silent)
    HIGH      — UR breaches, bad debt, peg deviations, liquidity issues (loud)
    CRITICAL  — Missing data, all-zero metrics, total system failures (loud)

Override defaults with ``silent=True/False``.

Hooks (optional):
    On import, ``utils.alert`` registers ``utils.dispatch.dispatch_emergency_withdrawal``
    if no hook is set yet (lazy import at end of module avoids cycles with ``dispatch``).
    Override with ``register_alert_hook(fn)``, or ``None`` to clear (tests). Hook
    exceptions are logged and swallowed.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from utils.logging import get_logger
from utils.telegram import send_telegram_message

logger = get_logger("utils.alert")

# Emoji prefixes per severity level
_SEVERITY_EMOJI = {
    "LOW": "ℹ️",
    "MEDIUM": "⚠️",
    "HIGH": "🚨",
    "CRITICAL": "🔴",
}

# Default silent behavior per severity level
_SEVERITY_SILENT_DEFAULT = {
    "LOW": True,
    "MEDIUM": False,
    "HIGH": False,
    "CRITICAL": False,
}

# Module-level hook storage
_alert_hook: Callable[["Alert"], None] | None = None


class AlertSeverity(Enum):
    """Alert severity levels."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Alert:
    """Immutable alert with severity, message, and protocol.

    Args:
        protocol: The actual protocol name (used by hooks/dispatch).
        channel: Telegram channel for routing. Falls back to ``protocol`` if empty.
    """

    severity: AlertSeverity
    message: str
    protocol: str
    channel: str = ""


def _ensure_default_dispatch_hook() -> None:
    """Install emergency dispatch hook if nothing registered (production default)."""
    global _alert_hook
    if _alert_hook is not None:
        return
    try:
        from utils.dispatch import dispatch_emergency_withdrawal

        _alert_hook = dispatch_emergency_withdrawal
    except ImportError:
        logger.debug("utils.dispatch not available, skipping default hook")


def register_alert_hook(callback: Callable[[Alert], None]) -> None:
    """Register a hook callback invoked for HIGH and CRITICAL alerts.

    Args:
        callback: Function receiving an Alert. Exceptions are logged and swallowed.
    """
    global _alert_hook
    _alert_hook = callback


def send_alert(
    alert: Alert,
    *,
    silent: bool | None = None,
    plain_text: bool = False,
) -> None:
    """Send an alert via Telegram with auto-emoji prefix and severity-based defaults.

    Args:
        alert: The Alert to send.
        *: prevents positional mistakes like accidentally swapping silent and plain_text, since both are bool
        silent: Override notification silencing. None uses severity default
                (LOW/MEDIUM=silent, HIGH/CRITICAL=loud).
        plain_text: If True, send without Markdown formatting.
    """
    emoji = _SEVERITY_EMOJI[alert.severity.value]
    message = f"{emoji} {alert.message}"

    if silent is None:
        silent = _SEVERITY_SILENT_DEFAULT[alert.severity.value]

    send_telegram_message(
        message,
        alert.channel or alert.protocol,
        silent,
        plain_text,
        severity=alert.severity.value,
        source="protocol",
        origin_protocol=alert.protocol,
        channel=alert.channel or alert.protocol,
    )

    # Invoke hook for HIGH and CRITICAL alerts
    if alert.severity in (AlertSeverity.HIGH, AlertSeverity.CRITICAL) and _alert_hook is not None:
        try:
            _alert_hook(alert)
        except Exception:
            logger.exception("Alert hook failed for %s alert", alert.severity.value)


# After AlertSeverity / Alert exist so utils.dispatch can import this module safely.
_ensure_default_dispatch_hook()

"""Dispatch emergency withdrawal requests to the liquidity-monitoring repo via GitHub API.

When a HIGH or CRITICAL alert fires, this module sends a ``repository_dispatch``
event to ``tapired/liquidity-monitoring`` with the protocol name and severity.
The receiving repo resolves which vaults/markets to act on from its own config
(``emergency_config.json``, ``markets_config.py``, ``forced_caps.json``).

Requires the ``PAT_DISPATCH`` environment variable (fine-grained PAT
with ``actions:write`` on the target repo).
"""

import os
import time

import requests

from utils.alert import Alert, AlertSeverity
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.logging import get_logger

logger = get_logger("utils.dispatch")

TARGET_REPO = "tapired/liquidity-monitoring"
DISPATCH_URL = f"https://api.github.com/repos/{TARGET_REPO}/dispatches"
DEFAULT_COOLDOWN_SECONDS = 3600  # 60 minutes

# Protocols that have emergency withdrawal config in liquidity-monitoring.
# Only these protocols will trigger a dispatch.
DISPATCHABLE_PROTOCOLS = {"infinifi", "cap", "ethena", "ethplus", "usdai", "origin", "maple"}


def _is_on_cooldown(protocol: str, cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS) -> bool:
    """Check if a dispatch was sent recently for this protocol."""
    cache_key = f"dispatch_last_{protocol}"
    last_ts = get_last_value_for_key_from_file(cache_filename, cache_key)
    if last_ts == 0:
        return False
    try:
        return (time.time() - float(last_ts)) < cooldown_seconds
    except (TypeError, ValueError):
        return False


def _record_dispatch(protocol: str) -> None:
    """Record the current timestamp as the last dispatch time for this protocol."""
    cache_key = f"dispatch_last_{protocol}"
    write_last_value_to_file(cache_filename, cache_key, time.time())


def dispatch_emergency_withdrawal(alert: Alert) -> None:
    """Dispatch an emergency withdrawal to liquidity-monitoring.

    Registered by default when ``utils.alert`` is imported (see
    ``_ensure_default_dispatch_hook``) unless a hook was already set; override via
    ``register_alert_hook``.
    Only dispatches for HIGH and CRITICAL alerts whose protocol is in
    ``DISPATCHABLE_PROTOCOLS``. Respects a per-protocol cooldown to avoid
    duplicate dispatches from repeated alerts.

    The receiving workflow resolves vaults, markets, and chains from its
    own ``emergency_config.json``.

    Args:
        alert: The alert that triggered the hook.
    """
    if alert.severity not in (AlertSeverity.HIGH, AlertSeverity.CRITICAL):
        return

    if os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG":
        logger.debug("Skipping dispatch (LOG_LEVEL=DEBUG)")
        return

    if alert.protocol not in DISPATCHABLE_PROTOCOLS:
        logger.debug("Protocol %s not in DISPATCHABLE_PROTOCOLS, skipping dispatch", alert.protocol)
        return

    if _is_on_cooldown(alert.protocol):
        logger.info("Dispatch for %s is on cooldown, skipping", alert.protocol)
        return

    token = os.getenv("PAT_DISPATCH")
    if not token:
        logger.warning("PAT_DISPATCH not set, cannot dispatch emergency withdrawal")
        return

    payload = {
        "event_type": "emergency_withdrawal",
        "client_payload": {
            "protocol": alert.protocol,
            "severity": alert.severity.value,
            "message": alert.message,
        },
    }

    try:
        response = requests.post(
            DISPATCH_URL,
            json=payload,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10,
        )
        response.raise_for_status()
        _record_dispatch(alert.protocol)
        logger.info(
            "Dispatched emergency withdrawal for %s (severity=%s)",
            alert.protocol,
            alert.severity.value,
        )
    except requests.RequestException:
        logger.exception("Failed to dispatch emergency withdrawal for %s", alert.protocol)

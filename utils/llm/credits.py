"""Low-credit alert for the Venice LLM API key.

When the key can no longer spend, every completion fails and the explainer quietly
degrades to "no AI summary" — nothing alerts. The consumer scripts (safe, timelock)
call :func:`check_llm_credits` at the start of each run so the account is topped up
(or the key's limit raised) before explanations stop.

The balance Venice reports for a key is what that key can still spend: the lower of
the account balance and what is left of the key's daily spend limit. A low reading
therefore means either the account is nearly empty or the key's daily limit is
nearly used up (it resets at the next epoch, 00:00 UTC). The inference key can't
tell the two apart — ``/billing/balance`` needs an admin key.

Environment variables:
    LLM_CREDIT_ALERT_THRESHOLD_USD: Alert when the spendable balance drops below
        this many dollars (default: 1).
"""

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.http_client import request_with_retry
from utils.llm.factory import _PROVIDER_DEFAULTS
from utils.logger import get_logger
from utils.telegram import send_error_message

logger = get_logger("utils.llm.credits")

DEFAULT_THRESHOLD_USD = 1.0
# Re-alert at most once a day while the balance stays low.
ALERT_COOLDOWN_SECONDS = 24 * 60 * 60
CACHE_KEY_ALERTED_AT = "LLM_CREDITS_ALERTED_AT"


@dataclass(frozen=True)
class VeniceBalance:
    """What a Venice API key can still spend in the current epoch."""

    usd: float
    diem: float
    access_permitted: bool
    next_epoch: str = ""

    @property
    def spendable(self) -> float:
        """USD plus DIEM (1 DIEM buys $1 of compute per epoch)."""
        return self.usd + self.diem


def fetch_venice_balance(api_key: str, base_url: str) -> VeniceBalance:
    """Read the key's balance from Venice.

    Uses ``/api_keys/rate_limits`` rather than ``/billing/balance``: the latter
    requires an admin key, while this one works with the inference key.

    Args:
        api_key: Venice API key.
        base_url: Venice API base URL (e.g. https://api.venice.ai/api/v1).

    Returns:
        The key's balance.

    Raises:
        requests.RequestException: If the request fails.
        KeyError, TypeError, ValueError: If the response is malformed.
    """
    response = request_with_retry(
        "get",
        f"{base_url.rstrip('/')}/api_keys/rate_limits",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    data = response.json()["data"]
    balances = data["balances"]
    return VeniceBalance(
        usd=float(balances.get("USD", 0)),
        diem=float(balances.get("DIEM", 0)),
        access_permitted=bool(data.get("accessPermitted", True)),
        next_epoch=str(data.get("nextEpochBegins") or ""),
    )


def check_llm_credits(label: str, alert_protocol: str | None = None, now: float | None = None) -> None:
    """Send an ops alert when the Venice balance is below the threshold.

    No-op for other providers or when no API key is set. Never raises: a failed
    balance read is logged and the calling monitor carries on.

    Args:
        label: Telegram protocol key used to label the alert (see ``send_error_message``).
        alert_protocol: Protocol key stored in alert history.
        now: Current unix time; defaults to ``time.time()``.
    """
    provider = (os.getenv("LLM_PROVIDER") or "venice").lower()
    api_key = os.getenv("LLM_API_KEY")
    if provider != "venice" or not api_key:
        return
    base_url = os.getenv("LLM_BASE_URL") or _PROVIDER_DEFAULTS["venice"]["base_url"]
    threshold = float(os.getenv("LLM_CREDIT_ALERT_THRESHOLD_USD") or DEFAULT_THRESHOLD_USD)
    now = time.time() if now is None else now

    try:
        balance = fetch_venice_balance(api_key, base_url)
    except Exception as e:  # noqa: BLE001 - the balance check must never block the monitor
        logger.warning("Could not read Venice balance: %s", e)
        return

    logger.info("Venice balance: USD %.2f, DIEM %.2f", balance.usd, balance.diem)
    if balance.access_permitted and balance.spendable >= threshold:
        # Topped up: clear the cooldown so the next drop alerts right away.
        if _last_alerted_at() > 0:
            write_last_value_to_file(cache_filename, CACHE_KEY_ALERTED_AT, 0)
        return

    if now - _last_alerted_at() < ALERT_COOLDOWN_SECONDS:
        return
    status = "access blocked" if not balance.access_permitted else f"below ${threshold:.2f}"
    send_error_message(
        f"🚨 Venice LLM key running out of credits ({status})\n"
        f"Spendable: ${balance.usd:.2f} USD, {balance.diem:.2f} DIEM.\n"
        "AI explanations on timelock and Safe alerts stop once it hits zero. "
        "This is the lower of the account balance and the key's daily spend limit: "
        f"if the account is funded, the limit resets at {_format_epoch(balance.next_epoch)}. "
        "Top up or raise the limit at https://venice.ai/settings/api",
        label,
        disable_notification=False,
        source="llm_credits",
        alert_protocol=alert_protocol,
    )
    write_last_value_to_file(cache_filename, CACHE_KEY_ALERTED_AT, int(now))


def _format_epoch(raw: str) -> str:
    """Render Venice's ``nextEpochBegins`` ISO timestamp as ``YYYY-MM-DD HH:MM UTC``."""
    try:
        return (
            datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        )
    except ValueError:
        return "the next epoch (00:00 UTC)"


def _last_alerted_at() -> float:
    """Return when the low-credit alert last fired (0 if never or reset)."""
    try:
        return float(get_last_value_for_key_from_file(cache_filename, CACHE_KEY_ALERTED_AT))
    except (TypeError, ValueError):
        return 0.0

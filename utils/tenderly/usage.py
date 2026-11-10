"""Detect organization-wide Tenderly TU spikes using settled hourly usage."""

import argparse
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv

from utils import store
from utils.http_client import request_with_retry
from utils.logger import get_logger
from utils.telegram import YEARN_MAINTENANCE_CHANNEL, resolve_channel, send_telegram_message

load_dotenv()

logger = get_logger("tenderly.usage")
PROTOCOL = "yearn"
ALERT_PROTOCOL = "yearn-internal"
STATE_NAMESPACE = "tenderly_usage"
HOUR_SECONDS = 3600
SETTLE_SECONDS = 5 * 60
BASELINE_HOURS = 24
SPIKE_MULTIPLIER = 3
MIN_EXTRA_TUS = 100_000


@dataclass(frozen=True)
class UsageCheck:
    """A settled hour and the peak usage in the preceding 24 hours."""

    hour: int
    tus: float
    baseline_peak: float

    @property
    def threshold(self) -> float:
        """Minimum usage satisfying both the relative and absolute thresholds."""
        return max(self.baseline_peak * SPIKE_MULTIPLIER, self.baseline_peak + MIN_EXTRA_TUS)

    @property
    def is_spike(self) -> bool:
        """Whether the hour crosses both spike thresholds."""
        return self.tus >= self.threshold


def fetch_usage(account: str) -> dict[str, Any]:
    """Fetch ungrouped weekly TU history with the configured Tenderly API token.

    Weekly history provides hourly buckets and enough data for a 24-hour baseline.
    The dashboard usage endpoint is not part of Tenderly's public API reference.
    """
    api_key = os.getenv("TENDERLY_API_KEY")
    if not api_key:
        raise ValueError("Set TENDERLY_API_KEY to enable the Tenderly usage monitor")
    account_slug = quote(account, safe="")
    response = request_with_retry(
        "get",
        f"https://api.tenderly.co/api/v1/account/{account_slug}/usage/containers/timeseries",
        headers={"X-Access-Key": api_key},
        params={"timespan": "1w", "cumulative": "false"},
    )
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Tenderly usage response must be a JSON object")
    return payload


def _usage_points(payload: dict[str, Any]) -> list[Any]:
    """Require one organization-wide TU series rather than a partial breakdown."""
    series = payload.get("timeseries")
    if not isinstance(series, list) or len(series) != 1:
        raise ValueError("Tenderly must return exactly one organization-wide usage series")
    row = series[0]
    if not isinstance(row, dict) or row.get("labels") != {}:
        raise ValueError("Tenderly returned grouped usage instead of organization-wide usage")
    usages = row.get("usages")
    if not isinstance(usages, dict) or not isinstance(usages.get("TUsConsumed"), list):
        raise ValueError("Tenderly usage response is missing the TUsConsumed history")
    return usages["TUsConsumed"]


def _parse_point(point: Any) -> tuple[int, float]:
    """Validate a TU value and its UTC hourly bucket timestamp."""
    if not isinstance(point, dict):
        raise ValueError("Tenderly usage history contains a malformed point")
    timestamp, tus = point.get("timestamp"), point.get("value")
    if type(timestamp) is not int or timestamp % HOUR_SECONDS:
        raise ValueError("Tenderly usage history must contain UTC hourly bucket timestamps")
    if type(tus) not in (int, float) or not math.isfinite(tus) or tus < 0:
        raise ValueError("Tenderly usage history must contain finite, non-negative TU values")
    return timestamp, tus


def evaluate_usage(payload: dict[str, Any], now: int) -> UsageCheck:
    """Compare the last settled hour with the preceding 24 complete hours.

    Missing or duplicate buckets fail the check rather than lowering its baseline.
    The current partial hour and hours ended less than five minutes ago are ignored.
    """
    history: dict[int, float] = {}
    for point in _usage_points(payload):
        timestamp, tus = _parse_point(point)
        if timestamp in history:
            raise ValueError("Tenderly usage history contains duplicate hourly buckets")
        history[timestamp] = tus
    hour = (now - SETTLE_SECONDS) // HOUR_SECONDS * HOUR_SECONDS - HOUR_SECONDS
    required = [hour - offset * HOUR_SECONDS for offset in range(BASELINE_HOURS + 1)]
    if any(timestamp not in history for timestamp in required):
        raise ValueError("Tenderly usage history is stale or missing one of the required 25 hourly buckets")
    return UsageCheck(hour, history[hour], max(history[timestamp] for timestamp in required[1:]))


def build_message(account: str, check: UsageCheck) -> str:
    """Build a credential-free Telegram alert with the window and comparison."""
    start = datetime.fromtimestamp(check.hour, UTC).strftime("%Y-%m-%d %H:%M")
    end = datetime.fromtimestamp(check.hour + HOUR_SECONDS, UTC).strftime("%H:%M")
    return (
        f"Tenderly usage spike — {account}\n"
        f"Hour: {start}–{end} UTC\n"
        f"Usage: {check.tus:,.0f} TUs\n"
        f"Previous 24-hour peak: {check.baseline_peak:,.0f} TUs/hour\n"
        f"Threshold: {check.threshold:,.0f} TUs/hour "
        f"({SPIKE_MULTIPLIER}× peak and at least {MIN_EXTRA_TUS:,} extra TUs)\n"
        "Check Tenderly Usage for unexpected projects, RPCs, or methods.\n"
        "https://dashboard.tenderly.co/"
    )


def check_usage(*, dry_run: bool = False) -> None:
    """Evaluate account usage and alert once per anomalous hour after delivery."""
    account = os.getenv("TENDERLY_ACCOUNT", "yearn")
    check = evaluate_usage(fetch_usage(account), int(time.time()))
    logger.info(
        "Tenderly %s: hour=%s usage=%.0f TUs previous_peak=%.0f threshold=%.0f spike=%s",
        account,
        datetime.fromtimestamp(check.hour, UTC).isoformat(),
        check.tus,
        check.baseline_peak,
        check.threshold,
        check.is_spike,
    )
    if not check.is_spike:
        return
    message = build_message(account, check)
    if dry_run:
        logger.warning("Dry run would alert: %s", message.replace("\n", " | "))
        return
    if store.state_get(STATE_NAMESPACE, account) == str(check.hour):
        logger.info("Already alerted for this Tenderly usage hour")
        return
    if send_telegram_message(
        message,
        resolve_channel(YEARN_MAINTENANCE_CHANNEL, PROTOCOL),
        plain_text=True,
        origin_protocol=ALERT_PROTOCOL,
        source="tenderly_usage",
        severity="MEDIUM",
    ):
        store.state_set(STATE_NAMESPACE, account, str(check.hour))


def main() -> None:
    """Run the scheduled usage check, optionally without delivery or state writes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without sending alerts or writing state")
    args = parser.parse_args()
    if not args.dry_run:
        check_usage()
        return
    try:
        check_usage(dry_run=True)
    except Exception:  # noqa: BLE001 - dry runs must not send crash alerts either
        logger.exception("Tenderly usage dry run failed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL, alert_protocol=ALERT_PROTOCOL)

"""Durably deliver risk alerts to liquidity-monitoring and optional vaults-mommy.

The existing liquidity envelope is preserved byte-for-byte in shape; vaults-mommy
also receives a signed event_id. Retry bodies and IDs survive process restarts.
"""

import hashlib
import hmac
import os

import requests

from utils.alert import Alert, AlertSeverity
from utils.dispatch_queue import Destination, DispatchQueue
from utils.logger import get_logger

logger = get_logger("utils.dispatch")
DEFAULT_WEBHOOK_URL = "http://127.0.0.1:8080/webhook/emergency"
DEFAULT_VAULTS_MOMMY_URL = "http://127.0.0.1:8081/webhook/emergency"

DISPATCHABLE_PROTOCOLS = {
    "infinifi",
    "cap",
    "ethena",
    "ethplus",
    "usdai",
    "origin",
    "maple",
    "3jane",
    "wbtc",
    "coinbase",
    "lombard",
    "tether",
    "circle",
    "maker",
}


def destinations() -> tuple[Destination, ...]:
    """Load independent credentials; an unset secret disables only its destination."""
    result = []
    for name, prefix, default, signed_id in (
        ("liquidity", "LIQUIDITY_WEBHOOK", DEFAULT_WEBHOOK_URL, False),
        ("vaults-mommy", "VAULTS_MOMMY_WEBHOOK", DEFAULT_VAULTS_MOMMY_URL, True),
    ):
        secret = os.getenv(f"{prefix}_SECRET")
        if secret:
            result.append(Destination(name, os.getenv(f"{prefix}_URL", default), secret, signed_id))
        elif os.getenv(f"{prefix}_URL"):
            logger.warning("%s_SECRET missing; %s dispatch is disabled", prefix, name)
    return tuple(result)


def _signature_header(secret: str, body: bytes) -> str:
    """Sign the exact persisted bytes that will be sent."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def drain_emergency_deliveries(*, queue: DispatchQueue | None = None, limit: int = 20) -> int:
    """Drain due work without needing a new alert; each destination progresses independently."""
    if os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG":
        logger.debug("Skipping emergency delivery retries (LOG_LEVEL=DEBUG); queue retained")
        return 0
    configured = destinations()
    if not configured:
        return 0
    queue = queue or DispatchQueue()
    attempted: set[int] = set()
    for _ in range(limit):
        progressed = False
        for destination in configured:
            delivery = queue.claim(destination.name, exclude=attempted)
            if delivery is None:
                continue
            progressed = True
            attempted.add(delivery.delivery_id)
            if destination.url != delivery.url:
                queue.finish(
                    delivery, "terminal", error="configured endpoint changed; explicitly retry after operator review"
                )
                logger.error("Emergency delivery %s endpoint changed; retained for review", delivery.delivery_id)
                continue
            try:
                response = requests.post(
                    delivery.url,
                    data=delivery.body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": _signature_header(destination.secret, delivery.body),
                    },
                    timeout=(3, 10),
                    allow_redirects=False,
                    stream=True,
                )
                try:
                    code = response.status_code
                    delay = None
                    if code == 429:
                        raw_delay = response.headers.get("Retry-After", "")
                        if raw_delay.isdecimal():
                            delay = min(3600, int(raw_delay))
                finally:
                    response.close()
            except requests.RequestException as error:
                queue.finish(delivery, "retry", error=type(error).__name__)
                logger.warning(
                    "Emergency delivery %s to %s failed (%s); retry retained",
                    delivery.delivery_id,
                    destination.name,
                    type(error).__name__,
                )
                continue
            if 200 <= code < 300:
                queue.finish(delivery, "delivered")
                logger.info(
                    "Emergency event %s delivered to %s (%s)", delivery.event_id, destination.name, delivery.severity
                )
            elif code in {408, 429} or 500 <= code < 600:
                queue.finish(delivery, "retry", error=f"HTTP {code}", retry_after=delay)
                logger.warning(
                    "Emergency delivery %s to %s returned %s; retry retained",
                    delivery.delivery_id,
                    destination.name,
                    code,
                )
            else:
                queue.finish(
                    delivery, "terminal", error=f"HTTP {code}; correct configuration/payload then explicitly retry"
                )
                logger.error(
                    "Emergency delivery %s to %s terminal HTTP %s; operator correction required",
                    delivery.delivery_id,
                    destination.name,
                    code,
                )
        if not progressed:
            break
    return len(attempted)


def dispatch_emergency_withdrawal(alert: Alert) -> None:
    """Persist fanout before sending; CRITICAL bypasses HIGH's destination cooldown."""
    if (
        alert.severity not in (AlertSeverity.HIGH, AlertSeverity.CRITICAL)
        or alert.protocol not in DISPATCHABLE_PROTOCOLS
    ):
        return
    if os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG":
        logger.debug("Skipping dispatch (LOG_LEVEL=DEBUG)")
        return
    configured = destinations()
    if not configured:
        logger.warning("No emergency webhook secrets configured; dispatch disabled")
        return
    queue = DispatchQueue()
    queue.enqueue(alert.protocol, alert.severity.value, alert.message, configured)
    drain_emergency_deliveries(queue=queue)

"""Scheduled emergency retries, independent of newly generated monitoring alerts."""

import argparse

from utils.dispatch import destinations, drain_emergency_deliveries
from utils.dispatch_queue import DispatchQueue
from utils.logger import get_logger

logger = get_logger("utils.dispatch_retry")


def main() -> None:
    """Retry due deliveries; expose retained terminal failures for operator correction."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retry-terminal", type=int, help="Explicitly retry a corrected terminal delivery, preserving its ID"
    )
    args = parser.parse_args()
    queue = DispatchQueue()
    if args.retry_terminal is not None:
        record = next((row for row in queue.records() if row["id"] == args.retry_terminal), None)
        target = next((item for item in destinations() if record and item.name == record["destination"]), None)
        if target is None:
            raise ValueError("configure the delivery destination before explicitly retrying")
        queue.retry_terminal(args.retry_terminal, url=target.url)
    count = drain_emergency_deliveries(queue=queue)
    records = queue.records()
    terminal = [row for row in records if row["status"] == "terminal"]
    configured = {item.name for item in destinations()}
    for row in records:
        if row["status"] in {"pending", "retry"} and row["destination"] not in configured:
            logger.warning(
                "Emergency delivery %s held: %s credentials are not configured", row["id"], row["destination"]
            )
    logger.info("Emergency outbox: attempted %s deliveries; %s terminal failures", count, len(terminal))
    for row in terminal:
        logger.warning(
            "Emergency delivery %s to %s needs correction: %s", row["id"], row["destination"], row["last_error"]
        )


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, "yearn", alert_protocol="yearn-internal")

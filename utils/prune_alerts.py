import os

from utils.logging import get_logger
from utils.runner import run_with_alert
from utils.store import checkpoint_wal, prune_alerts

logger = get_logger("utils.prune_alerts")


def main() -> None:
    days = int(os.getenv("ALERTS_RETENTION_DAYS", "30"))
    deleted = prune_alerts(days)
    logger.info("Pruned %d alert rows older than %d days", deleted, days)
    checkpoint_wal()


if __name__ == "__main__":
    run_with_alert(main, "automation")

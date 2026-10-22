#!/usr/bin/env python3
"""Continuous timelock event subscriber (observation pilot).

Runs alongside the hourly ``timelock_alerts.py`` and never replaces it: it
observes the same ``TimelockEvent`` rows over an Envio WebSocket subscription and
bounded HTTP reconciliation, records provenance and timing in a local SQLite
inbox, and sends no alerts. With ``--notify errors`` it posts diagnostic notices
to the internal errors chat. See ``protocols/timelock/README.md``.

Usage:
    python protocols/timelock/event_subscriber.py run [--transport both|ws|http]
    python protocols/timelock/event_subscriber.py reconcile-once
    python protocols/timelock/event_subscriber.py report [--hours 24] [--json]
    python protocols/timelock/event_subscriber.py health
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict

from dotenv import load_dotenv

from protocols.timelock.subscriber.envio import http_poster
from protocols.timelock.subscriber.inbox import EventInbox
from protocols.timelock.subscriber.notices import PilotNotices
from protocols.timelock.subscriber.notifier import NOTIFY_MODES, build_notifier
from protocols.timelock.subscriber.reconcile import ReconcileConfig, build_scope, reconcile_all
from protocols.timelock.subscriber.report import build_report, format_report, report_to_json
from protocols.timelock.subscriber.worker import TRANSPORTS, Worker, WorkerConfig
from protocols.timelock.timelock_alerts import TIMELOCK_LIST, TIMELOCKS
from utils.graphql_ws import http_to_ws_url
from utils.logger import get_logger

load_dotenv()

_logger = get_logger("timelock.event_subscriber")


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def _graphql_url() -> str:
    url = os.getenv("ENVIO_GRAPHQL_URL")
    if not url:
        raise SystemExit("ENVIO_GRAPHQL_URL is not set")
    return url


def _reconcile_config(args: argparse.Namespace) -> ReconcileConfig:
    return ReconcileConfig(
        page_size=args.page_size,
        max_chunks_per_cycle=args.max_chunks,
        start_lookback_seconds=args.start_lookback_seconds,
    )


def cmd_run(args: argparse.Namespace) -> None:
    """Run the worker until SIGTERM."""
    url = _graphql_url()
    ws_url = os.getenv("ENVIO_GRAPHQL_WS_URL") or http_to_ws_url(url)
    config = WorkerConfig(
        transport=args.transport,
        reconcile_interval=args.reconcile_interval,
        heartbeat_interval=args.heartbeat_interval,
        snapshot_limit=args.snapshot_limit,
        head_stall_seconds=args.head_stall_seconds,
        reconcile=_reconcile_config(args),
    )
    inbox = EventInbox()
    notifier = build_notifier(args.notify)
    notices = PilotNotices(notifier, inbox, TIMELOCKS) if notifier is not None else None
    worker = Worker(http_poster(url), ws_url, inbox, build_scope(TIMELOCK_LIST), config, notices=notices)
    _logger.info(
        "starting timelock subscriber: transport=%s reconcile every %.0fs notices=%s",
        config.transport,
        config.reconcile_interval,
        "errors chat" if notices else "off",
    )
    asyncio.run(worker.run())


def cmd_reconcile_once(args: argparse.Namespace) -> None:
    """Run one reconcile cycle and print its per-chain result."""
    report = reconcile_all(
        http_poster(_graphql_url()), EventInbox(), build_scope(TIMELOCK_LIST), _reconcile_config(args)
    )
    summary = {
        "ok": report.ok,
        "error": report.error,
        "duration_ms": round(report.duration_ms, 1),
        "chains": [asdict(c) for c in report.chains],
    }
    print(json.dumps(summary, indent=2, default=str))
    if not report.ok:
        sys.exit(1)


def cmd_report(args: argparse.Namespace) -> None:
    """Print the coverage/latency report."""
    report = build_report(EventInbox(), args.hours)
    print(json.dumps(report_to_json(report), indent=2) if args.json else format_report(report))


def cmd_health(args: argparse.Namespace) -> None:
    """Exit non-zero when the worker's heartbeat is stale, reconciliation failed, or a chain stalled."""
    status = EventInbox().status()
    problems: list[str] = []
    heartbeat, updated_at = status.get("heartbeat", (None, 0.0))
    if heartbeat is None:
        problems.append("no heartbeat recorded")
    else:
        age = time.time() - updated_at
        if age > 3 * float(heartbeat.get("heartbeat_interval") or 30):
            problems.append(f"heartbeat is {age:.0f}s old")
        if heartbeat.get("last_reconcile_ok") is False:
            problems.append("last reconcile failed")
        if heartbeat.get("transport") in ("both", "ws") and not heartbeat.get("ws_connected"):
            problems.append("WebSocket disconnected")
        problems += [f"chain {c['chain_id']} head stalled" for c in heartbeat.get("chains", []) if c.get("stalled")]
    print(json.dumps({"ok": not problems, "problems": problems, "heartbeat": heartbeat}, indent=2))
    if problems:
        sys.exit(1)


def main() -> None:
    """Parse arguments and dispatch the subcommand."""
    parser = argparse.ArgumentParser(description="Continuous timelock event subscriber (observation pilot).")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_reconcile_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--page-size", type=int, default=200, help="Rows per GraphQL page.")
        p.add_argument("--max-chunks", type=int, default=20, help="Block-range chunks per chain per cycle.")
        p.add_argument(
            "--start-lookback-seconds",
            type=int,
            default=int(_env_float("TIMELOCK_SUBSCRIBER_START_LOOKBACK_SECONDS", 0)),
            help="History to backfill on a chain's first reconcile (default 0: start at the indexed head).",
        )

    run = sub.add_parser("run", help="Run the worker until SIGTERM.")
    run.add_argument("--transport", choices=TRANSPORTS, default=os.getenv("TIMELOCK_SUBSCRIBER_TRANSPORT", "both"))
    run.add_argument(
        "--reconcile-interval", type=float, default=_env_float("TIMELOCK_SUBSCRIBER_RECONCILE_SECONDS", 60)
    )
    run.add_argument("--heartbeat-interval", type=float, default=30)
    run.add_argument("--snapshot-limit", type=int, default=50, help="Rows in each live-query snapshot.")
    run.add_argument("--head-stall-seconds", type=float, default=_env_float("TIMELOCK_SUBSCRIBER_STALL_SECONDS", 900))
    run.add_argument(
        "--notify",
        choices=NOTIFY_MODES,
        default=os.getenv("TIMELOCK_SUBSCRIBER_NOTIFY", "none"),
        help="Post pilot notices to the internal errors chat (errors) or not at all (none, default).",
    )
    add_reconcile_args(run)
    run.set_defaults(func=cmd_run)

    once = sub.add_parser("reconcile-once", help="Run one HTTP reconcile cycle.")
    add_reconcile_args(once)
    once.set_defaults(func=cmd_reconcile_once)

    report = sub.add_parser("report", help="Print the coverage/latency report.")
    report.add_argument("--hours", type=float, default=24)
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_report)

    health = sub.add_parser("health", help="Exit 1 when the worker is unhealthy.")
    health.set_defaults(func=cmd_health)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    # Crashes go to the internal ops feed only; the pilot itself sends no alerts.
    run_with_alert(main, "yearn", alert_protocol="yearn-internal")

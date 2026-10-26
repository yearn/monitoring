#!/usr/bin/env python3
"""Continuous event subscriber (observation pilot) for named Envio event streams.

Runs alongside the scheduled monitors and never replaces them: for each selected
stream (``timelock``: ``TimelockEvent``; ``morpho``: ``MorphoGovernanceEvent``) it
observes the entity over an Envio WebSocket subscription and bounded HTTP
reconciliation, records provenance and timing in the stream's own SQLite inbox,
and sends no alerts. With ``--notify errors`` it posts diagnostic notices to the
internal errors chat. See ``protocols/timelock/README.md``.

The script lives here because the timelock pilot came first and its systemd unit
runs this path; every stream shares the process, the HTTP session, and the
chain-head reads.

Usage:
    python protocols/timelock/event_subscriber.py [--streams timelock,morpho] run [--transport both|ws|http]
    python protocols/timelock/event_subscriber.py [--streams ...] reconcile-once
    python protocols/timelock/event_subscriber.py [--streams ...] report [--hours 24] [--json]
    python protocols/timelock/event_subscriber.py [--streams ...] health
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any

from dotenv import load_dotenv

from protocols.morpho.governance_stream import MORPHO_GOVERNANCE_STREAM, morpho_governance_scope
from protocols.timelock.event_stream import TIMELOCK_STREAM, timelock_scope
from utils.graphql_ws import http_to_ws_url
from utils.logger import get_logger
from utils.subscriber.envio import SharedChainHeads, http_poster
from utils.subscriber.inbox import EventInbox
from utils.subscriber.notices import PilotNotices
from utils.subscriber.notifier import NOTIFY_MODES, build_notifier
from utils.subscriber.reconcile import ReconcileConfig, reconcile_all
from utils.subscriber.report import build_report, format_report, report_to_json
from utils.subscriber.stream import EventStream, StreamScope
from utils.subscriber.worker import TRANSPORTS, Worker, WorkerConfig, run_workers

load_dotenv()

_logger = get_logger("timelock.event_subscriber")

# Streams this process can follow. Morpho needs an Envio deployment that serves
# MorphoGovernanceEvent (yearn-envio#71); until then leave it out of --streams.
STREAMS: dict[str, tuple[EventStream, Callable[[], StreamScope]]] = {
    TIMELOCK_STREAM.name: (TIMELOCK_STREAM, timelock_scope),
    MORPHO_GOVERNANCE_STREAM.name: (MORPHO_GOVERNANCE_STREAM, morpho_governance_scope),
}
DEFAULT_STREAMS = TIMELOCK_STREAM.name


def parse_streams(value: str) -> list[str]:
    """Parse a comma-separated stream list, keeping order and dropping duplicates.

    Raises:
        argparse.ArgumentTypeError: A name is not in ``STREAMS`` or the list is empty.
    """
    names = list(dict.fromkeys(name.strip() for name in value.split(",") if name.strip()))
    unknown = [name for name in names if name not in STREAMS]
    if unknown or not names:
        raise argparse.ArgumentTypeError(f"streams must be a non-empty subset of {sorted(STREAMS)}, got {value!r}")
    return names


def _selected(args: argparse.Namespace) -> list[tuple[EventStream, StreamScope]]:
    """Return each selected stream with its monitored scope."""
    return [(STREAMS[name][0], STREAMS[name][1]()) for name in args.streams]


def _env_float(name: str, default: float) -> float:
    """Read a float environment variable, falling back to ``default`` when unset or empty."""
    value = os.getenv(name)
    return float(value) if value else default


def _graphql_url() -> str:
    """Return ``ENVIO_GRAPHQL_URL`` or exit."""
    url = os.getenv("ENVIO_GRAPHQL_URL")
    if not url:
        raise SystemExit("ENVIO_GRAPHQL_URL is not set")
    return url


def _reconcile_config(args: argparse.Namespace) -> ReconcileConfig:
    """Build the reconcile bounds from the CLI arguments."""
    return ReconcileConfig(
        page_size=args.page_size,
        max_chunks_per_cycle=args.max_chunks,
        start_lookback_seconds=args.start_lookback_seconds,
    )


def cmd_run(args: argparse.Namespace) -> None:
    """Run one worker per selected stream in this process until SIGTERM."""
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
    post = http_poster(url)
    # One chain_metadata read per cycle for every stream.
    heads = SharedChainHeads(post)
    workers = []
    for stream, scope in _selected(args):
        inbox = EventInbox(stream)
        notifier = build_notifier(args.notify, stream.name)
        notices = PilotNotices(notifier, inbox, scope.labels) if notifier is not None else None
        workers.append(Worker(post, ws_url, inbox, scope.addresses, config, notices=notices, heads=heads))
        _logger.info(
            "starting %s stream: %s chains, transport=%s reconcile every %.0fs notices=%s",
            stream.name,
            len(scope.addresses),
            config.transport,
            config.reconcile_interval,
            "errors chat" if notices else "off",
        )
    asyncio.run(run_workers(workers, watchdog_interval=config.heartbeat_interval))


def cmd_reconcile_once(args: argparse.Namespace) -> None:
    """Run one reconcile cycle per selected stream and print each per-chain result."""
    post = http_poster(_graphql_url())
    heads = SharedChainHeads(post)
    summaries = {}
    ok = True
    for stream, scope in _selected(args):
        report = reconcile_all(post, EventInbox(stream), scope.addresses, _reconcile_config(args), heads=heads)
        ok = ok and report.ok
        summaries[stream.name] = {
            "ok": report.ok,
            "error": report.error,
            "duration_ms": round(report.duration_ms, 1),
            "chains": [asdict(c) for c in report.chains],
        }
    print(json.dumps(summaries, indent=2, default=str))
    if not ok:
        sys.exit(1)


def cmd_report(args: argparse.Namespace) -> None:
    """Print the coverage/latency report of every selected stream."""
    reports = {stream.name: build_report(EventInbox(stream), args.hours) for stream, _ in _selected(args)}
    if args.json:
        print(json.dumps({name: report_to_json(r) for name, r in reports.items()}, indent=2))
    else:
        print("\n\n".join(format_report(r) for r in reports.values()))


def heartbeat_problems(status: Mapping[str, tuple[Any, float]], now: float) -> list[str]:
    """Return what is wrong with one stream's stored heartbeat, if anything."""
    heartbeat, updated_at = status.get("heartbeat", (None, 0.0))
    if not isinstance(heartbeat, dict):
        return ["no heartbeat recorded"]
    problems: list[str] = []
    age = now - updated_at
    if age > 3 * float(heartbeat.get("heartbeat_interval") or 30):
        problems.append(f"heartbeat is {age:.0f}s old")
    if heartbeat.get("last_reconcile_ok") is False:
        problems.append("last reconcile failed")
    if heartbeat.get("transport") in ("both", "ws") and not heartbeat.get("ws_connected"):
        problems.append("WebSocket disconnected")
    problems += [f"chain {c['chain_id']} head stalled" for c in heartbeat.get("chains", []) if c.get("stalled")]
    return problems


def cmd_health(args: argparse.Namespace) -> None:
    """Exit non-zero when any selected stream's heartbeat is stale, reconciliation failed, or a chain stalled."""
    now = time.time()
    streams = {}
    problems: list[str] = []
    for stream, _ in _selected(args):
        status = EventInbox(stream).status()
        stream_problems = heartbeat_problems(status, now)
        problems += [f"{stream.name}: {problem}" for problem in stream_problems]
        streams[stream.name] = {"problems": stream_problems, "heartbeat": status.get("heartbeat", (None, 0.0))[0]}
    print(json.dumps({"ok": not problems, "problems": problems, "streams": streams}, indent=2))
    if problems:
        sys.exit(1)


def main() -> None:
    """Parse arguments and dispatch the subcommand."""
    parser = argparse.ArgumentParser(description="Continuous event subscriber (observation pilot).")
    parser.add_argument(
        "--streams",
        type=parse_streams,
        default=os.getenv("EVENT_SUBSCRIBER_STREAMS", DEFAULT_STREAMS),
        help=f"Comma-separated streams to follow, from {sorted(STREAMS)} (default: {DEFAULT_STREAMS}).",
    )
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

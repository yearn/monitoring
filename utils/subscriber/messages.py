"""Plain-text notices the subscriber pilot posts to the internal errors chat.

They are diagnostics for comparing the pilot with the scheduled monitors, not
alerts: short, plain text, with full addresses and explorer links. Text that
names the event kind uses the stream's noun ("timelock", "Morpho governance").
"""

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from utils.chains import EXPLORER_URLS, Chain
from utils.formatting import format_duration
from utils.subscriber.report import LatencyStats, PilotReport
from utils.subscriber.stream import EventStream


def chain_name(chain_id: int) -> str:
    """Return a readable chain name, falling back to the id."""
    try:
        return Chain.from_chain_id(chain_id).network_name.capitalize()
    except ValueError:
        return f"Chain {chain_id}"


def _address_line(label: str, address: str, chain_id: int) -> str:
    """Render ``label: address`` with an explorer link when the chain has one."""
    explorer = EXPLORER_URLS.get(chain_id)
    return f"{label}: {address}" + (f" {explorer}/address/{address}" if explorer else "")


def format_new_event(row: sqlite3.Row, labels: Mapping[tuple[str, int], Any], stream: EventStream) -> str:
    """Describe one newly observed event: what, where, which transport, and how late.

    Args:
        row: An inbox row (``<prefix>_events``).
        labels: ``(lowercase address, chain id)`` → object with ``protocol`` and ``label``.
        stream: The stream the row belongs to.
    """
    chain_id = int(row["chain_id"])
    address = str(row["contract_address"])
    config = labels.get((address.lower(), chain_id))
    name = f"{config.label} ({config.protocol})" if config else f"Unknown {stream.address_label.lower()}"
    payload = json.loads(row["payload_json"])
    delay = row["first_seen_at"] - row["block_timestamp"]
    explorer = EXPLORER_URLS.get(chain_id)
    tx = str(payload.get("transactionHash") or row["transaction_hash"])
    lines = [
        f"🛰️ New {stream.noun} event via {str(row['first_source']).upper()}, "
        f"{format_duration(int(delay))} after its block",
        f"{name} on {chain_name(chain_id)}: {row['event_name']} {row['event_id']}",
        _address_line(stream.address_label, address, chain_id),
        f"Tx: {tx}" + (f" {explorer}/tx/{tx}" if explorer else ""),
    ]
    if row["operation_id"]:
        lines.append(f"Operation ID: {row['operation_id']}")
    if stream.describe is not None:
        lines.extend(stream.describe(payload))
    return "\n".join(lines)


def format_event_burst(chain_id: int, source: str, event_ids: Sequence[str], stream: EventStream) -> str:
    """Summarize many new events at once (a backlog) instead of one notice each."""
    shown = ", ".join(event_ids[:20]) + (f" … (+{len(event_ids) - 20})" if len(event_ids) > 20 else "")
    return f"🛰️ {len(event_ids)} new {stream.noun} events on {chain_name(chain_id)} via {source.upper()}: {shown}"


def format_ws_miss(chain_id: int, event_ids: Sequence[str]) -> str:
    """HTTP found events first although the subscription was connected."""
    return (
        f"⚠️ WS miss: HTTP reconciliation found {len(event_ids)} event(s) on {chain_name(chain_id)} "
        f"before the connected WebSocket: {', '.join(event_ids[:20])}"
    )


def format_changes(chain_id: int, source: str, changed: int, restored: int, removed: int, stream: EventStream) -> str:
    """Indexed rows changed, vanished, or came back: a reorg or an indexer rewrite."""
    return (
        f"🔀 Indexed {stream.noun} events changed on {chain_name(chain_id)} (seen via {source.upper()}): "
        f"{changed} changed, {removed} removed, {restored} restored. Details: {stream.table('changes')}."
    )


def format_ws_down(seconds: float, error: str | None) -> str:
    """The subscription has been down longer than the notice threshold."""
    return f"🔌 WebSocket down for {format_duration(int(seconds))}; last error: {error or 'n/a'}"


def format_ws_up(downtime: float) -> str:
    """The subscription is back after a noticed outage."""
    return f"✅ WebSocket reconnected after {format_duration(int(downtime))}"


def format_stalled(chain_id: int, head: int, age: float) -> str:
    """A chain's indexed head stopped advancing."""
    return f"🐢 Envio head for {chain_name(chain_id)} stuck at block {head} for {format_duration(int(age))}"


def format_unstalled(chain_id: int) -> str:
    """A stalled chain's indexed head advances again."""
    return f"✅ Envio head for {chain_name(chain_id)} advancing again"


def format_reconcile_failing(failures: int, error: str | None) -> str:
    """HTTP reconciliation failed several cycles in a row."""
    return f"❌ HTTP reconciliation failed {failures} cycles in a row: {(error or 'n/a')[:500]}"


def format_reconcile_recovered(failures: int) -> str:
    """HTTP reconciliation succeeded again."""
    return f"✅ HTTP reconciliation recovered after {failures} failed cycles"


def _lat(stats: LatencyStats) -> str:
    """Render latency percentiles, or n/a without samples."""
    return "n/a" if not stats.count else f"p50 {stats.p50}s / p95 {stats.p95}s (n={stats.count})"


def format_daily_summary(report: PilotReport, stream: EventStream) -> str:
    """Condense a pilot report to a few lines."""
    coverage = "n/a" if report.ws_coverage is None else f"{report.ws_coverage:.1%}"
    return "\n".join(
        [
            f"📊 {stream.noun[:1].upper()}{stream.noun[1:]} subscriber, last 24h",
            f"Events {report.events} (HTTP-confirmed {report.confirmed_by_http}), WS coverage {coverage}, "
            f"WS-only {report.ws_only}, HTTP-only {report.http_only}",
            f"Delay WS {_lat(report.ws_latency)}; HTTP {_lat(report.http_latency)}; WS lead {_lat(report.ws_lead)}",
            f"WS outages {len(report.outages)}, downtime {format_duration(int(report.ws_downtime_seconds))}; "
            f"reconcile errors {report.reconcile_errors}/{report.reconcile_cycles}",
            f"Changed {report.changed}, removed {report.removed}, restored {report.restored}",
            f"Recommendation: {report.recommendation}",
        ]
    )

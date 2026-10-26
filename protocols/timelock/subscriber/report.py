"""Coverage and latency report for the timelock subscriber pilot.

Answers the pilot's questions from the inbox alone:

- Coverage: of the events HTTP reconciliation confirmed, how many did the
  WebSocket subscription see, and did HTTP miss anything the WebSocket saw?
- Latency: block timestamp to first sighting, per transport (p50/p95/max). The
  faster transport bounds Envio's indexing delay from above; Envio exposes no
  per-row index time, so indexing and delivery delay are not separable here.
- Recovery: WebSocket outages, and how fast events from inside an outage arrived.
- Integrity: duplicate snapshots absorbed, and changed/removed/restored rows.

It ends with a transport recommendation for production.
"""

import json
import math
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from protocols.timelock.subscriber.inbox import EventInbox

# Below this many events the latency percentiles mean little; keep collecting.
MIN_EVENTS_FOR_DECISION = 5
# The subscription must see at least this share of confirmed events to be relied on.
MIN_WS_COVERAGE = 0.99


@dataclass
class LatencyStats:
    """Delay percentiles in seconds."""

    count: int = 0
    p50: float | None = None
    p95: float | None = None
    max: float | None = None


@dataclass
class Outage:
    """One WebSocket disconnection window."""

    start: float
    end: float | None
    error: str | None
    # True when the outage began before the report window; ``start`` is then the window start.
    clipped: bool = False
    events_inside: int = 0
    recovered_by_ws: int = 0
    recovered_by_http: int = 0
    max_recovery_delay: float | None = None


@dataclass
class PilotReport:
    """Everything the pilot measured over a window."""

    window_start: float
    window_end: float
    events: int = 0
    confirmed_by_http: int = 0
    pending_http: int = 0
    seen_by_ws: int = 0
    seen_by_both: int = 0
    ws_only: int = 0
    http_only: int = 0
    ws_coverage: float | None = None
    removed: int = 0
    changed: int = 0
    restored: int = 0
    ws_duplicate_observations: int = 0
    http_duplicate_observations: int = 0
    ws_latency: LatencyStats = field(default_factory=LatencyStats)
    http_latency: LatencyStats = field(default_factory=LatencyStats)
    first_seen_latency: LatencyStats = field(default_factory=LatencyStats)
    ws_lead: LatencyStats = field(default_factory=LatencyStats)
    ws_connects: int = 0
    ws_disconnects: int = 0
    ws_downtime_seconds: float = 0.0
    outages: list[Outage] = field(default_factory=list)
    reconcile_cycles: int = 0
    reconcile_errors: int = 0
    reconcile_ms: LatencyStats = field(default_factory=LatencyStats)
    recommendation: str = ""
    rationale: str = ""


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def latency_stats(values: Sequence[float]) -> LatencyStats:
    """Summarize delays (seconds)."""
    if not values:
        return LatencyStats()
    return LatencyStats(
        count=len(values),
        p50=round(percentile(values, 0.5) or 0.0, 2),
        p95=round(percentile(values, 0.95) or 0.0, 2),
        max=round(max(values), 2),
    )


_CONNECTION_KINDS = ("ws_connected", "ws_disconnected", "worker_started", "worker_stopped")


def _opens_outage(entry: Any) -> bool:
    """True for a log entry after which no subscription is live until the next connect."""
    if entry["kind"] == "ws_connected":
        return False
    if entry["kind"] == "worker_started":
        return json.loads(entry["details_json"] or "{}").get("transport") != "http"
    return True


def _outages(inbox: EventInbox, since: float, until: float) -> list[Outage]:
    """Rebuild WebSocket outage windows from the worker log, clipped to ``[since, until]``.

    An outage runs from a disconnect (or worker start or stop) to the next connect.
    Worker restarts count too: nothing is subscribed while the worker is down. The
    connection state at ``since`` comes from the last log entry before it, so an
    outage that began earlier and still covers the window is reported from ``since``.
    """
    outages: list[Outage] = []
    current: Outage | None = None
    before = inbox.last_log_entry_before(since, _CONNECTION_KINDS)
    if before is not None and _opens_outage(before):
        error = json.loads(before["details_json"] or "{}").get("error") or before["kind"]
        current = Outage(start=since, end=None, error=error, clipped=True)
    for entry in inbox.log_entries(since, _CONNECTION_KINDS):
        kind, at = entry["kind"], entry["at"]
        if at > until:
            break
        if current is None and _opens_outage(entry):
            details = json.loads(entry["details_json"] or "{}")
            current = Outage(start=at, end=None, error=details.get("error") or kind)
        elif kind == "ws_connected" and current is not None:
            current.end = at
            outages.append(current)
            current = None
    if current is not None:
        outages.append(current)
    return outages


def _recommend(report: PilotReport) -> tuple[str, str]:
    if report.events < MIN_EVENTS_FOR_DECISION or report.seen_by_ws == 0:
        return (
            "undecided: keep the pilot running",
            f"{report.events} events in the window, {report.seen_by_ws} seen over WebSocket; "
            f"need at least {MIN_EVENTS_FOR_DECISION} with WebSocket sightings to compare transports.",
        )
    ws_p95 = report.ws_latency.p95
    http_p95 = report.http_latency.p95
    coverage = report.ws_coverage or 0.0
    if coverage >= MIN_WS_COVERAGE and ws_p95 is not None and (http_p95 is None or ws_p95 < http_p95):
        return (
            "both: WebSocket for latency, HTTP reconciliation for completeness",
            f"WebSocket saw {coverage:.1%} of confirmed events with p95 {ws_p95}s vs HTTP p95 {http_p95}s. "
            "HTTP reconciliation stays on regardless: it is the completeness guarantee, and Envio supports "
            "hosted WebSockets fully only on Dedicated plans.",
        )
    return (
        "http: HTTP polling only",
        f"WebSocket coverage {coverage:.1%} (needs {MIN_WS_COVERAGE:.0%}) and p95 {ws_p95}s vs HTTP p95 {http_p95}s "
        "do not justify an extra transport on a plan without full WebSocket support.",
    )


def build_report(inbox: EventInbox, hours: float, now: float | None = None) -> PilotReport:
    """Compute the pilot report over events with block timestamps in the last ``hours``."""
    end = now if now is not None else time.time()
    start = end - hours * 3600
    report = PilotReport(window_start=start, window_end=end)
    checkpoints = {cp.chain_id: cp.block_number for cp in inbox.checkpoints()}

    ws_delays: list[float] = []
    http_delays: list[float] = []
    first_delays: list[float] = []
    leads: list[float] = []
    rows = inbox.events_since(int(start))
    active = []
    for row in rows:
        if row["status"] == "removed":
            report.removed += 1
            continue
        active.append(row)
        block_ts = row["block_timestamp"]
        ws_at, http_at = row["ws_first_seen_at"], row["http_first_seen_at"]
        report.ws_duplicate_observations += max(0, row["ws_observations"] - 1)
        report.http_duplicate_observations += max(0, row["http_observations"] - 1)
        first_delays.append(row["first_seen_at"] - block_ts)
        if ws_at is not None:
            report.seen_by_ws += 1
            ws_delays.append(ws_at - block_ts)
        if http_at is not None:
            report.confirmed_by_http += 1
            http_delays.append(http_at - block_ts)
        elif row["block_number"] > checkpoints.get(row["chain_id"], -1):
            report.pending_http += 1  # above the checkpoint: HTTP has not scanned it yet
        if ws_at is not None and http_at is not None:
            report.seen_by_both += 1
            leads.append(http_at - ws_at)
        elif ws_at is not None and row["block_number"] <= checkpoints.get(row["chain_id"], -1):
            report.ws_only += 1  # HTTP scanned this block and did not return it
        elif http_at is not None:
            report.http_only += 1

    report.events = len(active)
    if report.confirmed_by_http:
        report.ws_coverage = round(report.seen_by_both / report.confirmed_by_http, 4)
    report.ws_latency = latency_stats(ws_delays)
    report.http_latency = latency_stats(http_delays)
    report.first_seen_latency = latency_stats(first_delays)
    report.ws_lead = latency_stats(leads)

    for change in inbox.changes_since(start):
        if change["kind"] == "changed":
            report.changed += 1
        elif change["kind"] == "restored":
            report.restored += 1

    report.ws_connects = len(inbox.log_entries(start, ("ws_connected",)))
    report.ws_disconnects = len(inbox.log_entries(start, ("ws_disconnected",)))
    report.outages = _outages(inbox, start, end)
    for outage in report.outages:
        outage_end = outage.end if outage.end is not None else end
        report.ws_downtime_seconds += outage_end - outage.start
        delays = []
        for row in active:
            # Block timestamps are whole seconds; compare at that resolution.
            if int(outage.start) <= row["block_timestamp"] <= outage_end:
                outage.events_inside += 1
                if row["first_source"] == "ws":
                    outage.recovered_by_ws += 1
                else:
                    outage.recovered_by_http += 1
                delays.append(row["first_seen_at"] - row["block_timestamp"])
        outage.max_recovery_delay = round(max(delays), 2) if delays else None
    report.ws_downtime_seconds = round(report.ws_downtime_seconds, 1)

    cycles = inbox.log_entries(start, ("reconcile",))
    report.reconcile_cycles = len(cycles)
    durations = []
    for cycle in cycles:
        durations.append(cycle["duration_ms"] or 0.0)
        details = json.loads(cycle["details_json"] or "{}")
        if details.get("error") or any(c.get("error") for c in details.get("chains", [])):
            report.reconcile_errors += 1
    report.reconcile_ms = latency_stats(durations)

    report.recommendation, report.rationale = _recommend(report)
    return report


def report_to_json(report: PilotReport) -> dict[str, Any]:
    """Serialize a report for ``--json`` output."""
    return asdict(report)


def format_report(report: PilotReport) -> str:
    """Render a report as plain text for the terminal."""

    def lat(stats: LatencyStats, unit: str = "s") -> str:
        if not stats.count:
            return "n/a"
        return f"p50 {stats.p50}{unit}, p95 {stats.p95}{unit}, max {stats.max}{unit} (n={stats.count})"

    window = (
        f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(report.window_start))} → "
        f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(report.window_end))} UTC"
    )
    lines = [
        f"Timelock subscriber pilot report ({window})",
        "",
        "Coverage",
        f"  events: {report.events} (confirmed by HTTP {report.confirmed_by_http}, pending HTTP {report.pending_http})",
        f"  seen by WS: {report.seen_by_ws}; both: {report.seen_by_both}; WS only: {report.ws_only}; "
        f"HTTP only: {report.http_only}",
        f"  WS coverage of HTTP-confirmed events: "
        f"{'n/a' if report.ws_coverage is None else f'{report.ws_coverage:.1%}'}",
        f"  duplicate observations absorbed: WS {report.ws_duplicate_observations}, "
        f"HTTP {report.http_duplicate_observations}",
        f"  changed: {report.changed}, removed: {report.removed}, restored: {report.restored}",
        "",
        "Latency (block timestamp → first sighting)",
        f"  WS:         {lat(report.ws_latency)}",
        f"  HTTP:       {lat(report.http_latency)}",
        f"  first seen: {lat(report.first_seen_latency)} (upper bound on Envio indexing delay)",
        f"  WS lead over HTTP: {lat(report.ws_lead)}",
        "",
        "Recovery",
        f"  WS connects {report.ws_connects}, disconnects {report.ws_disconnects}, "
        f"downtime {report.ws_downtime_seconds}s",
    ]
    for outage in report.outages:
        end = "ongoing" if outage.end is None else f"{outage.end - outage.start:.0f}s"
        if outage.clipped:
            end += ", began before the window"
        lines.append(
            f"  - outage at {time.strftime('%H:%M:%S', time.gmtime(outage.start))} ({end}, {outage.error}): "
            f"{outage.events_inside} events, {outage.recovered_by_ws} via WS, {outage.recovered_by_http} via HTTP, "
            f"max delay {'n/a' if outage.max_recovery_delay is None else f'{outage.max_recovery_delay}s'}"
        )
    lines += [
        f"  reconcile cycles {report.reconcile_cycles}, with errors {report.reconcile_errors}, "
        f"duration {lat(report.reconcile_ms, 'ms')}",
        "",
        f"Recommendation: {report.recommendation}",
        f"  {report.rationale}",
    ]
    return "\n".join(lines)

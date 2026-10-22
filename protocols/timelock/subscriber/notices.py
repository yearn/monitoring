"""Decides which pilot notices to post to the errors chat, and when.

The worker calls these hooks at the points where things happen; this class turns
them into deduplicated notices on a ``Notifier``:

- every newly observed event once, at first sighting (a burst becomes one summary);
- HTTP finding events first while the WebSocket was connected (a WS miss);
- changed, removed, or restored rows (reorgs);
- WebSocket down for longer than a threshold, and its recovery;
- a chain's indexed head stalling, and recovering;
- HTTP reconciliation failing several cycles in a row, and recovering;
- a daily report summary.

Hooks that read the inbox run their queries in a thread; posting never blocks.
"""

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from protocols.timelock.subscriber import messages
from protocols.timelock.subscriber.inbox import EventInbox
from protocols.timelock.subscriber.notifier import Notifier
from protocols.timelock.subscriber.reconcile import ReconcileReport
from protocols.timelock.subscriber.report import build_report

SUMMARY_STATUS_KEY = "last_summary_at"
# When notices were first enabled on this inbox. Events from blocks before it are
# history (startup snapshot, backfill, enabling notices on an existing pilot) and
# are never announced; events after it are, even if first seen after a restart.
BASELINE_STATUS_KEY = "notices_baseline_at"


@dataclass(frozen=True)
class NoticeConfig:
    """Thresholds for pilot notices."""

    # Above this many new events in one batch, post one summary instead of a notice each.
    max_event_notices: int = 10
    ws_down_seconds: float = 300.0
    reconcile_failures: int = 3
    summary_interval: float = 86_400.0


def _chain_of(event_id: str) -> int:
    return int(event_id.split("_", 1)[0])


class PilotNotices:
    """Notice state and policy for one worker run."""

    def __init__(
        self,
        notifier: Notifier,
        inbox: EventInbox,
        timelocks: Mapping[tuple[str, int], Any],
        config: NoticeConfig = NoticeConfig(),
        now: Callable[[], float] = time.time,
    ) -> None:
        self.notifier = notifier
        self.inbox = inbox
        self.timelocks = timelocks
        self.config = config
        self.ws_down_since: float | None = None
        self.ws_last_error: str | None = None
        self.ws_down_notified = False
        self.reconcile_failures = 0
        self.reconcile_failure_notified = False
        self.now = now
        self.baseline: float | None = None

    # ── events ────────────────────────────────────────────────────────

    async def start(self) -> float:
        """Load the notice baseline, recording it on the first run with notices enabled."""
        if self.baseline is None:
            status = await asyncio.to_thread(self.inbox.status)
            stored = status.get(BASELINE_STATUS_KEY, (None, 0.0))[0]
            if stored is None:
                stored = self.now()
                await asyncio.to_thread(self.inbox.set_status, BASELINE_STATUS_KEY, stored, stored)
            self.baseline = float(stored)
        return self.baseline

    async def _announceable(self, event_ids: list[str] | tuple[str, ...]) -> list[str]:
        """Return the ids whose block is at or after the baseline, in order."""
        if not event_ids:
            return []
        baseline = await self.start()
        rows = await asyncio.to_thread(self.inbox.get_events, event_ids)
        # Block timestamps are whole seconds; a block in the baseline's own second counts.
        return [row["event_id"] for row in rows if row["block_timestamp"] >= int(baseline)]

    async def new_events(self, source: str, event_ids: list[str] | tuple[str, ...]) -> None:
        """Post a notice per newly observed event, or one summary per chain for a burst.

        Events from blocks before the baseline are skipped: the first snapshot of a
        fresh pilot holds the newest historical events, which are not news.
        """
        event_ids = await self._announceable(event_ids)
        if not event_ids:
            return
        if len(event_ids) > self.config.max_event_notices:
            by_chain: dict[int, list[str]] = {}
            for event_id in event_ids:
                by_chain.setdefault(_chain_of(event_id), []).append(event_id)
            for chain_id, ids in sorted(by_chain.items()):
                self.notifier.post(messages.format_event_burst(chain_id, source, ids))
            return
        for row in await asyncio.to_thread(self.inbox.get_events, event_ids):
            self.notifier.post(messages.format_new_event(row, self.timelocks))

    def snapshot_changes(self, changed_ids: list[str] | tuple[str, ...]) -> None:
        """Post one notice per chain for rows a WebSocket snapshot showed changed or restored."""
        by_chain: dict[int, int] = {}
        for event_id in changed_ids:
            by_chain[_chain_of(event_id)] = by_chain.get(_chain_of(event_id), 0) + 1
        for chain_id, count in sorted(by_chain.items()):
            self.notifier.post(messages.format_changes(chain_id, "ws", changed=count, restored=0, removed=0))

    async def reconcile(self, report: ReconcileReport, ws_expected: bool) -> None:
        """Post notices for one reconcile cycle: new events, WS misses, reorgs, failure streaks."""
        for chain in report.chains:
            if chain.initialized:
                continue  # first run or backfill: history, not news
            if chain.new_ids:
                await self.new_events("http", chain.new_ids)
                missed = await self._announceable(chain.new_ids)
                if ws_expected and missed:
                    self.notifier.post(messages.format_ws_miss(chain.chain_id, missed))
            if chain.changed or chain.restored or chain.removed:
                self.notifier.post(
                    messages.format_changes(chain.chain_id, "http", chain.changed, chain.restored, chain.removed)
                )

        if report.ok:
            if self.reconcile_failure_notified:
                self.notifier.post(messages.format_reconcile_recovered(self.reconcile_failures))
            self.reconcile_failures = 0
            self.reconcile_failure_notified = False
            return
        self.reconcile_failures += 1
        if self.reconcile_failures >= self.config.reconcile_failures and not self.reconcile_failure_notified:
            error = report.error or "; ".join(f"chain {c.chain_id}: {c.error}" for c in report.chains if c.error)
            self.notifier.post(messages.format_reconcile_failing(self.reconcile_failures, error))
            self.reconcile_failure_notified = True

    # ── WebSocket ─────────────────────────────────────────────────────

    def ws_disconnected(self, now: float, error: str | None) -> None:
        """Start (or continue) an outage."""
        if self.ws_down_since is None:
            self.ws_down_since = now
        self.ws_last_error = error

    def ws_connected(self, now: float) -> None:
        """End an outage; post the recovery if its start was announced."""
        if self.ws_down_notified and self.ws_down_since is not None:
            self.notifier.post(messages.format_ws_up(now - self.ws_down_since))
        self.ws_down_since = None
        self.ws_down_notified = False

    def check_ws(self, now: float) -> None:
        """Announce an outage once it outlasts the threshold."""
        if self.ws_down_since is None or self.ws_down_notified:
            return
        if now - self.ws_down_since >= self.config.ws_down_seconds:
            self.notifier.post(messages.format_ws_down(now - self.ws_down_since, self.ws_last_error))
            self.ws_down_notified = True

    # ── heartbeat ─────────────────────────────────────────────────────

    def stall_changed(self, chain_id: int, stalled: bool, head: int, age: float) -> None:
        """Announce a chain's indexed head stalling or advancing again."""
        if stalled:
            self.notifier.post(messages.format_stalled(chain_id, head, age))
        else:
            self.notifier.post(messages.format_unstalled(chain_id))

    async def maybe_daily_summary(self, now: float) -> None:
        """Post the report summary once per ``summary_interval``, surviving restarts."""
        status = await asyncio.to_thread(self.inbox.status)
        last = status.get(SUMMARY_STATUS_KEY, (None, 0.0))[0]
        if last is None:
            # First run: schedule the first summary one interval from now.
            await asyncio.to_thread(self.inbox.set_status, SUMMARY_STATUS_KEY, now, now)
            return
        if now - float(last) < self.config.summary_interval:
            return
        report = await asyncio.to_thread(build_report, self.inbox, self.config.summary_interval / 3600, now)
        self.notifier.post(messages.format_daily_summary(report))
        await asyncio.to_thread(self.inbox.set_status, SUMMARY_STATUS_KEY, now, now)

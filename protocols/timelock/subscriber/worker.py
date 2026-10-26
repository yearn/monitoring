"""Continuous timelock event worker: WebSocket subscription, HTTP reconciliation, heartbeat.

Three asyncio tasks share one ``EventInbox``:

- ``ws``: holds a ``graphql-transport-ws`` live query over the newest monitored
  events and folds every snapshot into the inbox, reconnecting with capped
  exponential backoff. Each connect and disconnect is logged for the report.
- ``reconcile``: runs ``reconcile_all`` every ``reconcile_interval`` seconds, which
  is what makes the inbox complete regardless of the WebSocket.
- ``heartbeat``: writes worker status, flags chains whose indexed head stopped
  advancing, and pings the systemd watchdog only while reconciliation keeps
  completing, so a wedged worker is restarted.

The pilot sends no alerts. With an optional ``PilotNotices`` it posts diagnostic
notices to the internal errors chat from a background queue (see ``notices.py``);
without one, all output stays in journald and the inbox.
"""

import asyncio
import random
import signal
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from protocols.timelock.subscriber.envio import GraphQLPost, address_variants, live_subscription
from protocols.timelock.subscriber.inbox import SOURCE_HTTP, SOURCE_WS, EventInbox
from protocols.timelock.subscriber.notices import PilotNotices
from protocols.timelock.subscriber.reconcile import ReconcileConfig, ReconcileReport, in_scope, reconcile_all
from utils.graphql_ws import GraphQLWSError, subscribe
from utils.logger import get_logger
from utils.sd_notify import notify

_logger = get_logger("timelock.subscriber.worker")

TRANSPORTS = ("both", "ws", "http")

SubscribeFn = Callable[..., AsyncIterator[dict[str, Any]]]

# Doublings beyond this cannot matter (2**20 x any sane initial delay exceeds any
# cap) and keep ``2 ** n`` from overflowing a float after ~1,000 failures.
_MAX_BACKOFF_DOUBLINGS = 20


def backoff_delay(failures: int, initial: float, cap: float) -> float:
    """Return the reconnect delay before jitter: ``initial * 2**(failures-1)``, capped at ``cap``."""
    return min(cap, initial * 2 ** min(max(failures - 1, 0), _MAX_BACKOFF_DOUBLINGS))


@dataclass(frozen=True)
class WorkerConfig:
    """Timing knobs for the worker loops."""

    transport: str = "both"
    reconcile_interval: float = 60.0
    heartbeat_interval: float = 30.0
    snapshot_limit: int = 50
    backoff_initial: float = 1.0
    backoff_max: float = 60.0
    # A chain whose indexed head has not advanced for this long is reported stalled.
    head_stall_seconds: float = 900.0
    reconcile: ReconcileConfig = ReconcileConfig()


class Worker:
    """Runs the subscriber loops until ``stop()`` (or SIGTERM/SIGINT)."""

    def __init__(
        self,
        post: GraphQLPost,
        ws_url: str | None,
        inbox: EventInbox,
        scope: dict[int, set[str]],
        config: WorkerConfig,
        subscribe_fn: SubscribeFn = subscribe,
        now: Callable[[], float] = time.time,
        notices: PilotNotices | None = None,
    ) -> None:
        if config.transport not in TRANSPORTS:
            raise ValueError(f"transport must be one of {TRANSPORTS}, got {config.transport!r}")
        if config.transport in ("both", "ws") and not ws_url:
            raise ValueError("a WebSocket URL is required for the ws transport")
        self.post = post
        self.ws_url = ws_url
        self.inbox = inbox
        self.scope = scope
        self.config = config
        self.subscribe_fn = subscribe_fn
        self.now = now
        self.notices = notices
        self._stop = asyncio.Event()
        self.ws_connected = False
        self.ws_connects = 0
        self.ws_messages = 0
        self.last_ws_message_at: float | None = None
        self.last_reconcile_at: float | None = None
        self.last_reconcile: ReconcileReport | None = None
        self._stalled: set[int] = set()
        self.started_at: float | None = None
        self._tasks: list[asyncio.Task[None]] = []

    def stop(self) -> None:
        """Ask every loop to finish."""
        self._stop.set()

    async def _sleep(self, seconds: float) -> None:
        """Sleep until ``seconds`` pass or the worker is stopped."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, seconds))
        except TimeoutError:
            pass

    async def run(self) -> None:
        """Run the configured loops until stopped."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop)
            except NotImplementedError, RuntimeError:
                pass
        self.started_at = self.now()
        await asyncio.to_thread(self.inbox.log, "worker_started", self.started_at, transport=self.config.transport)
        if self.notices is not None:
            # Before any loop runs, so the first WebSocket snapshot is judged against it.
            await self.notices.start()
        loops = []
        if self.config.transport in ("both", "http"):
            loops.append(asyncio.create_task(self.reconcile_loop(), name="reconcile"))
        if self.config.transport in ("both", "ws"):
            loops.append(asyncio.create_task(self.ws_loop(), name="ws"))
        self._tasks = loops
        helpers = [asyncio.create_task(self.heartbeat_loop(), name="heartbeat")]
        if self.notices is not None:
            # Not in self._tasks: a dead notifier must not trip the watchdog.
            helpers.append(asyncio.create_task(self.notices.notifier.run(), name="notifier"))
            if self.config.transport in ("both", "ws"):
                self.notices.ws_disconnected(self.started_at, "not connected yet")
        notify("READY=1", f"STATUS=transport={self.config.transport}")
        try:
            await self._stop.wait()
        finally:
            notify("STOPPING=1")
            for task in [*loops, *helpers]:
                task.cancel()
            await asyncio.gather(*loops, *helpers, return_exceptions=True)
            await asyncio.to_thread(self.inbox.log, "worker_stopped", self.now())

    async def ws_loop(self) -> None:
        """Hold the live subscription, reconnecting with capped exponential backoff."""
        addresses = address_variants({a for addrs in self.scope.values() for a in addrs})
        query = live_subscription(self.config.snapshot_limit, sorted(self.scope))
        failures = 0
        while not self._stop.is_set():
            connected_at: float | None = None
            error = "server completed the subscription"
            try:
                async for payload in self.subscribe_fn(self.ws_url, query, {"addresses": addresses}):
                    received_at = self.now()
                    if connected_at is None:
                        connected_at = received_at
                        failures = 0
                        self.ws_connected = True
                        self.ws_connects += 1
                        await asyncio.to_thread(self.inbox.log, "ws_connected", received_at)
                        _logger.info("WebSocket subscription live")
                        if self.notices is not None:
                            self.notices.ws_connected(received_at)
                    if payload.get("errors"):
                        raise GraphQLWSError(f"GraphQL errors in snapshot: {payload['errors']!r}")
                    await self._ingest_snapshot(payload, received_at)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - every transport failure means reconnect
                error = f"{type(exc).__name__}: {exc}"
            self.ws_connected = False
            disconnected_at = self.now()
            await asyncio.to_thread(
                self.inbox.log,
                "ws_disconnected",
                disconnected_at,
                error=error,
                connected_for=(disconnected_at - connected_at) if connected_at is not None else None,
            )
            if self._stop.is_set():
                break
            if self.notices is not None:
                self.notices.ws_disconnected(disconnected_at, error)
            failures += 1
            delay = backoff_delay(failures, self.config.backoff_initial, self.config.backoff_max)
            delay *= random.uniform(0.8, 1.2)
            _logger.warning("WebSocket disconnected (%s); reconnecting in %.1fs", error, delay)
            await self._sleep(delay)

    async def _ingest_snapshot(self, payload: dict[str, Any], received_at: float) -> None:
        """Fold one live-query snapshot into the inbox and log first sightings."""
        rows = [r for r in (payload.get("data") or {}).get("TimelockEvent") or [] if in_scope(r, self.scope)]
        clock = time.perf_counter()
        result = await asyncio.to_thread(self.inbox.observe, rows, SOURCE_WS, received_at)
        ingest_ms = (time.perf_counter() - clock) * 1000
        self.ws_messages += 1
        self.last_ws_message_at = received_at
        if self.notices is not None:
            await self.notices.new_events(SOURCE_WS, result.new_ids)
            self.notices.snapshot_changes(result.changed_ids)
        if result.new or result.changed or result.restored:
            by_id = {str(r["id"]): r for r in rows}
            for event_id in result.new_ids:
                row = by_id[event_id]
                _logger.info(
                    "ws new event %s chain=%s timelock=%s %s latency=%.1fs",
                    event_id,
                    row.get("chainId"),
                    row.get("timelockAddress"),
                    row.get("eventName"),
                    received_at - int(row["blockTimestamp"]),
                )
            await asyncio.to_thread(
                self.inbox.log,
                "ws_snapshot",
                received_at,
                ingest_ms,
                rows=len(rows),
                new=result.new,
                changed=result.changed,
                restored=result.restored,
            )

    async def reconcile_loop(self) -> None:
        """Run a reconcile cycle every ``reconcile_interval`` seconds."""
        while not self._stop.is_set():
            started = self.now()
            ws_was_connected = self.ws_connected
            try:
                report = await asyncio.to_thread(
                    reconcile_all, self.post, self.inbox, self.scope, self.config.reconcile, self.now
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - an unexpected bug must not end reconciliation
                _logger.exception("reconcile cycle crashed")
                report = ReconcileReport(started_at=started, error=f"{type(exc).__name__}: {exc}")
            self.last_reconcile = report
            self.last_reconcile_at = self.now()
            ws_expected = ws_was_connected and self.config.transport == "both"
            self._log_reconcile(report, ws_expected)
            if self.notices is not None:
                await self.notices.reconcile(report, ws_expected)
            await self._sleep(self.config.reconcile_interval - (self.now() - started))

    def _log_reconcile(self, report: ReconcileReport, ws_expected: bool) -> None:
        if report.error:
            _logger.error("reconcile failed: %s", report.error)
        for chain in report.chains:
            if chain.error:
                _logger.error("reconcile chain %s failed: %s", chain.chain_id, chain.error)
            if chain.new and ws_expected:
                # HTTP found rows first although the subscription was up: a WS miss.
                _logger.warning(
                    "chain %s: HTTP found %s events before WS: %s", chain.chain_id, chain.new, chain.new_ids
                )
            elif chain.new:
                _logger.info("chain %s: %s new events via %s", chain.chain_id, chain.new, SOURCE_HTTP)
        _logger.info("reconcile done in %.0fms (ok=%s)", report.duration_ms, report.ok)

    def health(self) -> dict[str, Any]:
        """Return the worker's current health snapshot (also written as the heartbeat)."""
        now = self.now()
        chains = []
        for cp in self.inbox.checkpoints():
            if cp.chain_id not in self.scope:
                continue
            head_age = now - cp.head_changed_at
            chains.append(
                {
                    "chain_id": cp.chain_id,
                    "checkpoint": cp.block_number,
                    "indexed_head": cp.indexed_head,
                    "head_age_seconds": round(head_age, 1),
                    "stalled": head_age > self.config.head_stall_seconds,
                }
            )
        last = self.last_reconcile
        return {
            "at": now,
            "transport": self.config.transport,
            "ws_connected": self.ws_connected,
            "ws_connects": self.ws_connects,
            "ws_messages": self.ws_messages,
            "last_ws_message_at": self.last_ws_message_at,
            "last_reconcile_at": self.last_reconcile_at,
            "last_reconcile_ok": last.ok if last else None,
            "last_reconcile_ms": round(last.duration_ms, 1) if last else None,
            "reconcile_interval": self.config.reconcile_interval,
            "heartbeat_interval": self.config.heartbeat_interval,
            "chains": chains,
        }

    def is_alive(self) -> bool:
        """True while every loop task runs and reconcile cycles keep completing.

        A loop task that ended (it should only end on stop) makes the worker dead. The
        reconcile deadline counts from the worker's start until the first cycle
        completes, so a first cycle that hangs is caught too.
        """
        if any(task.done() for task in self._tasks):
            return False
        if self.config.transport == "ws":
            return True
        last = self.last_reconcile_at if self.last_reconcile_at is not None else self.started_at
        if last is None:
            return True  # run() has not started the loops yet
        return self.now() - last < 3 * self.config.reconcile_interval + 120

    async def heartbeat_loop(self) -> None:
        """Persist health, report stalled chains, and ping the systemd watchdog."""
        while not self._stop.is_set():
            health = await asyncio.to_thread(self.health)
            await asyncio.to_thread(self.inbox.set_status, "heartbeat", health, health["at"])
            for chain in health["chains"]:
                chain_id = chain["chain_id"]
                if chain["stalled"] and chain_id not in self._stalled:
                    _logger.warning(
                        "chain %s indexed head %s has not advanced for %.0fs",
                        chain_id,
                        chain["indexed_head"],
                        chain["head_age_seconds"],
                    )
                    self._stalled.add(chain_id)
                    if self.notices is not None:
                        self.notices.stall_changed(chain_id, True, chain["indexed_head"], chain["head_age_seconds"])
                elif not chain["stalled"] and chain_id in self._stalled:
                    _logger.info("chain %s indexed head advancing again", chain_id)
                    self._stalled.discard(chain_id)
                    if self.notices is not None:
                        self.notices.stall_changed(chain_id, False, chain["indexed_head"], 0.0)
            if self.notices is not None:
                self.notices.check_ws(health["at"])
                try:
                    await self.notices.maybe_daily_summary(health["at"])
                except Exception:  # noqa: BLE001 - a report bug must not stop the heartbeat
                    _logger.warning("daily summary failed", exc_info=True)
            if self.is_alive():
                notify(
                    "WATCHDOG=1", f"STATUS=ws={'up' if self.ws_connected else 'down'} stalled={sorted(self._stalled)}"
                )
            else:
                _logger.error(
                    "a worker loop stopped or reconcile has not completed recently; withholding watchdog ping"
                )
            await self._sleep(self.config.heartbeat_interval)

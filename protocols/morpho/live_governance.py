"""Live Morpho governance consumer: indexed events → daily-job decisions → recorded or shadow alerts.

Runs inside the ``morpho`` stream's worker (#448). Ingestion only writes the
inbox; this consumer reads it on its own schedule (woken after each ingest, with
a polling fallback), so a slow RPC read or Telegram call never delays intake.

For every inbox event without a decision for its current revision, in chain
order and grouped per vault, it asks ``live_rules`` what the daily jobs would
alert, renders that with the daily jobs' own grouping (``render_vault_alerts``),
and stores the decision and the alert requests durably (``live_store``):

- **Historical replay never alerts.** Events whose block predates the consumer's
  baseline (the first time it ran on this inbox) are recorded as ``historical``.
  Events from later blocks are evaluated even when first seen after a restart.
- **Failures retry.** An RPC node behind the event's block, or any read error,
  leaves the vault's events undecided and retries with capped backoff.
- **Delivery mode:** ``observe`` (default) records requests as ``observed`` and
  sends nothing. ``shadow`` posts each request to the internal errors chat,
  labelled ``[morpho-live-shadow]``, and only when that chat is configured, so
  shadow output never reaches the Morpho group. A failed send stays pending and
  is retried. Production delivery (the Morpho channel, shared dedup with the daily
  jobs) is part of the #450 cutover.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from protocols.morpho._alerts import render_vault_alerts
from protocols.morpho.config import VaultConfig
from protocols.morpho.live_rules import (
    REASON_ALERTED,
    GovernanceEvent,
    RpcBehindError,
    VaultEvaluation,
    evaluate_v1,
    evaluate_v2,
    vault_config,
)
from protocols.morpho.live_store import (
    OUTCOME_ALERTED,
    OUTCOME_HISTORICAL,
    OUTCOME_NO_ALERT,
    STATUS_OBSERVED,
    STATUS_PENDING,
    AlertRequest,
    Decision,
    LiveStore,
    PendingEvent,
)
from utils.chains import Chain
from utils.logger import get_logger
from utils.telegram import error_channel_configured, send_error_message
from utils.web3_wrapper import ChainManager

logger = get_logger("morpho.live_governance")

MODE_OBSERVE = "observe"
MODE_SHADOW = "shadow"
DELIVERY_MODES = (MODE_OBSERVE, MODE_SHADOW)

# Inbox status key: when the consumer first ran on this inbox. Older blocks are history.
BASELINE_STATUS_KEY = "live_baseline_at"
SHADOW_LABEL = "morpho-live-shadow"
# Alert-history key: internal-only, so shadow messages stay off the public Morpho page.
SHADOW_ALERT_PROTOCOL = "yearn-internal"

Evaluator = Callable[[Chain, VaultConfig, int, list[GovernanceEvent]], VaultEvaluation]
SendFn = Callable[[AlertRequest], None]


class ShadowChannelMissing(Exception):
    """Shadow delivery needs the internal errors chat; it never falls back to a protocol group."""


def evaluate_on_chain(chain: Chain, vault: VaultConfig, version: int, events: list[GovernanceEvent]) -> VaultEvaluation:
    """Evaluate one vault's events against its current on-chain state."""
    client = ChainManager.get_client(chain)
    return (evaluate_v1 if version == 1 else evaluate_v2)(client, chain, vault, events)


def send_shadow(request: AlertRequest) -> None:
    """Post one would-be Morpho alert to the internal errors chat.

    Raises:
        ShadowChannelMissing: No errors chat is configured.
    """
    if not error_channel_configured():
        raise ShadowChannelMissing("no errors chat configured for shadow delivery")
    send_error_message(
        f"shadow → morpho, {request.severity}, events {', '.join(request.event_ids)}\n\n{request.message}",
        SHADOW_LABEL,
        source="morpho_live_shadow",
        alert_protocol=SHADOW_ALERT_PROTOCOL,
    )


@dataclass(frozen=True)
class LiveConfig:
    """Mode and timing of the consumer loops."""

    mode: str = MODE_OBSERVE
    # Fallback poll; ingestion wakes the consumer as soon as rows land.
    poll_interval: float = 5.0
    retry_initial: float = 5.0
    retry_max: float = 300.0
    delivery_interval: float = 1.0
    batch_limit: int = 500


class LiveGovernanceConsumer:
    """Evaluates new Morpho governance events and records (or shadow-sends) the alerts they imply."""

    def __init__(
        self,
        store: LiveStore,
        config: LiveConfig = LiveConfig(),
        evaluate: Evaluator = evaluate_on_chain,
        send: SendFn = send_shadow,
        now: Callable[[], float] = time.time,
    ) -> None:
        if config.mode not in DELIVERY_MODES:
            raise ValueError(f"delivery mode must be one of {DELIVERY_MODES}, got {config.mode!r}")
        self.store = store
        self.config = config
        self.evaluate = evaluate
        self.send = send
        self.now = now
        self.baseline: float | None = None
        self._wake = asyncio.Event()
        # (chain id, vault) -> (consecutive failures, retry not before)
        self._retry: dict[tuple[int, str], tuple[int, float]] = {}
        self._delivery_failures = 0

    def wake(self) -> None:
        """Ask the consumer to look for new events now (called after each ingest)."""
        self._wake.set()

    def load_baseline(self) -> float:
        """Load the baseline, recording it the first time the consumer runs on this inbox."""
        if self.baseline is None:
            stored = self.store.inbox.status().get(BASELINE_STATUS_KEY, (None, 0.0))[0]
            if stored is None:
                stored = self.now()
                self.store.inbox.set_status(BASELINE_STATUS_KEY, stored, stored)
            self.baseline = float(stored)
        return self.baseline

    async def run(self) -> None:
        """Run the evaluation loop, and the delivery loop in shadow mode, until cancelled."""
        await asyncio.to_thread(self.load_baseline)
        loops = [self._evaluation_loop()]
        if self.config.mode == MODE_SHADOW:
            loops.append(self._delivery_loop())
        await asyncio.gather(*loops)

    async def _evaluation_loop(self) -> None:
        """Evaluate pending events whenever woken, or every ``poll_interval``."""
        while True:
            self._wake.clear()
            try:
                await asyncio.to_thread(self.process_once)
            except Exception:  # noqa: BLE001 - a store or rule bug must not end the consumer
                logger.exception("live governance evaluation pass failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.config.poll_interval)
            except TimeoutError:
                pass

    def process_once(self) -> int:
        """Evaluate every pending event once, per vault, in chain order.

        Returns:
            The number of events decided in this pass.
        """
        baseline = self.load_baseline()
        groups: dict[tuple[int, str], list[PendingEvent]] = {}
        for event in self.store.undecided(self.config.batch_limit):
            groups.setdefault((event.chain_id, event.vault_address), []).append(event)
        decided = 0
        for key, events in groups.items():
            failures, not_before = self._retry.get(key, (0, 0.0))
            if self.now() < not_before:
                continue
            # Block timestamps are whole seconds; a block in the baseline's own second counts.
            historical = [e for e in events if e.block_timestamp < int(baseline)]
            fresh = [e for e in events if e.block_timestamp >= int(baseline)]
            if historical:
                self._record(historical, OUTCOME_HISTORICAL, "block before the live baseline")
                decided += len(historical)
            if not fresh:
                continue
            try:
                decided += self._evaluate_vault(fresh)
            except RpcBehindError as exc:
                logger.info("%s on chain %s: %s; retrying", key[1], key[0], exc)
                self._retry[key] = (failures, self.now() + self.config.retry_initial)
            except Exception as exc:  # noqa: BLE001 - one vault's failure must not block the others
                delay = min(self.config.retry_max, self.config.retry_initial * 2**failures)
                logger.warning(
                    "live evaluation for %s on chain %s failed (%s); retry in %.0fs", key[1], key[0], exc, delay
                )
                self._retry[key] = (failures + 1, self.now() + delay)
            else:
                self._retry.pop(key, None)
        return decided

    def _record(self, events: list[PendingEvent], outcome: str, reason: str) -> None:
        """Record the same outcome for every event, with no alert requests."""
        decisions = [Decision(event, outcome, reason) for event in events]
        self.store.record(decisions, [], self.config.mode, STATUS_OBSERVED, self.now())

    def _evaluate_vault(self, events: list[PendingEvent]) -> int:
        """Evaluate one vault's fresh events and store the decisions with their alert requests."""
        first = GovernanceEvent.from_payload(events[0].payload)
        chain = Chain.from_chain_id(first.chain_id)
        vault = vault_config(chain, first.vault, first.version)
        if vault is None:
            self._record(events, OUTCOME_NO_ALERT, "vault not in config.py")
            return len(events)
        evaluation = self.evaluate(
            chain, vault, first.version, [GovernanceEvent.from_payload(e.payload) for e in events]
        )
        messages = [
            (severity.value, text) for severity, text in render_vault_alerts(evaluation.header, evaluation.alerts)
        ]
        decisions = []
        for event in events:
            reason = evaluation.reasons.get(event.event_id, "not evaluated")
            outcome = OUTCOME_ALERTED if reason == REASON_ALERTED and messages else OUTCOME_NO_ALERT
            decisions.append(Decision(event, outcome, reason))
        status = STATUS_PENDING if self.config.mode == MODE_SHADOW else STATUS_OBSERVED
        alert_ids = self.store.record(decisions, messages, self.config.mode, status, self.now())
        for severity, text in messages:
            logger.info("live %s %s alert for %s: %s", self.config.mode, severity, vault.name, text.split("\n", 1)[0])
        if alert_ids:
            logger.info("recorded %s live alert request(s) %s for %s", len(alert_ids), alert_ids, vault.name)
        return len(events)

    async def _delivery_loop(self) -> None:
        """Send pending shadow requests in order; on failure, retry the same request with backoff."""
        while True:
            delay = self.config.delivery_interval
            for request in await asyncio.to_thread(self.store.pending_alerts):
                try:
                    await asyncio.to_thread(self.send, request)
                except Exception as exc:  # noqa: BLE001 - delivery is retried, never dropped
                    self._delivery_failures += 1
                    await asyncio.to_thread(self.store.mark_failed, request.id, f"{type(exc).__name__}: {exc}")
                    delay = min(self.config.retry_max, self.config.retry_initial * 2 ** (self._delivery_failures - 1))
                    logger.warning("shadow delivery of request %s failed (%s); retry in %.0fs", request.id, exc, delay)
                    break  # keep order: nothing after a failed request goes out first
                self._delivery_failures = 0
                await asyncio.to_thread(self.store.mark_sent, request.id, self.now())
            await asyncio.sleep(delay)


def build_consumer(store: LiveStore, mode: str) -> LiveGovernanceConsumer:
    """Return a consumer for ``mode``, downgrading shadow to observe when no errors chat is configured."""
    if mode == MODE_SHADOW and not error_channel_configured():
        logger.warning("MORPHO_LIVE_DELIVERY=shadow but no errors chat is configured; observing only")
        mode = MODE_OBSERVE
    return LiveGovernanceConsumer(store, LiveConfig(mode=mode))


# Undecided events or undelivered shadow alerts older than this make `health` fail.
STALE_SECONDS = 600


def live_health(store: LiveStore, now: float) -> list[str]:
    """Return problems with the consumer's backlog: events not decided, or shadow alerts not delivered, in time."""
    problems = []
    stale_events = [e for e in store.undecided() if now - e.first_seen_at > STALE_SECONDS]
    if stale_events:
        problems.append(f"live: {len(stale_events)} event(s) undecided for over {STALE_SECONDS}s")
    pending = store.pending_alerts()
    if pending:
        oldest = min(row["created_at"] for row in store.alerts_since(0) if row["status"] == STATUS_PENDING)
        if now - oldest > STALE_SECONDS:
            problems.append(f"live: {len(pending)} shadow alert(s) undelivered, oldest {now - oldest:.0f}s")
    return problems


def live_summary(store: LiveStore, hours: float, now: float) -> str:
    """Summarize the consumer's decisions and alert requests over the last ``hours``."""
    since = now - hours * 3600
    decisions = store.decisions_since(since)
    alerts = store.alerts_since(since)
    by_outcome: dict[str, int] = {}
    for row in decisions:
        by_outcome[row["outcome"]] = by_outcome.get(row["outcome"], 0) + 1
    delays = sorted(
        row["decided_at"] - row["block_timestamp"] for row in decisions if row["outcome"] != OUTCOME_HISTORICAL
    )
    by_status: dict[str, int] = {}
    for row in alerts:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    p95 = delays[max(0, -(-len(delays) * 95 // 100) - 1)] if delays else None
    lines = [
        f"Live governance decisions, last {hours:g}h",
        f"  decisions: {len(decisions)} {dict(sorted(by_outcome.items()))}",
        "  block → decision: " + ("n/a" if p95 is None else f"p50 {delays[len(delays) // 2]:.1f}s, p95 {p95:.1f}s"),
        f"  alert requests: {len(alerts)} {dict(sorted(by_status.items()))}",
    ]
    for row in alerts[-10:]:
        lines.append(f"  - #{row['id']} {row['status']} {row['severity']}: {row['message'].split(chr(10), 1)[0][:120]}")
    return "\n".join(lines)

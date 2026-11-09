"""Audit of the live Morpho governance path against the daily jobs (#449).

The live path re-reads on-chain state instead of rebuilding it from events, so
there is no event-derived state to compare with the daily snapshots. What can be
compared is the outcome: every daily alert that implies a specific indexed event
should have a live decision for that event. In production the live path commits
the daily cache first, so a daily alert for such an event means the live path
missed it; in shadow both paths alert, and the audit still reports any gap.

The daily jobs record each such finding in ``morpho_live_audit``, next to the
stream's inbox. The consumer then settles each one:

- ``matched``: the live path alerted the event, or evaluated it after the daily
  job had already alerted it (it was catching up after an outage).
- ``divergent``: no indexed event once reconciliation has passed the block the
  daily job read, or the live path decided earlier that there was nothing to alert.
- ``waiting``: the indexer has not reached that block, or the event is undecided.
  Indexer lag is never counted as agreement or as divergence.
- ``out_of_window``: the event may predate the stream's first run, so the inbox
  cannot be expected to hold it.

Guardian, sentinel, and allocator changes, resolutions, and reminders are
daily-only by design and carry no expectation, so they are never audited.
"""

import json
import os
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from protocols.morpho._alerts import VaultAlert
from protocols.morpho.governance_stream import MORPHO_GOVERNANCE_STREAM
from protocols.morpho.live_store import (
    BASELINE_STATUS_KEY,
    OUTCOME_ALERTED,
    OUTCOME_HISTORICAL,
    LiveStore,
)
from utils.chains import Chain
from utils.logger import get_logger
from utils.paths import cache_path
from utils.subscriber.inbox import EventInbox
from utils.web3_wrapper import ChainManager

logger = get_logger("morpho.live_audit")

STATUS_WAITING = "waiting"
STATUS_MATCHED = "matched"
STATUS_DIVERGENT = "divergent"
STATUS_OUT_OF_WINDOW = "out_of_window"

AUDIT_LABEL = "morpho-live-audit"
# How far back of a daily finding its event may lie: the daily job runs once a day,
# so this tolerates two missed runs. Findings this close to the stream's first run
# are out of window, because the event may predate the inbox.
WINDOW_SECONDS = 3 * 24 * 60 * 60
# A finding still waiting after this long makes `health` fail.
WAITING_STALE_SECONDS = 24 * 60 * 60
# Divergent findings fail `health` for this long, so one is seen but does not fail it forever.
DIVERGENT_HEALTH_SECONDS = 7 * 24 * 60 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS morpho_live_audit (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id       INTEGER NOT NULL,
    vault_address  TEXT NOT NULL,
    event_name     TEXT NOT NULL,
    expected_args  TEXT NOT NULL,
    observed_block INTEGER NOT NULL,
    observed_at    REAL NOT NULL,
    status         TEXT NOT NULL,
    detail         TEXT,
    resolved_at    REAL,
    notified_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_morpho_live_audit_status ON morpho_live_audit(status, id);
"""


class AuditStore(LiveStore):
    """The live store plus the daily findings waiting to be compared with its decisions."""

    def __init__(self, inbox: EventInbox) -> None:
        super().__init__(inbox)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def add_findings(
        self, chain_id: int, vault: str, expected: list[tuple[str, dict[str, Any]]], block: int, now: float
    ) -> None:
        """Record one ``(event name, args)`` finding per expected event, as waiting."""
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO morpho_live_audit (
                    chain_id, vault_address, event_name, expected_args, observed_block, observed_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (chain_id, vault.lower(), name, json.dumps(args, default=str), block, now, STATUS_WAITING)
                    for name, args in expected
                ],
            )

    def open_findings(self) -> list[sqlite3.Row]:
        """Return findings not settled yet, oldest first."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM morpho_live_audit WHERE status = ? ORDER BY id", (STATUS_WAITING,)
            ).fetchall()

    def events_with_decisions(self, finding: sqlite3.Row) -> list[sqlite3.Row]:
        """Return the finding's candidate active events in its window, newest first, with any decision."""
        with self._connect() as conn:
            return conn.execute(
                f"""
                SELECT e.payload_json, d.outcome, d.reason, d.decided_at
                FROM {self._events} e
                LEFT JOIN morpho_live_decisions d ON d.event_id = e.event_id AND d.revision = e.revision
                WHERE e.chain_id = ? AND lower(e.contract_address) = ? AND e.event_name = ?
                  AND e.status = 'active' AND e.block_timestamp >= ?
                ORDER BY e.block_number DESC, e.log_index DESC
                """,
                (
                    finding["chain_id"],
                    finding["vault_address"],
                    finding["event_name"],
                    int(finding["observed_at"] - WINDOW_SECONDS),
                ),
            ).fetchall()

    def settle(self, finding_id: int, status: str, detail: str, now: float) -> None:
        """Record a finding's verdict."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE morpho_live_audit SET status = ?, detail = ?, resolved_at = ? WHERE id = ?",
                (status, detail, now, finding_id),
            )

    def unnotified_divergent(self) -> list[sqlite3.Row]:
        """Return divergent findings nobody has been told about yet."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM morpho_live_audit WHERE status = ? AND notified_at IS NULL ORDER BY id",
                (STATUS_DIVERGENT,),
            ).fetchall()

    def mark_notified(self, finding_id: int, now: float) -> None:
        """Record that a divergent finding's notice went out."""
        with self._connect() as conn:
            conn.execute("UPDATE morpho_live_audit SET notified_at = ? WHERE id = ?", (now, finding_id))

    def findings_since(self, since: float) -> list[sqlite3.Row]:
        """Return findings observed at or after ``since``, oldest first."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM morpho_live_audit WHERE observed_at >= ? ORDER BY id", (since,)
            ).fetchall()


def record_daily_findings(
    chain: Chain,
    vault_address: str,
    alerts: list[VaultAlert],
    observed_block: int | None = None,
    now: Callable[[], float] = time.time,
) -> None:
    """Record the indexed events a daily job's alerts imply, for the audit to settle.

    Best effort: the alerts are already sent, so a failure here is logged and never
    reaches the daily job. Nothing is recorded when the Morpho stream has no inbox.

    Args:
        chain: The vault's chain.
        vault_address: The vault.
        alerts: The sections the daily job just sent.
        observed_block: Block the job read at; the chain head is read when omitted.
        now: Clock.
    """
    expected = [(e.event, e.args) for alert in alerts for e in alert.expected]
    if not expected:
        return
    try:
        path = cache_path(MORPHO_GOVERNANCE_STREAM.db_filename)
        if not os.path.exists(path):
            return
        block = int(ChainManager.get_client(chain).eth.block_number) if observed_block is None else observed_block
        AuditStore(EventInbox(MORPHO_GOVERNANCE_STREAM, path)).add_findings(
            chain.chain_id, vault_address, expected, block, now()
        )
    except Exception:  # noqa: BLE001 - the audit must never fail a daily job that already alerted
        logger.warning("could not record daily findings for %s on %s", vault_address, chain.name, exc_info=True)


def _normalized(value: Any) -> str:
    """Return an argument in a form that compares across checksum case and integer encodings."""
    return str(value).lower().removeprefix("0x")


def _args_match(payload_json: str, expected: dict[str, Any]) -> bool:
    """True when the stored event carries every expected argument."""
    try:
        args = json.loads(json.loads(payload_json).get("argsJson") or "{}")
    except TypeError, ValueError:
        return False
    return isinstance(args, dict) and all(
        key in args and _normalized(args[key]) == _normalized(value) for key, value in expected.items()
    )


def _verdict(
    store: AuditStore, finding: sqlite3.Row, baseline: float | None, reconciled_to: int | None
) -> tuple[str, str] | None:
    """Return ``(status, detail)`` for a finding, or None while it must keep waiting."""
    if baseline is None:
        return None
    if finding["observed_at"] < baseline + WINDOW_SECONDS:
        return STATUS_OUT_OF_WINDOW, "the event may predate the stream's first run"
    expected = json.loads(finding["expected_args"])
    events = [row for row in store.events_with_decisions(finding) if _args_match(row["payload_json"], expected)]
    if not events:
        if reconciled_to is None or reconciled_to < finding["observed_block"]:
            return None
        return STATUS_DIVERGENT, f"no indexed event through block {reconciled_to}"
    if any(row["outcome"] == OUTCOME_ALERTED for row in events):
        return STATUS_MATCHED, "the live path alerted this event"
    newest = events[0]
    if newest["outcome"] is None:
        return None
    if newest["outcome"] == OUTCOME_HISTORICAL:
        return STATUS_OUT_OF_WINDOW, "the event predates the live baseline"
    if newest["decided_at"] >= finding["observed_at"]:
        return STATUS_MATCHED, "the live path evaluated it after the daily job alerted"
    return STATUS_DIVERGENT, f"the live path decided not to alert: {newest['reason']}"


def resolve_findings(store: AuditStore, now: float) -> int:
    """Settle every open finding that can be settled; return how many were."""
    stored = store.inbox.status().get(BASELINE_STATUS_KEY, (None, 0.0))[0]
    baseline = None if stored is None else float(stored)
    reconciled = {checkpoint.chain_id: checkpoint.block_number for checkpoint in store.inbox.checkpoints()}
    settled = 0
    for finding in store.open_findings():
        verdict = _verdict(store, finding, baseline, reconciled.get(finding["chain_id"]))
        if verdict is not None:
            store.settle(finding["id"], verdict[0], verdict[1], now)
            settled += 1
    return settled


def divergence_text(finding: sqlite3.Row) -> str:
    """Render one divergent finding for logs and the internal errors chat."""
    chain = Chain.from_chain_id(finding["chain_id"])
    vault = finding["vault_address"]
    link = f"{chain.explorer_url}/address/{vault}" if chain.explorer_url else vault
    return (
        f"[{AUDIT_LABEL}] a daily Morpho alert has no matching live alert\n"
        f"Chain: {chain.name}\n"
        f"Vault: {link}\n"
        f"Event: {finding['event_name']} {finding['expected_args']}\n"
        f"Daily job read block {finding['observed_block']}\n"
        f"Detail: {finding['detail']}"
    )


def notify_divergences(store: AuditStore, notify: Callable[[str], None], now: float) -> int:
    """Report each new divergence once.

    A failing ``notify`` raises and leaves that finding, and the later ones, for the
    next pass.
    """
    sent = 0
    for finding in store.unnotified_divergent():
        text = divergence_text(finding)
        logger.warning("%s", text)
        notify(text)
        store.mark_notified(finding["id"], now)
        sent += 1
    return sent


def audit_health(store: AuditStore, now: float) -> list[str]:
    """Return audit problems: recent divergences, or findings stuck waiting."""
    findings = store.findings_since(now - DIVERGENT_HEALTH_SECONDS)
    problems = []
    divergent = sum(1 for row in findings if row["status"] == STATUS_DIVERGENT)
    if divergent:
        problems.append(f"audit: {divergent} divergent finding(s) in the last {DIVERGENT_HEALTH_SECONDS // 86400} days")
    stuck = sum(1 for row in store.open_findings() if now - row["observed_at"] > WAITING_STALE_SECONDS)
    if stuck:
        problems.append(f"audit: {stuck} finding(s) waiting for over {WAITING_STALE_SECONDS // 3600}h")
    return problems


def audit_summary(store: AuditStore, hours: float, now: float) -> str:
    """Summarize daily-versus-live findings observed in the last ``hours``."""
    findings = store.findings_since(now - hours * 3600)
    by_status: dict[str, int] = {}
    for row in findings:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    lines = [f"Daily vs live audit, last {hours:g}h", f"  findings: {len(findings)} {dict(sorted(by_status.items()))}"]
    for row in [r for r in findings if r["status"] == STATUS_DIVERGENT][-10:]:
        lines.append(f"  - chain {row['chain_id']} {row['vault_address']} {row['event_name']}: {row['detail']}")
    return "\n".join(lines)

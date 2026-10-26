"""Durable decisions and alert requests for the live Morpho governance consumer.

Two tables live in the Morpho stream's inbox file (``morpho_subscriber.db``),
next to the events they are about but written by the consumer, never by
ingestion:

- ``morpho_live_decisions``: one row per evaluated event revision with its
  outcome (``alerted``, ``no_alert``, ``historical``) and the reason. An event is
  pending evaluation until it has a decision for its current revision, so a reorg
  that rewrites an event re-evaluates it, and a restart resumes where it stopped.
- ``morpho_live_alerts``: one row per message the daily jobs would have sent,
  with its delivery mode and status. Observation mode records ``observed``;
  shadow mode records ``pending`` and the delivery loop moves it to ``sent`` (or
  keeps retrying, recording ``last_error``); production records ``sent``. Each
  request keeps the revisions of the events it came from: a pending request whose
  events were removed or rewritten by a reorg is ``invalidated`` instead of sent
  (the rewritten event is evaluated again and makes its own request).
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Any

from utils.subscriber.inbox import EventInbox

OUTCOME_ALERTED = "alerted"
OUTCOME_NO_ALERT = "no_alert"
OUTCOME_HISTORICAL = "historical"

STATUS_OBSERVED = "observed"
STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_INVALIDATED = "invalidated"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS morpho_live_decisions (
    event_id        TEXT PRIMARY KEY,
    revision        INTEGER NOT NULL,
    chain_id        INTEGER NOT NULL,
    vault_address   TEXT NOT NULL,
    event_name      TEXT,
    block_timestamp INTEGER NOT NULL,
    first_seen_at   REAL NOT NULL,
    decided_at      REAL NOT NULL,
    outcome         TEXT NOT NULL,
    reason          TEXT,
    alert_ids       TEXT
);
CREATE INDEX IF NOT EXISTS idx_morpho_live_decisions_decided ON morpho_live_decisions(decided_at);

CREATE TABLE IF NOT EXISTS morpho_live_alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id      INTEGER NOT NULL,
    vault_address TEXT NOT NULL,
    mode          TEXT NOT NULL,
    severity      TEXT NOT NULL,
    message       TEXT NOT NULL,
    event_ids     TEXT NOT NULL,
    -- {event_id: revision} of the events the request came from
    event_revisions TEXT NOT NULL,
    created_at    REAL NOT NULL,
    status        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    sent_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_morpho_live_alerts_status ON morpho_live_alerts(status, id);
"""


@dataclass(frozen=True)
class PendingEvent:
    """An inbox event whose current revision has no decision yet."""

    event_id: str
    revision: int
    chain_id: int
    vault_address: str
    event_name: str | None
    block_timestamp: int
    first_seen_at: float
    payload: dict[str, Any]


@dataclass(frozen=True)
class Decision:
    """The outcome for one event revision."""

    event: PendingEvent
    outcome: str
    reason: str


@dataclass(frozen=True)
class AlertRequest:
    """One rendered message the daily jobs would send for a vault."""

    id: int
    chain_id: int
    vault_address: str
    mode: str
    severity: str
    message: str
    event_ids: list[str]
    attempts: int


class LiveStore:
    """SQLite store for the consumer's decisions and alert requests, in the stream inbox's file."""

    def __init__(self, inbox: EventInbox) -> None:
        self.inbox = inbox
        self.path = inbox.path
        self._events = inbox.stream.table("events")
        with self._connect():
            pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection; one transaction spans the ``with`` block."""
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.executescript(_SCHEMA)
            with conn:
                yield conn

    def undecided(self, limit: int = 500, exclude: set[tuple[int, str]] | None = None) -> list[PendingEvent]:
        """Return active events without a decision for their current revision, in chain order.

        Args:
            limit: Most rows to return.
            exclude: ``(chain id, lowercase vault)`` pairs to skip (vaults in backoff),
                applied before ``limit`` so they cannot crowd out other vaults.
        """
        skip = sorted(exclude or ())
        not_excluded = "".join(" AND NOT (e.chain_id = ? AND e.contract_address = ?)" for _ in skip)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT e.* FROM {self._events} e
                LEFT JOIN morpho_live_decisions d ON d.event_id = e.event_id AND d.revision = e.revision
                WHERE e.status = 'active' AND d.event_id IS NULL{not_excluded}
                ORDER BY e.chain_id, e.block_number, e.log_index
                LIMIT ?
                """,
                (*[value for pair in skip for value in pair], limit),
            ).fetchall()
        return [
            PendingEvent(
                event_id=row["event_id"],
                revision=row["revision"],
                chain_id=row["chain_id"],
                vault_address=row["contract_address"],
                event_name=row["event_name"],
                block_timestamp=row["block_timestamp"],
                first_seen_at=row["first_seen_at"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def record(
        self,
        decisions: list[Decision],
        messages: list[tuple[str, str]],
        mode: str,
        status: str,
        now: float,
    ) -> list[int]:
        """Store one vault batch's decisions and its alert requests in one transaction.

        Args:
            decisions: Outcome per event of the batch.
            messages: ``(severity, message)`` pairs to request, in send order.
            mode: Delivery mode the requests were made under.
            status: ``STATUS_OBSERVED`` or ``STATUS_PENDING``.
            now: Decision time (unix seconds).

        Returns:
            The new alert request ids.
        """
        alerted = [d.event for d in decisions if d.outcome == OUTCOME_ALERTED]
        alerted_ids = json.dumps([e.event_id for e in alerted])
        revisions = json.dumps({e.event_id: e.revision for e in alerted})
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            alert_ids = []
            if messages:
                chain_id, vault = decisions[0].event.chain_id, decisions[0].event.vault_address
                for severity, message in messages:
                    cursor = conn.execute(
                        """
                        INSERT INTO morpho_live_alerts (
                            chain_id, vault_address, mode, severity, message, event_ids, event_revisions,
                            created_at, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (chain_id, vault, mode, severity, message, alerted_ids, revisions, now, status),
                    )
                    alert_ids.append(int(cursor.lastrowid or 0))
            for decision in decisions:
                event = decision.event
                conn.execute(
                    """
                    INSERT INTO morpho_live_decisions (
                        event_id, revision, chain_id, vault_address, event_name, block_timestamp, first_seen_at,
                        decided_at, outcome, reason, alert_ids
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        revision = excluded.revision, decided_at = excluded.decided_at, outcome = excluded.outcome,
                        reason = excluded.reason, alert_ids = excluded.alert_ids
                    """,
                    (
                        event.event_id,
                        event.revision,
                        event.chain_id,
                        event.vault_address,
                        event.event_name,
                        event.block_timestamp,
                        event.first_seen_at,
                        now,
                        decision.outcome,
                        decision.reason,
                        json.dumps(alert_ids if decision.outcome == OUTCOME_ALERTED else []),
                    ),
                )
        return alert_ids

    def pending_alerts(self, limit: int = 50) -> list[AlertRequest]:
        """Return alert requests awaiting delivery, oldest first, invalidating reorged ones.

        A request whose source events are no longer active at the revisions it was
        made from (removed, or rewritten and so evaluated again) is marked
        ``invalidated`` and not returned.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM morpho_live_alerts WHERE status = ? ORDER BY id LIMIT ?", (STATUS_PENDING, limit)
            ).fetchall()
            current = []
            for row in rows:
                if self._sources_current(conn, json.loads(row["event_revisions"])):
                    current.append(row)
                else:
                    conn.execute(
                        "UPDATE morpho_live_alerts SET status = ?, last_error = ? WHERE id = ?",
                        (STATUS_INVALIDATED, "source event removed or rewritten by a reorg", row["id"]),
                    )
            rows = current
        return [
            AlertRequest(
                id=row["id"],
                chain_id=row["chain_id"],
                vault_address=row["vault_address"],
                mode=row["mode"],
                severity=row["severity"],
                message=row["message"],
                event_ids=json.loads(row["event_ids"]),
                attempts=row["attempts"],
            )
            for row in rows
        ]

    def _sources_current(self, conn: sqlite3.Connection, revisions: dict[str, int]) -> bool:
        """True when every source event is still active at the revision the request was made from."""
        for event_id, revision in revisions.items():
            row = conn.execute(
                f"SELECT status, revision FROM {self._events} WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None or row["status"] != "active" or row["revision"] != revision:
                return False
        return True

    def mark_sent(self, alert_id: int, now: float) -> None:
        """Record a delivered request."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE morpho_live_alerts SET status = ?, attempts = attempts + 1, sent_at = ?, last_error = NULL "
                "WHERE id = ?",
                (STATUS_SENT, now, alert_id),
            )

    def mark_failed(self, alert_id: int, error: str) -> None:
        """Record a failed delivery attempt; the request stays pending and is retried."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE morpho_live_alerts SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (error[:500], alert_id),
            )

    def decisions_since(self, since: float) -> list[sqlite3.Row]:
        """Return decisions made at or after ``since``, oldest first."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM morpho_live_decisions WHERE decided_at >= ? ORDER BY decided_at, event_id", (since,)
            ).fetchall()

    def alerts_since(self, since: float) -> list[sqlite3.Row]:
        """Return alert requests created at or after ``since``, oldest first."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM morpho_live_alerts WHERE created_at >= ? ORDER BY id", (since,)
            ).fetchall()

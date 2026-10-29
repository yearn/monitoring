"""Durable emergency fanout with independent destination success and retry state.

SQLite FULL commits precede every HTTP attempt. Leased attempts can be retried
following process failure, so delivery is at least once; receivers must dedupe.
Secrets are resolved at send time and never written to the queue.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils.paths import cache_path

SCHEMA_VERSION = 1
COOLDOWN_SECONDS = 3600
LEASE_SECONDS = 60


@dataclass(frozen=True)
class Destination:
    """A configured endpoint; credentials stay in memory."""

    name: str
    url: str
    secret: str = field(repr=False)
    signed_event_id: bool = False


@dataclass(frozen=True)
class Delivery:
    """One independently tracked destination of a logical alert."""

    delivery_id: int
    event_id: str
    destination: str
    url: str
    body: bytes
    protocol: str
    severity: str
    attempts: int
    lease_token: str


class DispatchQueue:
    """Cross-process transactional queue; no network I/O occurs inside a DB lock."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path or os.getenv("EMERGENCY_DISPATCH_DB") or cache_path("emergency_dispatch.sqlite3"))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, SCHEMA_VERSION):
                    raise ValueError(f"incompatible emergency dispatch DB version: {version}")
                if version == 0:
                    for statement in _SCHEMA:
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.commit()
                yield conn
            except BaseException:
                conn.rollback()
                raise

    def enqueue(
        self,
        protocol: str,
        severity: str,
        message: str,
        destinations: Sequence[Destination],
        *,
        now: float | None = None,
    ) -> str | None:
        """Persist eligible destinations atomically, preserving a distinct escalation ID.

        HIGH is suppressed by recent HIGH/CRITICAL success or pending work.
        CRITICAL is suppressed only by CRITICAL success/pending work, never HIGH.
        Failures at one destination cannot suppress another destination's event.
        """
        if severity not in {"HIGH", "CRITICAL"}:
            raise ValueError("only HIGH/CRITICAL can be dispatched")
        timestamp = time.time() if now is None else now
        event_id = uuid.uuid4().hex
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            selected = []
            for destination in destinations:
                relevant = ("HIGH", "CRITICAL") if severity == "HIGH" else ("CRITICAL",)
                placeholders = ",".join("?" for _ in relevant)
                success = conn.execute(
                    f"SELECT 1 FROM successes WHERE destination=? AND protocol=? AND severity IN ({placeholders}) AND succeeded_at>? LIMIT 1",
                    (destination.name, protocol, *relevant, timestamp - COOLDOWN_SECONDS),
                ).fetchone()
                pending = conn.execute(
                    f"SELECT 1 FROM deliveries WHERE destination=? AND protocol=? AND severity IN ({placeholders}) AND status IN ('pending','retry','terminal') LIMIT 1",
                    (destination.name, protocol, *relevant),
                ).fetchone()
                if not success and not pending:
                    selected.append(destination)
            if not selected:
                return None
            for destination in selected:
                payload: dict[str, Any] = {
                    "event_type": "emergency_withdrawal",
                    "client_payload": {"protocol": protocol, "severity": severity, "message": message},
                }
                if destination.signed_event_id:
                    payload["event_id"] = event_id
                body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                conn.execute(
                    "INSERT INTO deliveries(event_id,destination,url,body,protocol,severity,created_at,next_attempt_at) VALUES (?,?,?,?,?,?,?,?)",
                    (event_id, destination.name, destination.url, body, protocol, severity, timestamp, timestamp),
                )
            return event_id

    def claim(self, destination: str, *, exclude: set[int], now: float | None = None) -> Delivery | None:
        """Commit an expiring lease before releasing the DB lock for HTTP."""
        timestamp = time.time() if now is None else now
        token = uuid.uuid4().hex
        exclusions = " AND id NOT IN (" + ",".join("?" for _ in exclude) + ")" if exclude else ""
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM deliveries WHERE destination=? AND status IN ('pending','retry') AND next_attempt_at<=? AND lease_until<=?"
                + exclusions
                + " ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 ELSE 1 END, created_at, id LIMIT 1",
                (destination, timestamp, timestamp, *sorted(exclude)),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE deliveries SET lease_until=?,lease_token=?,attempts=attempts+1 WHERE id=?",
                (timestamp + LEASE_SECONDS, token, row["id"]),
            )
            return Delivery(
                row["id"],
                row["event_id"],
                row["destination"],
                row["url"],
                bytes(row["body"]),
                row["protocol"],
                row["severity"],
                row["attempts"] + 1,
                token,
            )

    def finish(
        self,
        delivery: Delivery,
        status: str,
        *,
        error: str | None = None,
        retry_after: float | None = None,
        now: float | None = None,
    ) -> None:
        """Commit acknowledgement or failure; a stale lease cannot overwrite a new one."""
        if status not in {"delivered", "retry", "terminal"}:
            raise ValueError("invalid delivery outcome")
        timestamp = time.time() if now is None else now
        delay = min(1800, 30 * 2 ** min(delivery.attempts - 1, 6)) if retry_after is None else retry_after
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE deliveries SET status=?,last_error=?,next_attempt_at=?,lease_until=0,lease_token=NULL WHERE id=? AND lease_token=?",
                (status, error, timestamp + delay, delivery.delivery_id, delivery.lease_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("emergency delivery lease changed; acknowledgement remains uncertain")
            if status == "delivered":
                conn.execute(
                    "INSERT INTO successes(destination,protocol,severity,succeeded_at) VALUES(?,?,?,?) ON CONFLICT(destination,protocol,severity) DO UPDATE SET succeeded_at=MAX(succeeded_at,excluded.succeeded_at)",
                    (delivery.destination, delivery.protocol, delivery.severity, timestamp),
                )

    def records(self) -> list[dict[str, Any]]:
        """Inspect delivery state, never credentials or arbitrary response bodies."""
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT id,event_id,destination,protocol,severity,status,attempts,next_attempt_at,last_error FROM deliveries ORDER BY id"
                )
            ]

    def retry_terminal(self, delivery_id: int, *, url: str | None = None) -> None:
        """Explicit operator retry after correcting a terminal error; preserve event ID/body."""
        with self._connect() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE deliveries SET status='retry',url=COALESCE(?,url),next_attempt_at=0,last_error=NULL WHERE id=? AND status='terminal'",
                (url, delivery_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("delivery is not a terminal failure")


_SCHEMA = (
    """CREATE TABLE deliveries (
        id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, destination TEXT NOT NULL,
        url TEXT NOT NULL, body BLOB NOT NULL, protocol TEXT NOT NULL,
        severity TEXT NOT NULL CHECK(severity IN ('HIGH','CRITICAL')),
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','retry','delivered','terminal')),
        created_at REAL NOT NULL, next_attempt_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
        lease_token TEXT, last_error TEXT, UNIQUE(event_id,destination)
    )""",
    "CREATE INDEX deliveries_due ON deliveries(destination,status,next_attempt_at)",
    """CREATE TABLE successes (
        destination TEXT NOT NULL, protocol TEXT NOT NULL, severity TEXT NOT NULL,
        succeeded_at REAL NOT NULL, PRIMARY KEY(destination,protocol,severity)
    )""",
)

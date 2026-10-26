"""Durable local inbox for one event stream seen by the continuous subscriber.

Every row of the stream's Envio entity the worker sees, from the WebSocket
subscription or from HTTP reconciliation, is folded into one row per Envio event
id (``<chainId>_<blockNumber>_<logIndex>``) with its provenance: which transport
saw it first, when each transport first saw it, how often it was re-observed,
and a fingerprint of its content, including the block hash. A row that comes
back with different content (a reorg replaced the block or the log at that
position) or disappears from a reconciled range (a reorg dropped it) is recorded
in ``<prefix>_changes`` instead of silently overwritten.

Each stream has its own SQLite file and table prefix (see ``EventStream``), so
streams never contend for one write lock. Per-chain checkpoints live here too,
independent of the hourly monitors' cursors. The inbox writes nothing else: no
Telegram, no alert store.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Any

from utils.paths import cache_path
from utils.subscriber.stream import EventStream

SOURCE_WS = "ws"
SOURCE_HTTP = "http"

_SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS {p}_events (
    event_id                TEXT PRIMARY KEY,
    chain_id                INTEGER NOT NULL,
    contract_address        TEXT NOT NULL,
    block_number            INTEGER NOT NULL,
    block_hash              TEXT,
    log_index               INTEGER NOT NULL,
    block_timestamp         INTEGER NOT NULL,
    transaction_hash        TEXT NOT NULL,
    event_name              TEXT,
    operation_id            TEXT,
    fingerprint             TEXT NOT NULL,
    payload_json            TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'active',
    revision                INTEGER NOT NULL DEFAULT 0,
    first_seen_at           REAL NOT NULL,
    first_source            TEXT NOT NULL,
    ws_first_seen_at        REAL,
    http_first_seen_at      REAL,
    ws_observations         INTEGER NOT NULL DEFAULT 0,
    http_observations       INTEGER NOT NULL DEFAULT 0,
    updated_at              REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_{p}_events_chain_block
    ON {p}_events(chain_id, block_number);
CREATE INDEX IF NOT EXISTS idx_{p}_events_first_seen
    ON {p}_events(first_seen_at);

CREATE TABLE IF NOT EXISTS {p}_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL,
    chain_id        INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    old_fingerprint TEXT,
    new_fingerprint TEXT,
    source          TEXT NOT NULL,
    observed_at     REAL NOT NULL,
    old_payload_json TEXT
);

CREATE TABLE IF NOT EXISTS {p}_checkpoints (
    chain_id        INTEGER PRIMARY KEY,
    block_number    INTEGER NOT NULL,
    indexed_head    INTEGER NOT NULL,
    head_changed_at REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS {p}_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    at           REAL NOT NULL,
    duration_ms  REAL,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_{p}_log_kind_at ON {p}_log(kind, at);

CREATE TABLE IF NOT EXISTS {p}_status (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


# Current schema version (PRAGMA user_version). Databases from older revisions are
# migrated in place by _migrate; observations, changes, and checkpoints are kept.
SCHEMA_VERSION = 2


def _schema(stream: EventStream) -> str:
    """Return the CREATE statements for ``stream``'s tables."""
    return _SCHEMA_TEMPLATE.replace("{p}", stream.table_prefix)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the column names of ``table``."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection, events_table: str) -> None:
    """Bring a database created by an earlier revision up to ``SCHEMA_VERSION``.

    v0 → v1: ``<prefix>_events.block_hash`` (rows observed before the event
    selection included ``blockHash`` keep NULL until they are next observed).
    v1 → v2: ``timelock_address`` renamed to ``contract_address`` once the inbox
    served more than timelocks.
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    with conn:
        # Lock, then re-check: another process may have migrated in between.
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        columns = _columns(conn, events_table)
        if "block_hash" not in columns:
            conn.execute(f"ALTER TABLE {events_table} ADD COLUMN block_hash TEXT")
        if "timelock_address" in columns:
            conn.execute(f"ALTER TABLE {events_table} RENAME COLUMN timelock_address TO contract_address")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _widened_only(stored_payload: str, event: dict[str, Any], stored_fingerprint: str) -> bool:
    """True when ``event`` differs from the stored row only by fields it newly selects.

    A row stored before the selection grew (e.g. before ``blockHash`` was added)
    fingerprints differently once re-observed. If dropping the fields the stored
    payload lacks reproduces the stored fingerprint, nothing on chain changed.
    """
    old = json.loads(stored_payload)
    added = set(event) - set(old)
    if not added:
        return False
    return event_fingerprint({k: v for k, v in event.items() if k not in added}) == stored_fingerprint


@dataclass
class ObserveResult:
    """What one batch of observed rows did to the inbox."""

    new: int = 0
    duplicate: int = 0
    changed: int = 0
    restored: int = 0
    # event_ids first seen in this batch, in input order.
    new_ids: tuple[str, ...] = ()
    # event_ids whose content changed or that came back after removal.
    changed_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Checkpoint:
    """Per-chain reconciliation progress.

    ``block_number`` is the highest block whose events were fully fetched over HTTP
    while the indexer reported it as processed. ``indexed_head`` is the indexer's
    ``latest_processed_block`` at the last reconcile and ``head_changed_at`` when it
    last advanced, which is how a stalled chain is detected without an RPC call.
    """

    chain_id: int
    block_number: int
    indexed_head: int
    head_changed_at: float
    updated_at: float


def event_fingerprint(event: dict[str, Any]) -> str:
    """Hash every field of an event row so any content change is detected.

    Addresses and hashes are lowercased first: Envio stores some addresses
    checksummed, and the casing must not look like a reorg.
    """
    normalized = {k: (v.lower() if isinstance(v, str) and v.startswith("0x") else v) for k, v in event.items()}
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, default=str).encode()).hexdigest()


def _lower(value: Any) -> str | None:
    """Lowercase ``value`` as a string, keeping None."""
    return str(value).lower() if value is not None else None


def parse_log_index(event: dict[str, Any]) -> int:
    """Return the log index, from the row or from its ``<chain>_<block>_<logIndex>`` id."""
    if event.get("logIndex") is not None:
        return int(event["logIndex"])
    return int(str(event["id"]).rsplit("_", 1)[-1])


class EventInbox:
    """SQLite-backed store for one stream's observed events, changes, checkpoints, and worker log.

    Each call opens its own short-lived connection so the WebSocket task and the
    reconcile thread can share the database safely (WAL mode, busy timeout).
    """

    def __init__(self, stream: EventStream, path: str | None = None) -> None:
        self.stream = stream
        self.path = path or cache_path(stream.db_filename)
        self._events = stream.table("events")
        self._changes = stream.table("changes")
        self._checkpoints = stream.table("checkpoints")
        self._log = stream.table("log")
        self._status = stream.table("status")
        self._schema = _schema(stream)
        with self._connect():
            pass

    def _operation_id(self, event: dict[str, Any]) -> Any:
        """Return the stream's operation id field of ``event``, if it has one."""
        field = self.stream.operation_field
        return event.get(field) if field else None

    @contextmanager
    def _connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Open a connection; one transaction spans the ``with`` block.

        Args:
            write: Take the database write lock before the first statement
                (``BEGIN IMMEDIATE``). Required wherever a read decides a write:
                otherwise the WebSocket and reconcile threads can both read an id
                as unseen and the second insert fails on the primary key.
        """
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.executescript(self._schema)
            _migrate(conn, self._events)
            with conn:
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn

    def observe(self, events: Iterable[dict[str, Any]], source: str, now: float) -> ObserveResult:
        """Fold observed rows into the inbox in one transaction.

        A known id with the same fingerprint only bumps the per-source counters (a
        duplicate snapshot or an overlapping reconcile). A known id with a different
        fingerprint is a changed occurrence; a previously removed id coming back is
        restored. Both are logged in ``<prefix>_changes``.

        Args:
            events: Rows of the stream's entity as returned by Envio.
            source: ``SOURCE_WS`` or ``SOURCE_HTTP``.
            now: Observation time (unix seconds).

        Returns:
            Counts of new, duplicate, changed, and restored rows.
        """
        result = ObserveResult()
        new_ids: list[str] = []
        changed_ids: list[str] = []
        seen_col = "ws_first_seen_at" if source == SOURCE_WS else "http_first_seen_at"
        count_col = "ws_observations" if source == SOURCE_WS else "http_observations"
        with self._connect(write=True) as conn:
            for event in events:
                event_id = str(event["id"])
                fingerprint = event_fingerprint(event)
                row = conn.execute(
                    f"SELECT fingerprint, status, payload_json FROM {self._events} WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        f"""
                        INSERT INTO {self._events} (
                            event_id, chain_id, contract_address, block_number, block_hash, log_index,
                            block_timestamp, transaction_hash, event_name, operation_id, fingerprint, payload_json,
                            first_seen_at, first_source, {seen_col}, {count_col}, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            event_id,
                            int(event["chainId"]),
                            str(event[self.stream.address_field]).lower(),
                            int(event["blockNumber"]),
                            _lower(event.get("blockHash")),
                            parse_log_index(event),
                            int(event["blockTimestamp"]),
                            str(event["transactionHash"]).lower(),
                            event.get("eventName"),
                            self._operation_id(event),
                            fingerprint,
                            json.dumps(event, sort_keys=True, default=str),
                            now,
                            source,
                            now,
                            now,
                        ),
                    )
                    result.new += 1
                    new_ids.append(event_id)
                    continue

                kind = None
                if row["status"] == "removed":
                    kind = "restored"
                    result.restored += 1
                elif row["fingerprint"] != fingerprint and _widened_only(
                    row["payload_json"], event, row["fingerprint"]
                ):
                    # Same event, stored before the selection grew: upgrade it in place.
                    conn.execute(
                        f"UPDATE {self._events} SET fingerprint = ?, payload_json = ?, block_hash = ? "
                        "WHERE event_id = ?",
                        (
                            fingerprint,
                            json.dumps(event, sort_keys=True, default=str),
                            _lower(event.get("blockHash")),
                            event_id,
                        ),
                    )
                    result.duplicate += 1
                elif row["fingerprint"] != fingerprint:
                    kind = "changed"
                    result.changed += 1
                else:
                    result.duplicate += 1

                if kind:
                    changed_ids.append(event_id)
                    conn.execute(
                        f"""
                        INSERT INTO {self._changes} (
                            event_id, chain_id, kind, old_fingerprint, new_fingerprint, source, observed_at,
                            old_payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            int(event["chainId"]),
                            kind,
                            row["fingerprint"],
                            fingerprint,
                            source,
                            now,
                            row["payload_json"],
                        ),
                    )
                    conn.execute(
                        f"""
                        UPDATE {self._events} SET
                            status = 'active', revision = revision + 1, fingerprint = ?, payload_json = ?,
                            block_hash = ?, block_timestamp = ?, transaction_hash = ?, event_name = ?,
                            operation_id = ?
                        WHERE event_id = ?
                        """,
                        (
                            fingerprint,
                            json.dumps(event, sort_keys=True, default=str),
                            _lower(event.get("blockHash")),
                            int(event["blockTimestamp"]),
                            str(event["transactionHash"]).lower(),
                            event.get("eventName"),
                            self._operation_id(event),
                            event_id,
                        ),
                    )
                conn.execute(
                    f"""
                    UPDATE {self._events} SET
                        {seen_col} = COALESCE({seen_col}, ?), {count_col} = {count_col} + 1, updated_at = ?
                    WHERE event_id = ?
                    """,
                    (now, now, event_id),
                )
        result.new_ids = tuple(new_ids)
        result.changed_ids = tuple(changed_ids)
        return result

    def mark_missing(
        self, chain_id: int, from_block: int, to_block: int, seen_ids: set[str], source: str, now: float
    ) -> list[str]:
        """Mark active events in a fully fetched block range that the indexer no longer returns.

        Only call this after every page of ``[from_block, to_block]`` was fetched:
        a partial range would mark real events as removed.

        Returns:
            The ids newly marked as removed.
        """
        with self._connect(write=True) as conn:
            rows = conn.execute(
                f"""
                SELECT event_id, fingerprint, payload_json FROM {self._events}
                WHERE chain_id = ? AND block_number BETWEEN ? AND ? AND status = 'active'
                """,
                (chain_id, from_block, to_block),
            ).fetchall()
            removed = [row for row in rows if row["event_id"] not in seen_ids]
            for row in removed:
                conn.execute(
                    f"""
                    INSERT INTO {self._changes} (
                        event_id, chain_id, kind, old_fingerprint, new_fingerprint, source, observed_at,
                        old_payload_json
                    ) VALUES (?, ?, 'removed', ?, NULL, ?, ?, ?)
                    """,
                    (row["event_id"], chain_id, row["fingerprint"], source, now, row["payload_json"]),
                )
                conn.execute(
                    f"UPDATE {self._events} SET status = 'removed', revision = revision + 1, updated_at = ? "
                    "WHERE event_id = ?",
                    (now, row["event_id"]),
                )
        return [row["event_id"] for row in removed]

    def get_checkpoint(self, chain_id: int) -> Checkpoint | None:
        """Return the stored checkpoint for a chain, or None before its first reconcile."""
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM {self._checkpoints} WHERE chain_id = ?", (chain_id,)).fetchone()
        if row is None:
            return None
        return Checkpoint(
            chain_id=row["chain_id"],
            block_number=row["block_number"],
            indexed_head=row["indexed_head"],
            head_changed_at=row["head_changed_at"],
            updated_at=row["updated_at"],
        )

    def checkpoints(self) -> list[Checkpoint]:
        """Return every chain's checkpoint, ordered by chain id."""
        with self._connect() as conn:
            ids = [r["chain_id"] for r in conn.execute(f"SELECT chain_id FROM {self._checkpoints} ORDER BY 1")]
        return [cp for chain_id in ids if (cp := self.get_checkpoint(chain_id))]

    def set_checkpoint(self, chain_id: int, block_number: int, indexed_head: int, now: float) -> None:
        """Advance a chain's checkpoint; it never moves backwards."""
        with self._connect(write=True) as conn:
            previous = conn.execute(
                f"SELECT block_number, indexed_head, head_changed_at FROM {self._checkpoints} WHERE chain_id = ?",
                (chain_id,),
            ).fetchone()
            head_changed_at = now
            if previous is not None:
                block_number = max(block_number, previous["block_number"])
                if indexed_head <= previous["indexed_head"]:
                    indexed_head = previous["indexed_head"]
                    head_changed_at = previous["head_changed_at"]
            conn.execute(
                f"""
                INSERT INTO {self._checkpoints} (chain_id, block_number, indexed_head, head_changed_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chain_id) DO UPDATE SET
                    block_number = excluded.block_number, indexed_head = excluded.indexed_head,
                    head_changed_at = excluded.head_changed_at, updated_at = excluded.updated_at
                """,
                (chain_id, block_number, indexed_head, head_changed_at, now),
            )

    def log(self, kind: str, now: float, duration_ms: float | None = None, **details: Any) -> None:
        """Append a worker log entry (ws_connected, ws_disconnected, reconcile, ...)."""
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {self._log} (kind, at, duration_ms, details_json) VALUES (?, ?, ?, ?)",
                (kind, now, duration_ms, json.dumps(details, sort_keys=True, default=str)),
            )

    def log_entries(self, since: float, kinds: Iterable[str] | None = None) -> list[sqlite3.Row]:
        """Return worker log entries at or after ``since``, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM {self._log} WHERE at >= ? ORDER BY at, id", (since,)).fetchall()
        wanted = set(kinds) if kinds is not None else None
        return [row for row in rows if wanted is None or row["kind"] in wanted]

    def last_log_entry_before(self, before: float, kinds: Iterable[str]) -> sqlite3.Row | None:
        """Return the newest worker log entry of one of ``kinds`` strictly before ``before``."""
        wanted = list(kinds)
        placeholders = ", ".join("?" for _ in wanted)
        with self._connect() as conn:
            return conn.execute(
                f"SELECT * FROM {self._log} WHERE at < ? AND kind IN ({placeholders}) ORDER BY at DESC, id DESC "
                "LIMIT 1",
                (before, *wanted),
            ).fetchone()

    def get_events(self, event_ids: Iterable[str]) -> list[sqlite3.Row]:
        """Return inbox rows for ``event_ids``, in the given order (unknown ids are skipped)."""
        ids = list(event_ids)
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM {self._events} WHERE event_id IN ({placeholders})", ids).fetchall()
        by_id = {row["event_id"]: row for row in rows}
        return [by_id[event_id] for event_id in ids if event_id in by_id]

    def events_since(self, since_block_timestamp: int) -> list[sqlite3.Row]:
        """Return inbox rows whose block timestamp is at or after ``since_block_timestamp``."""
        with self._connect() as conn:
            return conn.execute(
                f"SELECT * FROM {self._events} WHERE block_timestamp >= ? ORDER BY chain_id, block_number, log_index",
                (since_block_timestamp,),
            ).fetchall()

    def changes_since(self, since: float) -> list[sqlite3.Row]:
        """Return change records observed at or after ``since``."""
        with self._connect() as conn:
            return conn.execute(
                f"SELECT * FROM {self._changes} WHERE observed_at >= ? ORDER BY observed_at, id", (since,)
            ).fetchall()

    def set_status(self, key: str, value: Any, now: float) -> None:
        """Store one worker status value (JSON-encoded)."""
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {self._status} (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, sort_keys=True, default=str), now),
            )

    def status(self) -> dict[str, tuple[Any, float]]:
        """Return all worker status values with their update time."""
        with self._connect() as conn:
            rows = conn.execute(f"SELECT key, value, updated_at FROM {self._status}").fetchall()
        return {row["key"]: (json.loads(row["value"]), row["updated_at"]) for row in rows}

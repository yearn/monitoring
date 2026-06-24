from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any, NotRequired, TypedDict

from utils.paths import cache_path

_initialized = False
_initialized_path: str | None = None


class AlertEvent(TypedDict):
    id: int
    created_at: str
    source: str
    protocol: str
    channel: str
    severity: str | None
    message: str
    plain_text: bool
    silent: bool
    delivery_status: str
    delivered_at: str | None
    delivery_error: str | None
    dedupe_key: NotRequired[str | None]
    fingerprint: NotRequired[str | None]
    metadata: dict[str, Any]


def format_utc_iso(value: datetime) -> str:
    """Return a fixed-width UTC ISO-8601 timestamp suitable for TEXT comparisons."""
    if value.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_timestamp(value: str) -> str:
    """Normalize an ISO-8601 timestamp to fixed-width UTC."""
    raw = value
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    parsed = datetime.fromisoformat(raw)
    return format_utc_iso(parsed)


def utc_now_iso() -> str:
    return format_utc_iso(datetime.now(UTC))


def db_path() -> str:
    """Return the SQLite database path under CACHE_DIR."""
    return cache_path("monitoring.db")


def initialize_database() -> None:
    """Create the SQLite database schema if needed."""
    with closing(_connect()):
        pass


def checkpoint_wal() -> None:
    """Checkpoint and truncate SQLite WAL files."""
    with closing(_connect()) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    global _initialized, _initialized_path
    current_path = db_path()
    if _initialized and _initialized_path == current_path:
        return
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alert_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      TEXT NOT NULL,
            source          TEXT NOT NULL,
            protocol        TEXT NOT NULL,
            channel         TEXT NOT NULL DEFAULT '',
            severity        TEXT,
            message         TEXT NOT NULL,
            plain_text      INTEGER NOT NULL DEFAULT 0,
            silent          INTEGER NOT NULL DEFAULT 0,
            delivery_status TEXT NOT NULL DEFAULT 'generated',
            delivered_at    TEXT,
            delivery_error  TEXT,
            dedupe_key      TEXT,
            fingerprint     TEXT,
            metadata_json   TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_alert_events_created_at
            ON alert_events(created_at);

        CREATE INDEX IF NOT EXISTS idx_alert_events_protocol_created_id
            ON alert_events(protocol, created_at, id);

        CREATE INDEX IF NOT EXISTS idx_alert_events_severity_created_id
            ON alert_events(severity, created_at, id);

        CREATE INDEX IF NOT EXISTS idx_alert_events_source_created_id
            ON alert_events(source, created_at, id);

        CREATE TABLE IF NOT EXISTS monitor_state (
            namespace  TEXT NOT NULL,
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (namespace, key)
        );
        """
    )
    conn.commit()
    _initialized = True
    _initialized_path = current_path


def _row_to_alert(row: sqlite3.Row) -> AlertEvent:
    metadata_raw = row["metadata_json"]
    metadata = json.loads(metadata_raw) if metadata_raw else {}
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "source": row["source"],
        "protocol": row["protocol"],
        "channel": row["channel"],
        "severity": row["severity"],
        "message": row["message"],
        "plain_text": bool(row["plain_text"]),
        "silent": bool(row["silent"]),
        "delivery_status": row["delivery_status"],
        "delivered_at": row["delivered_at"],
        "delivery_error": row["delivery_error"],
        "dedupe_key": row["dedupe_key"],
        "fingerprint": row["fingerprint"],
        "metadata": metadata,
    }


def record_alert(
    *,
    message: str,
    protocol: str,
    channel: str = "",
    severity: str | None = None,
    source: str = "protocol",
    plain_text: bool = False,
    silent: bool = False,
    delivery_status: str = "generated",
    metadata: dict[str, object] | None = None,
    dedupe_key: str | None = None,
    fingerprint: str | None = None,
) -> int:
    """Insert an alert event and return its id."""
    # `closing(...)` closes the connection (sqlite3's own `with` only manages the
    # transaction, never closes); the trailing `conn` commits/rolls back the write.
    with closing(_connect()) as conn, conn:
        cursor = conn.execute(
            """
            INSERT INTO alert_events (
                created_at, source, protocol, channel, severity, message, plain_text,
                silent, delivery_status, dedupe_key, fingerprint, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                source,
                protocol,
                channel,
                severity,
                message,
                int(plain_text),
                int(silent),
                delivery_status,
                dedupe_key,
                fingerprint,
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
        alert_id = cursor.lastrowid
        if alert_id is None:
            raise RuntimeError("SQLite did not return an alert id")
        return alert_id


def update_alert_delivery(
    alert_id: int,
    *,
    status: str,
    delivered_at: str | None = None,
    error: str | None = None,
) -> None:
    """Update Telegram delivery fields for an alert event."""
    if delivered_at is None and status == "delivered":
        delivered_at = utc_now_iso()
    elif delivered_at is not None:
        delivered_at = normalize_timestamp(delivered_at)
    with closing(_connect()) as conn, conn:
        conn.execute(
            """
            UPDATE alert_events
            SET delivery_status = ?, delivered_at = ?, delivery_error = ?
            WHERE id = ?
            """,
            (status, delivered_at, error, alert_id),
        )


def get_alert(alert_id: int) -> AlertEvent | None:
    """Return one alert event by id."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM alert_events WHERE id = ?", (alert_id,)).fetchone()
    return _row_to_alert(row) if row else None


def query_alerts(
    *,
    protocol: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    cursor: int | None = None,
    limit: int = 100,
) -> list[AlertEvent]:
    """Return alert events ordered by id descending."""
    limit = max(1, min(limit, 1000))
    clauses: list[str] = []
    params: list[object] = []
    for column, value in (("protocol", protocol), ("severity", severity), ("source", source)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if from_ts is not None:
        clauses.append("created_at >= ?")
        params.append(normalize_timestamp(from_ts))
    if to_ts is not None:
        clauses.append("created_at < ?")
        params.append(normalize_timestamp(to_ts))
    if cursor is not None:
        clauses.append("id < ?")
        params.append(cursor)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM alert_events {where} ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with closing(_connect()) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_alert(row) for row in rows]


def prune_alerts(older_than_days: int) -> int:
    """Delete old alert events and return the number of deleted rows."""
    cutoff = format_utc_iso(datetime.now(UTC) - timedelta(days=older_than_days))
    with closing(_connect()) as conn, conn:
        cursor = conn.execute("DELETE FROM alert_events WHERE created_at < ?", (cutoff,))
        return cursor.rowcount


def state_get(namespace: str, key: str) -> str | None:
    """Return a stored monitor state value."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT value FROM monitor_state WHERE namespace = ? AND key = ?",
            (namespace, key),
        ).fetchone()
    return str(row["value"]) if row else None


def state_set(namespace: str, key: str, value: str) -> None:
    """Upsert a monitor state value."""
    with closing(_connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO monitor_state (namespace, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(namespace, key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (namespace, key, value, utc_now_iso()),
        )

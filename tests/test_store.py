from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from utils import paths, store


def _use_cache_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(paths, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(store, "_initialized", False)
    monkeypatch.setattr(store, "_initialized_path", None)


def test_db_path_uses_cache_dir(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    assert store.db_path() == str(tmp_path / "monitoring.db")


def test_record_get_update_and_metadata(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    alert_id = store.record_alert(
        message="hello",
        protocol="aave",
        channel="alerts",
        severity="LOW",
        plain_text=True,
        silent=True,
        metadata={"tx": "0xabc"},
    )

    store.update_alert_delivery(alert_id, status="delivered", delivered_at="2026-06-11T10:00:00Z")
    row = store.get_alert(alert_id)

    assert row is not None
    assert row["message"] == "hello"
    assert row["protocol"] == "aave"
    assert row["channel"] == "alerts"
    assert row["severity"] == "LOW"
    assert row["plain_text"] is True
    assert row["silent"] is True
    assert row["delivery_status"] == "delivered"
    assert row["delivered_at"] == "2026-06-11T10:00:00.000000Z"
    assert row["metadata"] == {"tx": "0xabc"}


def test_get_alert_missing(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    assert store.get_alert(999) is None


def test_query_filters_order_cursor_and_limit(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    old_id = store.record_alert(message="old", protocol="aave", severity="LOW", source="protocol")
    new_id = store.record_alert(message="new", protocol="morpho", severity="HIGH", source="ops_error")

    with sqlite3.connect(store.db_path()) as conn:
        conn.execute("UPDATE alert_events SET created_at = ? WHERE id = ?", ("2026-06-10T00:00:00Z", old_id))
        conn.execute("UPDATE alert_events SET created_at = ? WHERE id = ?", ("2026-06-11T00:00:00Z", new_id))

    assert [row["id"] for row in store.query_alerts()] == [new_id, old_id]
    assert [row["id"] for row in store.query_alerts(protocol="aave")] == [old_id]
    assert [row["id"] for row in store.query_alerts(severity="HIGH")] == [new_id]
    assert [row["id"] for row in store.query_alerts(source="ops_error")] == [new_id]
    assert [row["id"] for row in store.query_alerts(from_ts="2026-06-10T12:00:00Z")] == [new_id]
    assert [row["id"] for row in store.query_alerts(to_ts="2026-06-10T12:00:00Z")] == [old_id]
    assert [row["id"] for row in store.query_alerts(cursor=new_id)] == [old_id]
    assert len(store.query_alerts(limit=5000)) == 2


def test_query_timestamp_filters_normalize_second_precision_bounds(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    alert_id = store.record_alert(message="same second", protocol="aave")

    with sqlite3.connect(store.db_path()) as conn:
        conn.execute(
            "UPDATE alert_events SET created_at = ? WHERE id = ?",
            ("2026-06-11T00:00:00.123456Z", alert_id),
        )

    assert [row["id"] for row in store.query_alerts(from_ts="2026-06-11T00:00:00Z")] == [alert_id]
    assert store.query_alerts(to_ts="2026-06-11T00:00:00Z") == []


def test_prune_alerts(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    old_id = store.record_alert(message="old", protocol="aave")
    keep_id = store.record_alert(message="keep", protocol="aave")
    old_ts = (datetime.now(UTC) - timedelta(days=45)).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(store.db_path()) as conn:
        conn.execute("UPDATE alert_events SET created_at = ? WHERE id = ?", (old_ts, old_id))

    assert store.prune_alerts(30) == 1
    assert store.get_alert(old_id) is None
    assert store.get_alert(keep_id) is not None


def test_wal_read_while_write(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    alert_id = store.record_alert(message="hello", protocol="aave")
    reader = sqlite3.connect(store.db_path())
    try:
        assert reader.execute("SELECT count(*) FROM alert_events").fetchone()[0] == 1
        store.update_alert_delivery(alert_id, status="delivered")
        assert store.get_alert(alert_id)["delivery_status"] == "delivered"  # type: ignore[index]
    finally:
        reader.close()


def test_monitor_state_round_trip(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    assert store.state_get("cache-id.txt", "aave") is None
    store.state_set("cache-id.txt", "aave", "42")
    assert store.state_get("cache-id.txt", "aave") == "42"
    store.state_set("cache-id.txt", "aave", "43")
    assert store.state_get("cache-id.txt", "aave") == "43"
    assert store.state_get("cache-id-daily.txt", "aave") is None

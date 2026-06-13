from __future__ import annotations

import json
import sqlite3
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from api.server import AlertsHandler, parse_alert_query
from utils import paths, store


def _use_cache_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(paths, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(store, "_initialized", False)
    monkeypatch.setattr(store, "_initialized_path", None)


def _request(server: ThreadingHTTPServer, method: str, path: str) -> tuple[int, dict]:
    host, port = server.server_address
    conn = HTTPConnection(host, port)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        body = response.read()
        return response.status, json.loads(body.decode())
    finally:
        conn.close()


def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), AlertsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_parse_alert_query_validates_timestamps():
    parsed = parse_alert_query("since=2026-06-11T01:00:00%2B01:00&to=2026-06-11T02:00:00Z&limit=999")
    assert parsed.from_ts == "2026-06-11T00:00:00.000000Z"
    assert parsed.limit == 500


def test_healthz(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    server = _server()
    try:
        status, body = _request(server, "GET", "/healthz")
    finally:
        server.shutdown()
    assert status == 200
    assert body == {"status": "ok"}


def test_alerts_routes_filters_and_errors(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    aave_id = store.record_alert(message="aave", protocol="aave", severity="LOW", source="protocol")
    morpho_id = store.record_alert(message="morpho", protocol="morpho", severity="HIGH", source="ops_error")
    server = _server()
    try:
        status, body = _request(server, "GET", "/v1/alerts?limit=1")
        assert status == 200
        assert body["data"][0]["id"] == morpho_id
        assert body["next_cursor"] == str(morpho_id)

        status, body = _request(server, "GET", f"/v1/alerts?cursor={morpho_id}")
        assert status == 200
        assert [row["id"] for row in body["data"]] == [aave_id]

        status, body = _request(server, "GET", "/v1/alerts?protocol=aave&severity=LOW&source=protocol")
        assert status == 200
        assert [row["id"] for row in body["data"]] == [aave_id]

        status, body = _request(server, "GET", f"/v1/alerts/{aave_id}")
        assert status == 200
        assert body["message"] == "aave"

        status, body = _request(server, "GET", "/v1/alerts/999")
        assert status == 404
        assert body["error"] == "not_found"

        status, body = _request(server, "GET", "/v1/alerts?severity=BAD")
        assert status == 400
        assert body["error"] == "bad_request"

        status, _ = _request(server, "GET", "/v1/alerts?from=2026-06-11T00:00:00")
        assert status == 400

        status, _ = _request(server, "GET", "/v1/alerts?from=2026-06-11T01:00:00Z&to=2026-06-11T00:00:00Z")
        assert status == 400

        status, _ = _request(server, "GET", "/unknown")
        assert status == 404

        status, _ = _request(server, "POST", "/v1/alerts")
        assert status == 405
    finally:
        server.shutdown()


def test_alerts_route_normalizes_second_precision_timestamp_bounds(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    alert_id = store.record_alert(message="same second", protocol="aave")
    with sqlite3.connect(store.db_path()) as conn:
        conn.execute(
            "UPDATE alert_events SET created_at = ? WHERE id = ?",
            ("2026-06-11T00:00:00.123456Z", alert_id),
        )

    server = _server()
    try:
        status, body = _request(server, "GET", "/v1/alerts?from=2026-06-11T00:00:00Z")
        assert status == 200
        assert [row["id"] for row in body["data"]] == [alert_id]

        status, body = _request(server, "GET", "/v1/alerts?to=2026-06-11T00:00:00Z")
        assert status == 200
        assert body["data"] == []
    finally:
        server.shutdown()

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from api.server import AlertsHandler
from automation.config import JobsConfig, Profile, Task, load_jobs_config
from utils.monitoring_config import (
    MonitoringConfigError,
    load_monitoring_config,
    monitoring_to_json,
    protocol_to_json,
)


def test_load_monitoring_config_parses_root_file():
    config = load_monitoring_config()
    assert config.version
    assert config.protocols
    assert "3jane" in config.protocols


def test_protocol_to_json_structure():
    config = load_monitoring_config()
    protocol = config.protocols["3jane"]
    data = protocol_to_json(protocol)
    assert data["slug"] == "3jane"
    assert data["display_name"] == "3Jane"
    assert data["cadence"] == "Hourly"
    assert data["monitor_count"] == len(data["monitors"])
    assert all("name" in m and "description" in m for m in data["monitors"])


def test_monitoring_to_json_is_sorted_and_counted():
    config = load_monitoring_config()
    data = monitoring_to_json(config)
    assert data["version"] == config.version
    assert data["count"] == len(config.protocols)
    slugs = [p["slug"] for p in data["data"]]
    assert slugs == sorted(slugs)


def test_invalid_severity_rejected():
    from utils.monitoring_config import Monitor

    with pytest.raises(MonitoringConfigError, match="invalid severity"):
        Monitor(name="x", description="y", severity="WRONG")


def test_missing_required_keys_rejected(tmp_path):
    path = tmp_path / "monitoring.yaml"
    path.write_text("version: '1.0'\nprotocols:\n  bad:\n    display_name: Bad\n", encoding="utf-8")
    with pytest.raises(MonitoringConfigError, match="missing keys"):
        load_monitoring_config(path)


def _protocol_from_script(script: str) -> str | None:
    parts = script.split("/")
    if len(parts) < 2 or parts[0] != "protocols" or not parts[1]:
        return None
    return parts[1]


def test_all_jobs_yaml_protocols_have_monitoring_metadata():
    """Every enabled protocol task in jobs.yaml must have a monitoring.yaml entry."""
    jobs = load_jobs_config()
    monitoring = load_monitoring_config()

    protocols_with_tasks: set[str] = set()
    for profile in jobs.enabled_profiles:
        for task in profile.enabled_tasks:
            protocol = _protocol_from_script(task.script)
            if protocol is not None:
                protocols_with_tasks.add(protocol)

    missing = protocols_with_tasks - set(monitoring.protocols)
    assert not missing, f"protocols in jobs.yaml without monitoring.yaml entry: {sorted(missing)}"


def test_monitoring_tasks_exist_in_jobs_yaml():
    """Every task referenced in an enabled monitoring.yaml entry must be scheduled in jobs.yaml."""
    jobs = load_jobs_config()
    monitoring = load_monitoring_config()

    scheduled_tasks: set[str] = set()
    for profile in jobs.enabled_profiles:
        for task in profile.enabled_tasks:
            scheduled_tasks.add(task.script)

    errors: list[str] = []
    for protocol in monitoring.protocols.values():
        if protocol.disabled:
            continue
        for task in protocol.tasks:
            if task not in scheduled_tasks:
                errors.append(f"{protocol.slug}: {task}")

    assert not errors, f"monitoring.yaml tasks not found in jobs.yaml: {errors}"


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


def test_disabled_protocols_included():
    config = load_monitoring_config()
    disabled = {slug for slug, p in config.protocols.items() if p.disabled}
    expected = {"euler", "pendle", "silo"}
    assert expected <= disabled, f"expected disabled protocols {expected} to be present, got {disabled}"

    for slug in expected:
        data = protocol_to_json(config.protocols[slug])
        assert data["disabled"] is True


def test_api_monitoring_route(monkeypatch, tmp_path):
    config = JobsConfig(
        profiles={
            "hourly": Profile(
                name="hourly",
                cron="5 * * * *",
                tasks=[Task(name="demo", script="protocols/demo/main.py")],
            ),
        },
        path=Path("jobs.yaml"),
    )
    monkeypatch.setattr("api.server.load_jobs_config", lambda: config)
    server = _server()
    try:
        status, body = _request(server, "GET", "/v1/monitoring")
    finally:
        server.shutdown()

    assert status == 200
    assert body["version"]
    assert isinstance(body["data"], list)
    assert body["count"] == len(body["data"])
    assert any(p["slug"] == "3jane" for p in body["data"])

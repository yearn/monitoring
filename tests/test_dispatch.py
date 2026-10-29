"""Durable sender behavior: fanout, escalation, retry and legacy liquidity compatibility."""

import hashlib
import hmac
import json
import multiprocessing
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from automation.config import load_jobs_config
from utils import dispatch
from utils.alert import Alert, AlertSeverity
from utils.dispatch_queue import Destination, DispatchQueue


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    monkeypatch.setenv("LIQUIDITY_WEBHOOK_SECRET", "liquidity-test-key")
    monkeypatch.setenv("VAULTS_MOMMY_WEBHOOK_SECRET", "mommy-test-key")
    clock = [1000.0]
    monkeypatch.setattr("utils.dispatch_queue.time.time", lambda: clock[0])
    return clock


@pytest.fixture
def post(monkeypatch: pytest.MonkeyPatch) -> Mock:
    mock = Mock(return_value=SimpleNamespace(status_code=200, headers={}, close=Mock()))
    monkeypatch.setattr(dispatch.requests, "post", mock)
    return mock


def alert(severity: AlertSeverity = AlertSeverity.HIGH, protocol: str = "3jane") -> Alert:
    return Alert(severity, "Reserves low — USD3", protocol, channel="pegs")


def test_fanout_is_durable_before_first_send_and_keeps_liquidity_envelope(configured: list[float], post: Mock) -> None:
    def accept(url: str, **kwargs):
        records = DispatchQueue().records()
        assert len(records) == 2 and all(row["attempts"] <= 1 for row in records)
        return SimpleNamespace(status_code=200, headers={}, close=Mock())

    post.side_effect = accept
    dispatch.dispatch_emergency_withdrawal(alert())
    assert post.call_count == 2
    liquidity, mommy = [call.kwargs for call in post.call_args_list]
    legacy = json.loads(liquidity["data"])
    assert legacy == {
        "event_type": "emergency_withdrawal",
        "client_payload": {"protocol": "3jane", "severity": "HIGH", "message": "Reserves low — USD3"},
    }
    new = json.loads(mommy["data"])
    assert new["client_payload"] == legacy["client_payload"]
    assert new["event_id"] == DispatchQueue().records()[0]["event_id"]
    for kwargs, secret in ((liquidity, "liquidity-test-key"), (mommy, "mommy-test-key")):
        assert (
            kwargs["headers"]["X-Hub-Signature-256"]
            == "sha256=" + hmac.new(secret.encode(), kwargs["data"], hashlib.sha256).hexdigest()
        )
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == (3, 10)
    assert all(row["status"] == "delivered" for row in DispatchQueue().records())


def test_high_then_critical_twenty_minutes_later_bypasses_cooldown(configured: list[float], post: Mock) -> None:
    dispatch.dispatch_emergency_withdrawal(alert())
    first_ids = {row["event_id"] for row in DispatchQueue().records()}
    configured[0] += 1200
    dispatch.dispatch_emergency_withdrawal(alert(AlertSeverity.CRITICAL))
    records = DispatchQueue().records()
    assert post.call_count == 4
    assert {row["severity"] for row in records} == {"HIGH", "CRITICAL"}
    assert first_ids.isdisjoint({row["event_id"] for row in records if row["severity"] == "CRITICAL"})
    dispatch.dispatch_emergency_withdrawal(alert())
    dispatch.dispatch_emergency_withdrawal(alert(AlertSeverity.CRITICAL))
    assert post.call_count == 4
    configured[0] += 3601
    dispatch.dispatch_emergency_withdrawal(alert(AlertSeverity.CRITICAL))
    assert post.call_count == 6


def test_unavailable_destination_does_not_suppress_other_and_retry_survives_restart(
    configured: list[float], post: Mock
) -> None:
    def fail_one(url: str, **kwargs):
        if "8081" in url:
            raise requests.Timeout("down")
        return SimpleNamespace(status_code=200, headers={}, close=Mock())

    post.side_effect = fail_one
    dispatch.dispatch_emergency_withdrawal(alert())
    records = DispatchQueue().records()
    assert [row["status"] for row in records] == ["delivered", "retry"]
    saved_body = post.call_args_list[1].kwargs["data"]
    configured[0] += 60
    post.side_effect = None
    # A new queue instance models restart. No new alert is generated.
    assert dispatch.drain_emergency_deliveries(queue=DispatchQueue()) == 1
    assert post.call_args.kwargs["data"] == saved_body
    assert all(row["status"] == "delivered" for row in DispatchQueue().records())


def test_pending_high_never_prevents_new_critical(configured: list[float], post: Mock) -> None:
    post.side_effect = requests.ConnectionError("down")
    dispatch.dispatch_emergency_withdrawal(alert())
    configured[0] += 1200
    dispatch.dispatch_emergency_withdrawal(alert(AlertSeverity.CRITICAL))
    records = DispatchQueue().records()
    assert len(records) == 4
    assert len({row["event_id"] for row in records}) == 2


@pytest.mark.parametrize("code", [408, 429, 500, 503])
def test_retryable_http_outcomes_keep_stable_id_and_body(configured: list[float], post: Mock, code: int) -> None:
    post.return_value = SimpleNamespace(status_code=code, headers={"Retry-After": "120"}, close=Mock())
    dispatch.dispatch_emergency_withdrawal(alert())
    records = DispatchQueue().records()
    assert all(row["status"] == "retry" for row in records)
    assert all(row["next_attempt_at"] == 1120 if code == 429 else row["next_attempt_at"] == 1030 for row in records)
    assert dispatch.drain_emergency_deliveries() == 0
    configured[0] = 1121
    before = [call.kwargs["data"] for call in post.call_args_list]
    dispatch.drain_emergency_deliveries()
    assert [call.kwargs["data"] for call in post.call_args_list[2:]] == before


@pytest.mark.parametrize("code", [302, 400, 401, 403, 404, 422])
def test_terminal_errors_require_explicit_operator_retry(configured: list[float], post: Mock, code: int) -> None:
    post.return_value = SimpleNamespace(status_code=code, headers={}, close=Mock())
    dispatch.dispatch_emergency_withdrawal(alert())
    records = DispatchQueue().records()
    assert all(row["status"] == "terminal" for row in records)
    configured[0] += 7200
    assert dispatch.drain_emergency_deliveries() == 0
    dispatch.dispatch_emergency_withdrawal(alert())
    assert len(DispatchQueue().records()) == 2
    queue = DispatchQueue()
    queue.retry_terminal(records[0]["id"])
    post.return_value = SimpleNamespace(status_code=200, headers={}, close=Mock())
    assert dispatch.drain_emergency_deliveries(queue=queue) == 1
    assert queue.records()[0]["event_id"] == records[0]["event_id"]


def test_independent_credentials_and_opt_in_preserve_liquidity(
    configured: list[float], post: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VAULTS_MOMMY_WEBHOOK_SECRET")
    dispatch.dispatch_emergency_withdrawal(alert())
    assert post.call_count == 1
    assert post.call_args.args[0] == dispatch.DEFAULT_WEBHOOK_URL
    monkeypatch.setenv("VAULTS_MOMMY_WEBHOOK_SECRET", "mommy-test-key")
    dispatch.dispatch_emergency_withdrawal(alert())
    assert post.call_count == 2 and "8081" in post.call_args.args[0]


def test_endpoint_override_remains_compatible(
    configured: list[float], post: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIQUIDITY_WEBHOOK_URL", "http://localhost:9000/webhook/emergency")
    dispatch.dispatch_emergency_withdrawal(alert())
    assert post.call_args_list[0].args[0] == "http://localhost:9000/webhook/emergency"


def test_endpoint_change_needs_explicit_review_before_rerouting(
    configured: list[float], post: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    post.side_effect = requests.Timeout("down")
    dispatch.dispatch_emergency_withdrawal(alert())
    configured[0] += 60
    monkeypatch.setenv("VAULTS_MOMMY_WEBHOOK_URL", "http://127.0.0.1:9001/webhook/emergency")
    dispatch.drain_emergency_deliveries()
    record = DispatchQueue().records()[1]
    assert record["status"] == "terminal"
    DispatchQueue().retry_terminal(record["id"], url="http://127.0.0.1:9001/webhook/emergency")
    post.side_effect = None
    dispatch.drain_emergency_deliveries()
    assert post.call_args.args[0] == "http://127.0.0.1:9001/webhook/emergency"


def test_debug_preserves_existing_queue_without_sending(
    configured: list[float], post: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = DispatchQueue()
    queue.enqueue("3jane", "HIGH", "old event", dispatch.destinations())
    before = queue.records()
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    dispatch.dispatch_emergency_withdrawal(alert(AlertSeverity.CRITICAL))
    assert dispatch.drain_emergency_deliveries(queue=queue) == 0
    assert queue.records() == before
    post.assert_not_called()


@pytest.mark.parametrize(
    "severity,protocol",
    [(AlertSeverity.LOW, "3jane"), (AlertSeverity.MEDIUM, "3jane"), (AlertSeverity.HIGH, "unknown")],
)
def test_ineligible_alerts_do_not_enqueue_or_send(
    configured: list[float], post: Mock, severity: AlertSeverity, protocol: str
) -> None:
    dispatch.dispatch_emergency_withdrawal(alert(severity, protocol))
    assert not DispatchQueue().records()
    post.assert_not_called()


def test_missing_all_secrets_disables_new_delivery(
    configured: list[float], post: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LIQUIDITY_WEBHOOK_SECRET")
    monkeypatch.delenv("VAULTS_MOMMY_WEBHOOK_SECRET")
    dispatch.dispatch_emergency_withdrawal(alert())
    post.assert_not_called()


def test_enqueue_disk_failure_prevents_any_http(
    configured: list[float], post: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(DispatchQueue, "enqueue", fail)
    with pytest.raises(sqlite3.OperationalError):
        dispatch.dispatch_emergency_withdrawal(alert())
    post.assert_not_called()


def test_expired_lease_recovers_same_event_and_stale_ack_cannot_clear_it(tmp_path: Path) -> None:
    queue = DispatchQueue(tmp_path / "queue.sqlite3")
    queue.enqueue("3jane", "HIGH", "test", [Destination("vaults-mommy", "http://local", "secret", True)], now=1000)
    first = queue.claim("vaults-mommy", exclude=set(), now=1000)
    assert first is not None
    assert queue.claim("vaults-mommy", exclude=set(), now=1001) is None
    restarted = DispatchQueue(tmp_path / "queue.sqlite3")
    next_attempt = restarted.claim("vaults-mommy", exclude=set(), now=1061)
    assert next_attempt is not None
    assert next_attempt.body == first.body and next_attempt.event_id == first.event_id
    with pytest.raises(RuntimeError, match="lease changed"):
        queue.finish(first, "delivered", now=1062)
    restarted.finish(next_attempt, "delivered", now=1062)
    assert restarted.records()[0]["attempts"] == 2


def _enqueue_concurrent(path: str) -> None:
    DispatchQueue(path).enqueue(
        "3jane", "HIGH", "same incident", [Destination("liquidity", "http://local", "key")], now=1000
    )


def test_concurrent_monitors_do_not_duplicate_pending_protocol(tmp_path: Path) -> None:
    path = str(tmp_path / "queue.sqlite3")
    DispatchQueue(path).records()
    ctx = multiprocessing.get_context("fork")
    writers = [ctx.Process(target=_enqueue_concurrent, args=(path,)) for _ in range(8)]
    for process in writers:
        process.start()
    for process in writers:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert len(DispatchQueue(path).records()) == 1


def test_unknown_database_schema_refuses_delivery(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="incompatible"):
        DispatchQueue(path).records()


def test_retry_job_is_scheduled_without_monitor_alerts() -> None:
    jobs = load_jobs_config()
    profile = jobs.profiles["emergency_delivery"]
    assert profile.cron == "* * * * *"
    assert any(task.script == "utils/dispatch_retry.py" for task in profile.enabled_tasks)


def test_successful_http_with_failed_ack_persistence_remains_uncertain(
    configured: list[float], post: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = DispatchQueue.finish

    def fail(self, delivery, status, **kwargs):
        raise sqlite3.OperationalError("ack commit failed")

    monkeypatch.setattr(DispatchQueue, "finish", fail)
    with pytest.raises(sqlite3.OperationalError):
        dispatch.dispatch_emergency_withdrawal(alert())
    first = post.call_args.kwargs["data"]
    monkeypatch.setattr(DispatchQueue, "finish", original)
    configured[0] += 61
    dispatch.drain_emergency_deliveries(queue=DispatchQueue())
    assert post.call_args_list[1].kwargs["data"] == first
    assert all(row["status"] == "delivered" for row in DispatchQueue().records())


def test_configured_secrets_are_not_stored(configured: list[float], post: Mock) -> None:
    queue = DispatchQueue()
    dispatch.dispatch_emergency_withdrawal(alert())
    for path in Path(queue.path).parent.glob(Path(queue.path).name + "*"):
        assert b"liquidity-test-key" not in path.read_bytes()
        assert b"mommy-test-key" not in path.read_bytes()

"""Behavioral tests for the hourly Tenderly usage monitor."""

from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from utils.tenderly import usage

NOW = 1000 * usage.HOUR_SECONDS + usage.SETTLE_SECONDS
LATEST_HOUR = 999 * usage.HOUR_SECONDS


def history(*, current: float = 400_000, baseline: float = 100_000) -> dict[str, Any]:
    """Build hourly provider history including an expensive partial current hour."""
    points = [
        {"timestamp": LATEST_HOUR - offset * usage.HOUR_SECONDS, "value": baseline}
        for offset in range(usage.BASELINE_HOURS + 2)
    ]
    points[0]["value"] = current
    points.append({"timestamp": LATEST_HOUR + usage.HOUR_SECONDS, "value": 100_000_000})
    return {"timeseries": [{"labels": {}, "usages": {"TUsConsumed": points}}]}


@pytest.fixture
def monitor(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock, dict[str, str]]:
    """Replace network delivery and persistence boundaries with in-memory fakes."""
    monkeypatch.setenv("TENDERLY_API_KEY", "test-key")
    monkeypatch.setenv("TENDERLY_ACCOUNT", "test-account")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_YEARN_MAINTENANCE", "test-chat")
    monkeypatch.setattr(usage.time, "time", lambda: NOW)
    request = MagicMock()
    request.return_value.json.return_value = history()
    monkeypatch.setattr(usage, "request_with_retry", request)
    send = MagicMock(return_value=True)
    monkeypatch.setattr(usage, "send_telegram_message", send)
    state: dict[str, str] = {}
    monkeypatch.setattr(usage.store, "state_get", lambda namespace, key: state.get(key))
    monkeypatch.setattr(usage.store, "state_set", lambda namespace, key, value: state.update({key: value}))
    return request, send, state


def test_ignores_partial_hour_and_uses_prior_peak() -> None:
    """A normal hourly cron burst stays below the threshold despite a huge partial hour."""
    payload = history(current=600_000)
    payload["timeseries"][0]["usages"]["TUsConsumed"][12]["value"] = 500_000
    check = usage.evaluate_usage(payload, LATEST_HOUR)
    assert check.hour == LATEST_HOUR
    assert check.baseline_peak == 500_000
    assert check.threshold == 1_500_000
    assert not check.is_spike


def test_waits_five_minutes_after_hour_ends(
    monitor: tuple[MagicMock, MagicMock, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The just-ended high-usage hour is not evaluated before the settling delay."""
    monkeypatch.setattr(usage.time, "time", lambda: NOW - 1)
    usage.check_usage()
    monitor[1].assert_not_called()
    assert monitor[2] == {"test-account": str(LATEST_HOUR)}


@pytest.mark.parametrize(
    ("current", "baseline", "spike"),
    [
        (299_999, 100_000, False),
        (300_000, 100_000, True),
        (90_000, 10_000, False),
        (110_000, 10_000, True),
        (99_999, 0, False),
        (100_000, 0, True),
        (0, 0, False),
    ],
)
def test_requires_relative_spike_and_absolute_increase(current: float, baseline: float, spike: bool) -> None:
    """Small traffic increases do not alert merely because the baseline is near zero."""
    assert usage.evaluate_usage(history(current=current, baseline=baseline), LATEST_HOUR).is_spike is spike


def test_daily_cron_is_in_baseline() -> None:
    """The corresponding prior-day hour is included in the peak comparison."""
    payload = history(current=1_000_000)
    payload["timeseries"][0]["usages"]["TUsConsumed"][24]["value"] = 1_000_000
    assert not usage.evaluate_usage(payload, LATEST_HOUR).is_spike


def test_order_does_not_change_the_comparison() -> None:
    """Provider rows may arrive in either chronological order."""
    payload = history()
    payload["timeseries"][0]["usages"]["TUsConsumed"].reverse()
    assert usage.evaluate_usage(payload, LATEST_HOUR).is_spike


@pytest.mark.parametrize("missing_offset", [0, 1, 24])
def test_missing_or_stale_hour_is_not_treated_as_zero(missing_offset: int) -> None:
    """Incomplete coverage must fail rather than suppressing or inventing an alert."""
    payload = history()
    payload["timeseries"][0]["usages"]["TUsConsumed"].pop(missing_offset)
    with pytest.raises(ValueError, match="stale or missing"):
        usage.evaluate_usage(payload, LATEST_HOUR)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"timeseries": []},
        {"timeseries": [None]},
        {"timeseries": [{}, {}]},
        {"timeseries": [{"labels": {"project_id": "partial"}}]},
        {"timeseries": [{"labels": {}, "usages": None}]},
        {"timeseries": [{"labels": {}, "usages": {"TUsConsumed": "invalid"}}]},
        {"timeseries": [{"labels": {}, "usages": {"TUsConsumed": []}}]},
    ],
)
def test_rejects_missing_or_grouped_usage(payload: dict[str, Any]) -> None:
    """A changed provider schema cannot silently narrow the account coverage."""
    with pytest.raises(ValueError, match="Tenderly"):
        usage.evaluate_usage(payload, LATEST_HOUR)


@pytest.mark.parametrize(
    "point",
    [
        None,
        {},
        {"timestamp": True, "value": 100},
        {"timestamp": float("nan"), "value": 100},
        {"timestamp": float("inf"), "value": 100},
        {"timestamp": LATEST_HOUR + 0.5, "value": 100},
        {"timestamp": -usage.HOUR_SECONDS, "value": 100},
        {"timestamp": LATEST_HOUR + 900, "value": 100},
        {"timestamp": LATEST_HOUR, "value": "100"},
        {"timestamp": LATEST_HOUR, "value": -1},
        {"timestamp": LATEST_HOUR, "value": float("nan")},
        {"timestamp": LATEST_HOUR, "value": float("inf")},
        {"timestamp": LATEST_HOUR, "value": True},
    ],
)
def test_rejects_malformed_points(point: Any) -> None:
    """Invalid timestamps and non-finite or negative values must fail clearly."""
    payload = history()
    payload["timeseries"][0]["usages"]["TUsConsumed"][0] = point
    with pytest.raises(ValueError, match="Tenderly"):
        usage.evaluate_usage(payload, LATEST_HOUR)


def test_duplicate_buckets_are_rejected() -> None:
    """Two values for one hour cannot produce a reliable comparison."""
    payload = history()
    points = payload["timeseries"][0]["usages"]["TUsConsumed"]
    points.append(points[0])
    with pytest.raises(ValueError, match="duplicate"):
        usage.evaluate_usage(payload, LATEST_HOUR)


def test_accepts_whole_valued_float_timestamps() -> None:
    """JSON numeric timestamps have identical meaning with or without a decimal point."""
    payload = history()
    for point in payload["timeseries"][0]["usages"]["TUsConsumed"]:
        point["timestamp"] = float(point["timestamp"])
    assert usage.evaluate_usage(payload, LATEST_HOUR).is_spike


@pytest.mark.parametrize("failure", ["api", "missing", "skipped", "delivery"])
def test_retries_missed_spike_after_hour_rollover(
    monitor: tuple[MagicMock, MagicMock, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A later cron run must recover an earlier spike even when the new hour is normal."""
    request, send, state = monitor
    first = history()
    if failure == "api":
        request.side_effect = requests.Timeout("timeout")
    elif failure == "missing":
        first["timeseries"][0]["usages"]["TUsConsumed"].pop(0)
    elif failure == "skipped":
        send.return_value = False
    else:
        send.side_effect = requests.Timeout("delivery timeout")
    request.return_value.json.return_value = first
    if failure == "skipped":
        usage.check_usage()
    else:
        with pytest.raises((requests.Timeout, ValueError)):
            usage.check_usage()

    request.side_effect = None
    send.reset_mock(side_effect=True, return_value=True)
    send.return_value = True
    second = history()
    second["timeseries"][0]["usages"]["TUsConsumed"][-1]["value"] = 100_000
    request.return_value.json.return_value = second
    monkeypatch.setattr(usage.time, "time", lambda: NOW + usage.HOUR_SECONDS)
    usage.check_usage()
    send.assert_called_once()
    assert "400,000 TUs" in send.call_args.args[0]
    assert state == {"test-account": str(LATEST_HOUR + 2 * usage.HOUR_SECONDS)}


def test_catches_up_multiple_hours_with_one_request(
    monitor: tuple[MagicMock, MagicMock, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery evaluates every pending hour in order without resending processed spikes."""
    request, send, state = monitor
    state["test-account"] = str(LATEST_HOUR)
    payload = history()
    points = payload["timeseries"][0]["usages"]["TUsConsumed"]
    points[-1]["value"] = 100_000
    points.extend(
        [
            {"timestamp": LATEST_HOUR + 2 * usage.HOUR_SECONDS, "value": 1_500_000},
            {"timestamp": LATEST_HOUR + 3 * usage.HOUR_SECONDS, "value": 100_000},
        ]
    )
    request.return_value.json.return_value = payload
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 3 * usage.HOUR_SECONDS)
    usage.check_usage()
    assert send.call_count == 2
    assert "400,000 TUs" in send.call_args_list[0].args[0]
    assert "1,500,000 TUs" in send.call_args_list[1].args[0]
    assert state == {"test-account": str(LATEST_HOUR + 4 * usage.HOUR_SECONDS)}
    usage.check_usage()
    assert send.call_count == 2
    request.assert_called_once()


def test_retains_progress_before_later_delivery_failure(
    monitor: tuple[MagicMock, MagicMock, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed later spike retries without repeating an earlier delivered spike."""
    request, send, state = monitor
    state["test-account"] = str(LATEST_HOUR)
    payload = history()
    payload["timeseries"][0]["usages"]["TUsConsumed"][-1]["value"] = 1_500_000
    request.return_value.json.return_value = payload
    monkeypatch.setattr(usage.time, "time", lambda: NOW + usage.HOUR_SECONDS)
    send.side_effect = [True, False]
    usage.check_usage()
    assert state == {"test-account": str(LATEST_HOUR + usage.HOUR_SECONDS)}
    send.side_effect = None
    send.return_value = True
    usage.check_usage()
    assert send.call_count == 3
    assert "1,500,000 TUs" in send.call_args.args[0]
    assert state == {"test-account": str(LATEST_HOUR + 2 * usage.HOUR_SECONDS)}


def test_does_not_skip_gap_in_pending_history(monitor: tuple[MagicMock, MagicMock, dict[str, str]]) -> None:
    """Unavailable history, including an expired baseline, keeps the pending cursor visible."""
    request, send, state = monitor
    pending = LATEST_HOUR - 7 * 24 * usage.HOUR_SECONDS
    state["test-account"] = str(pending)
    with pytest.raises(ValueError, match="Check Tenderly history before resetting its cursor"):
        usage.check_usage()
    request.assert_called_once()
    send.assert_not_called()
    assert state == {"test-account": str(pending)}


@pytest.mark.parametrize("cursor", ["invalid", "0.5", "-3600", str(LATEST_HOUR + 900), str(NOW + 24 * 3600)])
def test_corrupt_cursor_is_not_silently_reset(
    monitor: tuple[MagicMock, MagicMock, dict[str, str]], cursor: str
) -> None:
    """Bad saved state requires inspection rather than silently discarding unprocessed hours."""
    request, send, state = monitor
    state["test-account"] = cursor
    with pytest.raises(ValueError, match="Invalid Tenderly usage cursor"):
        usage.check_usage()
    request.assert_not_called()
    send.assert_not_called()
    assert state == {"test-account": cursor}


def test_first_start_does_not_alert_on_old_history(monitor: tuple[MagicMock, MagicMock, dict[str, str]]) -> None:
    """First startup monitors the newest settled hour without historical alert spam."""
    request, send, state = monitor
    payload = history(current=100_000)
    payload["timeseries"][0]["usages"]["TUsConsumed"][2]["value"] = 1_500_000
    request.return_value.json.return_value = payload
    usage.check_usage()
    send.assert_not_called()
    assert state == {"test-account": str(LATEST_HOUR + usage.HOUR_SECONDS)}


def test_sends_once_and_records_only_delivered_hour(monitor: tuple[MagicMock, MagicMock, dict[str, str]]) -> None:
    """Rerunning an already delivered anomalous hour does not send again."""
    request, send, state = monitor
    usage.check_usage()
    usage.check_usage()
    assert request.call_args.kwargs["params"] == {"timespan": "1w", "cumulative": "false"}
    assert request.call_args.kwargs["headers"] == {"X-Access-Key": "test-key"}
    send.assert_called_once()
    message, channel = send.call_args.args
    assert "test-account" in message
    assert "400,000 TUs" in message
    assert "100,000 TUs/hour" in message
    assert "UTC" in message
    assert "test-key" not in message
    assert channel == "yearn_maintenance"
    assert send.call_args.kwargs["origin_protocol"] == "yearn-internal"
    assert state == {"test-account": str(LATEST_HOUR + usage.HOUR_SECONDS)}


def test_falls_back_to_yearn_channel(
    monitor: tuple[MagicMock, MagicMock, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing Yearn destination is used when no maintenance chat is configured."""
    monkeypatch.delenv("TELEGRAM_CHAT_ID_YEARN_MAINTENANCE")
    usage.check_usage()
    assert monitor[1].call_args.args[1] == "yearn"


def test_normal_usage_advances_without_alerting(monitor: tuple[MagicMock, MagicMock, dict[str, str]]) -> None:
    """Normal hours advance the cursor so they do not need to be checked again."""
    request, send, state = monitor
    request.return_value.json.return_value = history(current=100_000)
    usage.check_usage()
    send.assert_not_called()
    assert state == {"test-account": str(LATEST_HOUR + usage.HOUR_SECONDS)}


def test_dry_run_has_no_delivery_or_state_writes(monitor: tuple[MagicMock, MagicMock, dict[str, str]]) -> None:
    """An anomalous live sample remains safe to inspect in dry-run mode."""
    _, send, state = monitor
    usage.check_usage(dry_run=True)
    send.assert_not_called()
    assert not state


def test_skipped_send_can_retry(monitor: tuple[MagicMock, MagicMock, dict[str, str]]) -> None:
    """Missing Telegram credentials or debug mode must not consume the dedupe cursor."""
    _, send, state = monitor
    send.return_value = False
    usage.check_usage()
    assert state == {"test-account": str(LATEST_HOUR)}
    send.return_value = True
    usage.check_usage()
    assert send.call_count == 2
    assert state


def test_failed_send_can_retry(monitor: tuple[MagicMock, MagicMock, dict[str, str]]) -> None:
    """A delivery failure leaves the anomalous hour available for retry."""
    _, send, state = monitor
    send.side_effect = RuntimeError("Telegram unavailable")
    with pytest.raises(RuntimeError, match="Telegram unavailable"):
        usage.check_usage()
    assert state == {"test-account": str(LATEST_HOUR)}


def test_missing_key_fails_before_network(
    monitor: tuple[MagicMock, MagicMock, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment without an API token reports an actionable configuration error."""
    monkeypatch.delenv("TENDERLY_API_KEY")
    with pytest.raises(ValueError, match="Set TENDERLY_API_KEY"):
        usage.check_usage()
    monitor[0].assert_not_called()


@pytest.mark.parametrize(
    "error", [requests.Timeout("timeout"), requests.HTTPError("Forbidden"), ValueError("Bad JSON")]
)
def test_fetch_failure_does_not_mark_hour_delivered(
    monitor: tuple[MagicMock, MagicMock, dict[str, str]], error: Exception
) -> None:
    """Transport, authentication, and malformed JSON failures remain observable."""
    request, send, state = monitor
    request.side_effect = error
    with pytest.raises(type(error)):
        usage.check_usage()
    send.assert_not_called()
    assert state == {"test-account": str(LATEST_HOUR)}


def test_non_object_response_fails(monitor: tuple[MagicMock, MagicMock, dict[str, str]]) -> None:
    """An unexpected JSON document must not be interpreted as normal usage."""
    monitor[0].return_value.json.return_value = []
    with pytest.raises(ValueError, match="JSON object"):
        usage.check_usage()


def test_dry_run_failure_does_not_send_crash_alert(
    monitor: tuple[MagicMock, MagicMock, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI reports dry-run errors without invoking the scheduled crash sender."""
    from utils.runner import run_with_alert

    monkeypatch.setattr("sys.argv", ["usage.py", "--dry-run"])
    crash = MagicMock()
    monkeypatch.setattr("utils.runner.send_error_message", crash)
    monitor[0].side_effect = requests.Timeout("timeout")
    with pytest.raises(SystemExit) as error:
        run_with_alert(usage.main, usage.PROTOCOL)
    assert error.value.code == 1
    monitor[1].assert_not_called()
    crash.assert_not_called()
    assert not monitor[2]

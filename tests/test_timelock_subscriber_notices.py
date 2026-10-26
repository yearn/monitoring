"""Tests for the subscriber pilot's errors-chat notices."""

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from protocols.timelock.subscriber.inbox import SOURCE_HTTP, SOURCE_WS, EventInbox
from protocols.timelock.subscriber.notices import NoticeConfig, PilotNotices
from protocols.timelock.subscriber.notifier import (
    ALERT_PROTOCOL,
    NOTIFY_LABEL,
    Notifier,
    build_notifier,
    send_to_errors_chat,
)
from protocols.timelock.subscriber.reconcile import ChainReconcileResult, ReconcileConfig, ReconcileReport
from protocols.timelock.subscriber.worker import Worker, WorkerConfig
from protocols.timelock.timelock_alerts import TimelockConfig
from tests.timelock_subscriber_fakes import TIMELOCK, FakeEnvio, FakeWSServer, make_event

TIMELOCKS = {(TIMELOCK, 1): TimelockConfig(TIMELOCK, 1, "YEARN_TIMELOCK", "Yearn TimelockController")}
SCOPE = {1: {TIMELOCK}, 8453: {TIMELOCK}}


class RecordingNotifier(Notifier):
    """Notifier whose queue is inspected directly instead of being sent."""

    def __init__(self) -> None:
        super().__init__(send=lambda text: None)
        self.posted: list[str] = []

    def post(self, text: str) -> None:
        self.posted.append(text)


@pytest.fixture
def inbox(tmp_path: Path) -> EventInbox:
    return EventInbox(str(tmp_path / "sub.db"))


@pytest.fixture
def notices(inbox: EventInbox) -> PilotNotices:
    # Baseline at t=0: every fixture event (2023 block timestamps) is after it.
    return PilotNotices(RecordingNotifier(), inbox, TIMELOCKS, NoticeConfig(max_event_notices=3), now=lambda: 0.0)


def _posted(notices: PilotNotices) -> list[str]:
    notifier = notices.notifier
    assert isinstance(notifier, RecordingNotifier)
    return notifier.posted


# ── notifier ───────────────────────────────────────────────────────────


def test_notifier_coalesces_queued_notices_and_survives_send_failures() -> None:
    async def scenario() -> list[str]:
        sent: list[str] = []
        calls = {"n": 0}

        def send(text: str) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("telegram down")
            sent.append(text)

        notifier = Notifier(send=send, min_interval=0)
        notifier.post("lost")  # first send fails and is dropped
        task = asyncio.create_task(notifier.run())
        await asyncio.sleep(0.05)
        for i in range(3):
            notifier.post(f"n{i}")
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert notifier.failed == 1
        return sent

    assert asyncio.run(scenario()) == ["n0\n\nn1\n\nn2"]


def test_notifier_splits_batches_over_budget_and_drops_when_full() -> None:
    async def scenario() -> tuple[list[str], int]:
        sent: list[str] = []
        notifier = Notifier(send=sent.append, max_queue=3, min_interval=0)
        big = "x" * 1500
        for _ in range(4):  # the 4th does not fit the queue
            notifier.post(big)
        task = asyncio.create_task(notifier.run())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return sent, notifier.dropped

    sent, dropped = asyncio.run(scenario())
    assert dropped == 1
    assert [len(t) for t in sent] == [1500, 1500, 1500]  # 2 x 1500 + separator exceeds the budget


def test_build_notifier_requires_errors_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build_notifier("none") is None
    assert build_notifier("errors") is None  # conftest strips TELEGRAM_*: no errors chat
    monkeypatch.setenv("TELEGRAM_CHAT_ID_ERRORS", "-100123")
    assert isinstance(build_notifier("errors"), Notifier)
    with pytest.raises(ValueError, match="notify mode"):
        build_notifier("public")


def test_send_to_errors_chat_is_labelled_and_internal() -> None:
    with patch("protocols.timelock.subscriber.notifier.send_error_message") as send:
        send_to_errors_chat("hello")
    send.assert_called_once_with("hello", NOTIFY_LABEL, source="timelock_subscriber", alert_protocol=ALERT_PROTOCOL)
    assert ALERT_PROTOCOL == "yearn-internal"


# ── notice policy ──────────────────────────────────────────────────────


def test_new_event_notice_has_full_address_links_and_transport(notices: PilotNotices, inbox: EventInbox) -> None:
    event = make_event(1, 26_249_234, 585, ts=1_700_000_000, eventName="CallScheduled")
    inbox.observe([event], SOURCE_WS, 1_700_000_004.2)
    asyncio.run(notices.new_events(SOURCE_WS, ["1_26249234_585"]))
    (text,) = _posted(notices)
    assert "via WS, 4s after its block" in text
    assert "Yearn TimelockController (YEARN_TIMELOCK) on Mainnet: CallScheduled 1_26249234_585" in text
    assert f"Timelock: {TIMELOCK} https://etherscan.io/address/{TIMELOCK}" in text
    assert f"https://etherscan.io/tx/{event['transactionHash']}" in text
    assert f"Operation ID: {event['operationId']}" in text


def test_event_burst_becomes_one_summary_per_chain(notices: PilotNotices) -> None:
    ids = [f"1_{b}_0" for b in range(5)] + ["8453_7_0"]
    notices.inbox.observe([*(make_event(1, b) for b in range(5)), make_event(8453, 7)], SOURCE_HTTP, 1.0)
    asyncio.run(notices.new_events(SOURCE_HTTP, ids))
    assert _posted(notices) == [
        "🛰️ 5 new timelock events on Mainnet via HTTP: 1_0_0, 1_1_0, 1_2_0, 1_3_0, 1_4_0",
        "🛰️ 1 new timelock events on Base via HTTP: 8453_7_0",
    ]


def test_reconcile_notices_skip_initialization_and_flag_ws_misses(notices: PilotNotices, inbox: EventInbox) -> None:
    inbox.observe([make_event(1, 10)], SOURCE_HTTP, 1_700_000_100.0)
    report = ReconcileReport(
        started_at=0,
        chains=[
            ChainReconcileResult(chain_id=8453, initialized=True, new=50, new_ids=["8453_1_0"]),
            ChainReconcileResult(chain_id=1, new=1, new_ids=["1_10_0"], changed=1, removed=2),
        ],
    )
    asyncio.run(notices.reconcile(report, ws_expected=True))
    posted = _posted(notices)
    assert len(posted) == 3
    assert posted[0].startswith("🛰️ New timelock event via HTTP")
    assert posted[1].startswith("⚠️ WS miss: HTTP reconciliation found 1 event(s) on Mainnet")
    assert posted[2].startswith("🔀 Indexed timelock events changed on Mainnet (seen via HTTP): 1 changed, 2 removed")


def test_reconcile_failure_streak_is_announced_once_and_recovery_after(notices: PilotNotices) -> None:
    failed = ReconcileReport(started_at=0, error="GraphQL request failed: 500")
    ok = ReconcileReport(started_at=0)
    for _ in range(4):
        asyncio.run(notices.reconcile(failed, ws_expected=False))
    asyncio.run(notices.reconcile(ok, ws_expected=False))
    asyncio.run(notices.reconcile(ok, ws_expected=False))
    assert _posted(notices) == [
        "❌ HTTP reconciliation failed 3 cycles in a row: GraphQL request failed: 500",
        "✅ HTTP reconciliation recovered after 4 failed cycles",
    ]


def test_short_failure_streak_stays_quiet(notices: PilotNotices) -> None:
    asyncio.run(notices.reconcile(ReconcileReport(started_at=0, error="blip"), ws_expected=False))
    asyncio.run(notices.reconcile(ReconcileReport(started_at=0), ws_expected=False))
    assert _posted(notices) == []


def test_ws_outage_announced_after_threshold_then_recovery(notices: PilotNotices) -> None:
    notices.ws_disconnected(1_000.0, "ConnectionClosedError: boom")
    notices.check_ws(1_299.0)
    assert _posted(notices) == []
    notices.check_ws(1_300.0)
    notices.check_ws(1_400.0)
    notices.ws_disconnected(1_410.0, "InvalidStatus: 502")  # still the same outage
    notices.ws_connected(1_500.0)
    assert _posted(notices) == [
        "🔌 WebSocket down for 5m; last error: ConnectionClosedError: boom",
        "✅ WebSocket reconnected after 8m",
    ]


def test_brief_ws_blip_is_not_announced(notices: PilotNotices) -> None:
    notices.ws_disconnected(1_000.0, "blip")
    notices.check_ws(1_100.0)
    notices.ws_connected(1_120.0)
    notices.check_ws(2_000.0)
    assert _posted(notices) == []


def test_stall_transitions(notices: PilotNotices) -> None:
    notices.stall_changed(8453, True, 52_947_197, 960.0)
    notices.stall_changed(8453, False, 52_947_300, 0.0)
    assert _posted(notices) == [
        "🐢 Envio head for Base stuck at block 52947197 for 16m",
        "✅ Envio head for Base advancing again",
    ]


def test_daily_summary_is_scheduled_and_survives_restart(inbox: EventInbox) -> None:
    first = PilotNotices(RecordingNotifier(), inbox, TIMELOCKS, NoticeConfig(summary_interval=100.0))
    asyncio.run(first.maybe_daily_summary(1_000.0))  # schedules only
    asyncio.run(first.maybe_daily_summary(1_050.0))
    assert _posted(first) == []

    restarted = PilotNotices(RecordingNotifier(), inbox, TIMELOCKS, NoticeConfig(summary_interval=100.0))
    asyncio.run(restarted.maybe_daily_summary(1_100.0))
    asyncio.run(restarted.maybe_daily_summary(1_150.0))
    (summary,) = _posted(restarted)
    assert summary.startswith("📊 Timelock subscriber, last 24h")
    assert "Recommendation: undecided" in summary


# ── worker end to end ──────────────────────────────────────────────────


def test_worker_posts_each_event_once_and_ws_outage_notices(tmp_path: Path) -> None:
    async def scenario() -> list[str]:
        envio = FakeEnvio()
        envio.set_head(1, 1)
        envio.set_head(8453, 1)
        server = FakeWSServer(envio)
        await server.start()
        sent: list[str] = []
        inbox = EventInbox(str(tmp_path / "sub.db"))
        notices = PilotNotices(
            Notifier(send=sent.append, min_interval=0), inbox, TIMELOCKS, NoticeConfig(ws_down_seconds=0.2)
        )
        config = WorkerConfig(
            # One timed reconcile at startup only, so the WebSocket always sees the event first.
            reconcile_interval=60,
            heartbeat_interval=0.05,
            backoff_initial=0.05,
            backoff_max=0.1,
            reconcile=ReconcileConfig(overlap_blocks={1: 5, 8453: 5}),
        )
        worker = Worker(envio.post, server.url, inbox, SCOPE, config, notices=notices)

        async def until(predicate, timeout: float = 5.0) -> None:  # noqa: ANN001
            deadline = time.monotonic() + timeout
            while not predicate():
                assert time.monotonic() < deadline, "condition not met in time"
                await asyncio.sleep(0.02)

        task = asyncio.create_task(worker.run())
        try:
            await until(lambda: worker.ws_connected and worker.last_reconcile_at is not None)
            envio.add(make_event(1, 10, ts=int(time.time())))
            await until(lambda: any("New timelock event" in t for t in sent))
            # A later snapshot repeating the event must not post it again.
            envio.add(make_event(8453, 5, ts=int(time.time()), address="0x" + "99" * 20))
            await asyncio.sleep(0.2)

            server.accepting = False
            await server.drop_connections()
            await until(lambda: any("WebSocket down" in t for t in sent))
            server.accepting = True
            await until(lambda: any("WebSocket reconnected" in t for t in sent))
        finally:
            worker.stop()
            await asyncio.wait_for(task, 5)
            await server.stop()
        return sent

    sent = asyncio.run(scenario())
    text = "\n\n".join(sent)
    assert text.count("New timelock event") == 1
    assert "via WS" in text and "WS miss" not in text
    assert text.count("WebSocket down") == 1 and text.count("WebSocket reconnected") == 1


# ── startup baseline ───────────────────────────────────────────────────


def test_events_before_the_baseline_are_never_announced(inbox: EventInbox) -> None:
    notices = PilotNotices(RecordingNotifier(), inbox, TIMELOCKS, now=lambda: 1_700_000_000.0)
    inbox.observe(
        [make_event(1, 10, ts=1_699_000_000), make_event(1, 11, ts=1_700_000_005)], SOURCE_WS, 1_700_000_010.0
    )
    asyncio.run(notices.new_events(SOURCE_WS, ["1_10_0", "1_11_0"]))
    (text,) = _posted(notices)
    assert "1_11_0" in text and "1_10_0" not in text

    report = ReconcileReport(started_at=0, chains=[ChainReconcileResult(chain_id=1, new=1, new_ids=["1_10_0"])])
    asyncio.run(notices.reconcile(report, ws_expected=True))
    assert len(_posted(notices)) == 1  # no notice and no WS miss for a historical event


def test_baseline_is_persisted_across_restarts(inbox: EventInbox) -> None:
    first = PilotNotices(RecordingNotifier(), inbox, TIMELOCKS, now=lambda: 1_000.0)
    assert asyncio.run(first.start()) == 1_000.0
    restarted = PilotNotices(RecordingNotifier(), inbox, TIMELOCKS, now=lambda: 9_000.0)
    assert asyncio.run(restarted.start()) == 1_000.0


def test_fresh_pilot_stays_quiet_about_history_and_restart_announces_downtime_events(tmp_path: Path) -> None:
    async def run_for(envio: FakeEnvio, server: FakeWSServer, path: str, sent: list[str], body) -> None:  # noqa: ANN001
        inbox = EventInbox(path)
        notices = PilotNotices(Notifier(send=sent.append, min_interval=0), inbox, TIMELOCKS)
        config = WorkerConfig(reconcile_interval=60, heartbeat_interval=0.05, reconcile=ReconcileConfig())
        worker = Worker(envio.post, server.url, inbox, SCOPE, config, notices=notices)
        task = asyncio.create_task(worker.run())
        try:
            deadline = time.monotonic() + 5
            while not (worker.ws_messages and worker.last_reconcile_at is not None):
                assert time.monotonic() < deadline
                await asyncio.sleep(0.02)
            await body()
            await asyncio.sleep(0.2)
        finally:
            worker.stop()
            await asyncio.wait_for(task, 5)

    async def scenario() -> tuple[list[str], list[str]]:
        path = str(tmp_path / "sub.db")
        envio = FakeEnvio()
        envio.add(make_event(1, 100, ts=int(time.time()) - 7 * 86_400))  # week-old, already indexed
        envio.set_head(8453, 1)
        server = FakeWSServer(envio)
        await server.start()
        fresh: list[str] = []
        restarted: list[str] = []
        try:

            async def nothing() -> None:
                return None

            await run_for(envio, server, path, fresh, nothing)
            envio.add(make_event(1, 101, ts=int(time.time())))  # lands while the worker is down
            await run_for(envio, server, path, restarted, nothing)
        finally:
            await server.stop()
        return fresh, restarted

    fresh, restarted = asyncio.run(scenario())
    assert fresh == []
    assert len(restarted) == 1 and "1_101_0" in restarted[0] and "1_100_0" not in restarted[0]

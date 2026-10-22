"""End-to-end tests for the continuous timelock subscriber against a fake Envio.

These drive the real worker, the real graphql-transport-ws client, and the real
SQLite inbox; only Envio is faked (an in-process HTTP stand-in and a local
WebSocket server). They cover controlled disconnects, worker restarts, and
backlog recovery, and assert every expected event lands exactly once.
"""

import asyncio
import json
import socket
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from websockets.asyncio.server import serve
from websockets.typing import Subprotocol

from protocols.timelock.subscriber.inbox import SOURCE_HTTP, SOURCE_WS, EventInbox
from protocols.timelock.subscriber.reconcile import ReconcileConfig, reconcile_all
from protocols.timelock.subscriber.report import PilotReport, _recommend, build_report, format_report, percentile
from protocols.timelock.subscriber.worker import Worker, WorkerConfig, backoff_delay
from tests.timelock_subscriber_fakes import TIMELOCK, FakeEnvio, FakeWSServer, make_event
from utils.graphql_ws import GraphQLWSError, http_to_ws_url, subscribe
from utils.sd_notify import notify

SCOPE = {1: {TIMELOCK}, 8453: {TIMELOCK}}


def _config(transport: str = "both", reconcile_interval: float = 0.2) -> WorkerConfig:
    return WorkerConfig(
        transport=transport,
        reconcile_interval=reconcile_interval,
        heartbeat_interval=0.05,
        backoff_initial=0.05,
        backoff_max=0.2,
        reconcile=ReconcileConfig(page_size=20, overlap_blocks={1: 5, 8453: 5}, max_block_span={1: 50, 8453: 50}),
    )


async def _until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def _rows(inbox: EventInbox) -> dict[str, dict]:
    return {row["event_id"]: dict(row) for row in inbox.events_since(0)}


async def _run_worker(worker: Worker, body: Callable[[], Awaitable[None]]) -> None:
    task = asyncio.create_task(worker.run())
    try:
        await body()
    finally:
        worker.stop()
        await asyncio.wait_for(task, 5)


def _now_event(chain_id: int, block: int, log_index: int = 0) -> dict:
    return make_event(chain_id, block, log_index, ts=int(time.time()))


def _seed_heads(envio: FakeEnvio) -> None:
    envio.set_head(1, 1)
    envio.set_head(8453, 1)


# ── graphql-transport-ws client ────────────────────────────────────────


def test_http_to_ws_url() -> None:
    assert (
        http_to_ws_url("https://indexer.hyperindex.xyz/abc/v1/graphql") == "wss://indexer.hyperindex.xyz/abc/v1/graphql"
    )
    assert http_to_ws_url("http://localhost:8080/v1/graphql") == "ws://localhost:8080/v1/graphql"


def test_client_handshake_ping_and_snapshot() -> None:
    async def scenario() -> None:
        envio = FakeEnvio()
        envio.add(make_event(1, 5))
        server = FakeWSServer(envio, ping_first=True)
        await server.start()
        try:
            async for payload in subscribe(server.url, "subscription { x }", {}):
                assert [r["id"] for r in payload["data"]["TimelockEvent"]] == ["1_5_0"]
                break
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_client_raises_on_subscription_error() -> None:
    async def handler(conn) -> None:  # noqa: ANN001
        await conn.recv()
        await conn.send(json.dumps({"type": "connection_ack"}))
        sub = json.loads(await conn.recv())
        await conn.send(json.dumps({"id": sub["id"], "type": "error", "payload": [{"message": "bad field"}]}))
        await conn.wait_closed()

    async def scenario() -> None:
        async with serve(handler, "127.0.0.1", 0, subprotocols=[Subprotocol("graphql-transport-ws")]) as server:
            port = list(server.sockets)[0].getsockname()[1]
            with pytest.raises(GraphQLWSError, match="bad field"):
                async for _ in subscribe(f"ws://127.0.0.1:{port}", "subscription { x }"):
                    pass

    asyncio.run(scenario())


def test_client_rejects_missing_ack() -> None:
    async def handler(conn) -> None:  # noqa: ANN001
        await conn.recv()
        await conn.send(json.dumps({"type": "connection_error"}))
        await conn.wait_closed()

    async def scenario() -> None:
        async with serve(handler, "127.0.0.1", 0, subprotocols=[Subprotocol("graphql-transport-ws")]) as server:
            port = list(server.sockets)[0].getsockname()[1]
            with pytest.raises(GraphQLWSError, match="connection_ack"):
                async for _ in subscribe(f"ws://127.0.0.1:{port}", "subscription { x }"):
                    pass

    asyncio.run(scenario())


# ── worker end to end ──────────────────────────────────────────────────


def test_ws_delivers_first_and_http_confirms(tmp_path: Path) -> None:
    async def scenario() -> None:
        envio = FakeEnvio()
        _seed_heads(envio)
        server = FakeWSServer(envio)
        await server.start()
        inbox = EventInbox(str(tmp_path / "sub.db"))
        # The timed reconcile runs once at startup and then not again during the test,
        # so the subscription sees the events first; HTTP confirms them on demand.
        config = _config(reconcile_interval=60)
        worker = Worker(envio.post, server.url, inbox, SCOPE, config)

        async def body() -> None:
            await _until(lambda: worker.ws_connected and worker.last_reconcile_at is not None)
            envio.add(_now_event(1, 10), _now_event(8453, 20))
            await _until(lambda: len(_rows(inbox)) == 2)
            report = await asyncio.to_thread(reconcile_all, envio.post, inbox, SCOPE, config.reconcile)
            assert report.ok

        try:
            await _run_worker(worker, body)
        finally:
            await server.stop()
        rows = _rows(inbox)
        assert {r["first_source"] for r in rows.values()} == {SOURCE_WS}
        assert all(r["ws_first_seen_at"] <= r["http_first_seen_at"] for r in rows.values())

        report = build_report(inbox, hours=1)
        assert report.events == 2 and report.seen_by_both == 2 and report.ws_coverage == 1.0
        assert report.ws_only == 0 and report.http_only == 0
        assert report.ws_lead.count == 2

    asyncio.run(scenario())


def test_disconnect_backlog_is_recovered_without_duplicates(tmp_path: Path) -> None:
    async def scenario() -> None:
        envio = FakeEnvio()
        _seed_heads(envio)
        # Snapshot holds only 3 rows, so most of an outage backlog is invisible to
        # the WebSocket on reconnect and must come from HTTP reconciliation.
        server = FakeWSServer(envio, limit=3)
        await server.start()
        inbox = EventInbox(str(tmp_path / "sub.db"))
        worker = Worker(envio.post, server.url, inbox, SCOPE, _config(reconcile_interval=0.3))

        async def body() -> None:
            await _until(lambda: worker.ws_connected)
            envio.add(_now_event(1, 5))
            await _until(lambda: "1_5_0" in _rows(inbox))

            server.accepting = False
            await server.drop_connections()
            await _until(lambda: not worker.ws_connected)
            backlog = [_now_event(1, b, li) for b in range(6, 31) for li in range(2)] + [
                _now_event(8453, b) for b in range(2, 12)
            ]
            envio.add(*backlog)
            await asyncio.sleep(0.3)
            server.accepting = True
            await _until(lambda: worker.ws_connects >= 2)
            await _until(lambda: len(_rows(inbox)) == 1 + len(backlog))
            await _until(lambda: all(r["http_first_seen_at"] for r in _rows(inbox).values()))

        try:
            await _run_worker(worker, body)
        finally:
            await server.stop()

        rows = _rows(inbox)
        assert len(rows) == 61  # every event exactly once (event_id is the primary key)
        assert all(r["status"] == "active" and r["revision"] == 0 for r in rows.values())
        assert any(r["first_source"] == SOURCE_HTTP for r in rows.values())
        disconnects = inbox.log_entries(0, ("ws_disconnected",))
        assert disconnects and "test disconnect" in json.loads(disconnects[0]["details_json"])["error"]

        report = build_report(inbox, hours=1)
        assert report.ws_disconnects >= 1 and report.outages
        assert sum(o.events_inside for o in report.outages) >= 60
        assert report.http_only > 0
        assert "Recovery" in format_report(report)

    asyncio.run(scenario())


def test_worker_restart_resumes_and_recovers_events_from_downtime(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = str(tmp_path / "sub.db")
        envio = FakeEnvio()
        _seed_heads(envio)
        server = FakeWSServer(envio, limit=2)
        await server.start()
        try:
            first = Worker(envio.post, server.url, EventInbox(path), SCOPE, _config())

            async def before() -> None:
                await _until(lambda: first.ws_connected and first.last_reconcile_at is not None)
                envio.add(_now_event(1, 10))
                await _until(lambda: "1_10_0" in _rows(EventInbox(path)))

            await _run_worker(first, before)

            # Indexed while no worker runs.
            envio.add(*(_now_event(1, b) for b in range(11, 21)), _now_event(8453, 7))

            inbox = EventInbox(path)
            second = Worker(envio.post, server.url, inbox, SCOPE, _config())

            async def after() -> None:
                await _until(lambda: len(_rows(inbox)) == 12)
                envio.add(_now_event(8453, 8))
                await _until(lambda: len(_rows(inbox)) == 13)
                await _until(lambda: all(r["http_first_seen_at"] for r in _rows(inbox).values()))

            await _run_worker(second, after)
        finally:
            await server.stop()

        rows = _rows(inbox)
        assert len(rows) == 13
        assert all(r["revision"] == 0 for r in rows.values())
        kinds = [e["kind"] for e in inbox.log_entries(0, ("worker_started", "worker_stopped"))]
        assert kinds == ["worker_started", "worker_stopped", "worker_started", "worker_stopped"]

    asyncio.run(scenario())


def test_http_only_transport_needs_no_websocket(tmp_path: Path) -> None:
    async def scenario() -> None:
        envio = FakeEnvio()
        _seed_heads(envio)
        inbox = EventInbox(str(tmp_path / "sub.db"))
        worker = Worker(envio.post, None, inbox, SCOPE, _config(transport="http", reconcile_interval=0.05))

        async def body() -> None:
            await _until(lambda: worker.last_reconcile_at is not None)
            envio.add(_now_event(1, 3))
            await _until(lambda: "1_3_0" in _rows(inbox))
            await _until(lambda: "heartbeat" in inbox.status())

        await _run_worker(worker, body)
        assert _rows(inbox)["1_3_0"]["first_source"] == SOURCE_HTTP

    asyncio.run(scenario())


def test_health_reports_stalled_chain(tmp_path: Path) -> None:
    clock = [1_000.0]
    envio = FakeEnvio()
    _seed_heads(envio)
    inbox = EventInbox(str(tmp_path / "sub.db"))
    config = WorkerConfig(transport="http", head_stall_seconds=900)
    worker = Worker(envio.post, None, inbox, SCOPE, config, now=lambda: clock[0])

    reconcile_all(envio.post, inbox, SCOPE, config.reconcile, now=lambda: clock[0])
    clock[0] += 1_000
    envio.set_head(1, 50)  # Mainnet advances, Base stays put
    reconcile_all(envio.post, inbox, SCOPE, config.reconcile, now=lambda: clock[0])

    health = worker.health()
    assert {c["chain_id"]: c["stalled"] for c in health["chains"]} == {1: False, 8453: True}
    assert {c["chain_id"]: c["head_age_seconds"] for c in health["chains"]} == {1: 0.0, 8453: 1_000.0}


def test_worker_rejects_bad_transport_config(tmp_path: Path) -> None:
    inbox = EventInbox(str(tmp_path / "sub.db"))
    with pytest.raises(ValueError, match="transport"):
        Worker(FakeEnvio().post, "ws://x", inbox, SCOPE, WorkerConfig(transport="carrier-pigeon"))
    with pytest.raises(ValueError, match="WebSocket URL"):
        Worker(FakeEnvio().post, None, inbox, SCOPE, WorkerConfig(transport="both"))


# ── report and supervision helpers ─────────────────────────────────────


def test_percentile_nearest_rank() -> None:
    assert percentile([], 0.5) is None
    assert percentile([5.0, 1.0, 3.0], 0.5) == 3.0
    assert percentile([float(v) for v in range(1, 21)], 0.95) == 19.0


def test_recommendation_rules() -> None:
    sparse = PilotReport(window_start=0, window_end=1, events=2, seen_by_ws=2)
    assert _recommend(sparse)[0].startswith("undecided")

    good = PilotReport(window_start=0, window_end=1, events=20, seen_by_ws=20, ws_coverage=1.0)
    good.ws_latency.p95, good.http_latency.p95 = 2.0, 45.0
    assert _recommend(good)[0].startswith("both")

    lossy = PilotReport(window_start=0, window_end=1, events=20, seen_by_ws=15, ws_coverage=0.75)
    lossy.ws_latency.p95, lossy.http_latency.p95 = 2.0, 45.0
    assert _recommend(lossy)[0].startswith("http")


def test_report_counts_ws_only_rows_that_http_scanned_but_did_not_return(tmp_path: Path) -> None:
    inbox = EventInbox(str(tmp_path / "sub.db"))
    now = time.time()
    inbox.observe([make_event(1, 10, ts=int(now) - 10)], SOURCE_WS, now)
    inbox.observe([make_event(1, 99, ts=int(now) - 5)], SOURCE_WS, now)
    inbox.set_checkpoint(1, 50, 50, now)
    report = build_report(inbox, hours=1, now=now + 1)
    assert report.ws_only == 1 and report.pending_http == 1 and report.confirmed_by_http == 0


def test_sd_notify_sends_datagram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind(path)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        assert notify("READY=1", "STATUS=ok")
        assert sock.recv(1024) == b"READY=1\nSTATUS=ok"
    monkeypatch.delenv("NOTIFY_SOCKET")
    assert notify("READY=1") is False


def test_backoff_delay_is_capped_without_overflow() -> None:
    assert backoff_delay(1, 1.0, 60.0) == 1.0
    assert backoff_delay(4, 1.0, 60.0) == 8.0
    assert backoff_delay(7, 1.0, 60.0) == 60.0
    # 2 ** 1024 overflows a float; the exponent is capped before the multiply.
    assert backoff_delay(1_025, 1.0, 60.0) == 60.0
    assert backoff_delay(10**9, 1.0, 60.0) == 60.0


def test_reconcile_loop_survives_unexpected_errors(tmp_path: Path) -> None:
    async def scenario() -> None:
        envio = FakeEnvio()
        _seed_heads(envio)
        calls = {"n": 0}

        def flaky_post(query: str, variables: dict) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.IntegrityError("simulated bug")  # not an EnvioQueryError
            return envio.post(query, variables)

        inbox = EventInbox(str(tmp_path / "sub.db"))
        worker = Worker(flaky_post, None, inbox, SCOPE, _config(transport="http", reconcile_interval=0.05))

        async def body() -> None:
            await _until(lambda: worker.last_reconcile is not None and worker.last_reconcile.ok)
            envio.add(_now_event(1, 3))
            await _until(lambda: "1_3_0" in _rows(inbox))
            assert worker.is_alive()

        await _run_worker(worker, body)

    asyncio.run(scenario())


def test_watchdog_deadline_covers_a_hung_first_reconcile(tmp_path: Path) -> None:
    clock = [1_000.0]
    worker = Worker(
        FakeEnvio().post,
        None,
        EventInbox(str(tmp_path / "sub.db")),
        SCOPE,
        _config(transport="http"),
        now=lambda: clock[0],
    )
    worker.started_at = clock[0]  # run() started; the first reconcile never returns
    assert worker.is_alive()
    clock[0] += 3 * worker.config.reconcile_interval + 121
    assert not worker.is_alive()
    worker.last_reconcile_at = clock[0]  # a completed cycle resets the deadline
    assert worker.is_alive()


def test_watchdog_withheld_when_a_loop_task_ended(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker = Worker(FakeEnvio().post, None, EventInbox(str(tmp_path / "sub.db")), SCOPE, _config(transport="http"))
        worker.started_at = worker.now()

        async def crashed() -> None:
            raise RuntimeError("loop died")

        task = asyncio.create_task(crashed())
        await asyncio.gather(task, return_exceptions=True)
        worker._tasks = [task]
        assert not worker.is_alive()

    asyncio.run(scenario())


def test_report_includes_outage_that_began_before_the_window(tmp_path: Path) -> None:
    inbox = EventInbox(str(tmp_path / "sub.db"))
    inbox.log("worker_started", 0.0, transport="both")
    inbox.log("ws_connected", 10.0)
    inbox.log("ws_disconnected", 500.0, error="ConnectionClosedError: boom")
    # Window [1000, 3400]: no connection log entries inside it.
    report = build_report(inbox, hours=2400 / 3600, now=3_400.0)
    assert len(report.outages) == 1
    outage = report.outages[0]
    assert outage.clipped and outage.start == 1_000.0 and outage.end is None
    assert report.ws_downtime_seconds == 2_400.0
    assert "began before the window" in format_report(report)

    # Reconnected inside the window: clipped at the start, closed at the connect.
    inbox.log("ws_connected", 1_600.0)
    report = build_report(inbox, hours=2400 / 3600, now=3_400.0)
    assert [(o.start, o.end, o.clipped) for o in report.outages] == [(1_000.0, 1_600.0, True)]
    assert report.ws_downtime_seconds == 600.0


def test_report_ignores_closed_outage_before_the_window(tmp_path: Path) -> None:
    inbox = EventInbox(str(tmp_path / "sub.db"))
    inbox.log("worker_started", 0.0, transport="both")
    inbox.log("ws_connected", 10.0)
    report = build_report(inbox, hours=1, now=7_200.0)
    assert report.outages == [] and report.ws_downtime_seconds == 0.0

"""Tests for named event streams: Morpho governance alongside timelocks in one subscriber process."""

import argparse
import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from protocols.morpho.config import VAULTS_V1_BY_CHAIN, VAULTS_V2_BY_CHAIN, iter_vaults
from protocols.morpho.governance_stream import (
    MORPHO_GOVERNANCE_STREAM,
    describe_governance_event,
    morpho_governance_scope,
)
from protocols.timelock.event_stream import TIMELOCK_STREAM
from protocols.timelock.event_subscriber import STREAMS, heartbeat_problems, parse_streams
from tests.timelock_subscriber_fakes import (
    MORPHO_VAULT,
    TIMELOCK,
    FakeEnvio,
    FakeWSServer,
    make_event,
    make_morpho_event,
)
from utils.subscriber import envio as envio_module
from utils.subscriber.envio import EnvioQueryError, SharedChainHeads, http_poster, live_subscription, range_query
from utils.subscriber.inbox import SOURCE_HTTP, SOURCE_WS, EventInbox
from utils.subscriber.notices import NoticeConfig, PilotNotices
from utils.subscriber.notifier import Notifier
from utils.subscriber.reconcile import ReconcileConfig, reconcile_all
from utils.subscriber.report import build_report, format_report
from utils.subscriber.worker import Worker, WorkerConfig, run_workers

TIMELOCK_SCOPE = {1: {TIMELOCK}}
MORPHO_SCOPE = {1: {MORPHO_VAULT}}
CONFIG = ReconcileConfig(page_size=20, overlap_blocks={1: 5}, max_block_span={1: 50})


class RecordingNotifier(Notifier):
    """Notifier that keeps posted notices instead of sending them."""

    def __init__(self) -> None:
        super().__init__(send=lambda text: None)
        self.posted: list[str] = []

    def post(self, text: str) -> None:
        self.posted.append(text)


def _inboxes(tmp_path: Path) -> tuple[EventInbox, EventInbox]:
    return EventInbox(TIMELOCK_STREAM, str(tmp_path / "timelock.db")), EventInbox(
        MORPHO_GOVERNANCE_STREAM, str(tmp_path / "morpho.db")
    )


def _ids(inbox: EventInbox) -> set[str]:
    return {row["event_id"] for row in inbox.events_since(0)}


# ── stream definitions ─────────────────────────────────────────────────


def test_morpho_queries_target_the_governance_entity() -> None:
    query = range_query(MORPHO_GOVERNANCE_STREAM, 1, 10, 20, 50, 0, since_ts=None)
    assert "MorphoGovernanceEvent(" in query and "vaultAddress: { _in: $addresses }" in query
    assert "argsJson" in query and "timelockAddress" not in query
    live = live_subscription(MORPHO_GOVERNANCE_STREAM, 50, [8453, 1])
    assert "subscription MorphoGovernanceEventLive" in live and "chainId: { _in: [1, 8453] }" in live


def test_morpho_scope_covers_every_configured_vault_with_a_label() -> None:
    scope = morpho_governance_scope()
    configured = [*iter_vaults(VAULTS_V1_BY_CHAIN), *iter_vaults(VAULTS_V2_BY_CHAIN)]

    assert sum(len(addresses) for addresses in scope.addresses.values()) == len(configured)
    for chain, vault in configured:
        assert vault.address.lower() in scope.addresses[chain.chain_id]
        assert scope.labels[(vault.address.lower(), chain.chain_id)].protocol == "morpho"
    assert scope.labels[(MORPHO_VAULT, 1)].label == "Yearn USDC V1"


def test_stream_names_tables_and_files_are_distinct() -> None:
    assert TIMELOCK_STREAM.table("events") == "timelock_sub_events"  # unchanged for the running pilot
    assert MORPHO_GOVERNANCE_STREAM.table("events") == "morpho_sub_events"
    assert TIMELOCK_STREAM.db_filename != MORPHO_GOVERNANCE_STREAM.db_filename
    assert set(STREAMS) == {"timelock", "morpho"}


def test_describe_governance_event_shows_args_and_survives_bad_json() -> None:
    lines = describe_governance_event(make_morpho_event(1, 10, vaultVersion=2, argsJson='{"b":"2","a":true}'))
    assert lines == ["Vault V2, sent by 0x" + "cd" * 20, 'Args: {"a":true,"b":"2"}']

    long = describe_governance_event(make_morpho_event(1, 10, argsJson=json.dumps({"data": "0x" + "ab" * 1000})))
    assert long[1].endswith("…") and len(long[1]) < 700
    assert describe_governance_event(make_morpho_event(1, 10, argsJson="not json"))[1] == "Args: not json"


# ── isolated inboxes and shared chain heads ────────────────────────────


def test_streams_reconcile_into_separate_inboxes_with_one_metadata_read(tmp_path: Path) -> None:
    envio = FakeEnvio()
    envio.set_head(1, 1)
    timelock_inbox, morpho_inbox = _inboxes(tmp_path)
    heads = SharedChainHeads(envio.post)
    assert reconcile_all(envio.post, timelock_inbox, TIMELOCK_SCOPE, CONFIG, heads=heads).ok
    assert reconcile_all(envio.post, morpho_inbox, MORPHO_SCOPE, CONFIG, heads=heads).ok

    envio.add(make_event(1, 10), make_morpho_event(1, 11), make_morpho_event(1, 12, address="0x" + "99" * 20))
    shared = SharedChainHeads(envio.post)
    reads = envio.metadata_reads
    timelock = reconcile_all(envio.post, timelock_inbox, TIMELOCK_SCOPE, CONFIG, heads=shared)
    morpho = reconcile_all(envio.post, morpho_inbox, MORPHO_SCOPE, CONFIG, heads=shared)

    assert envio.metadata_reads == reads + 1  # two streams, one chain_metadata read
    assert (timelock.chains[0].new, morpho.chains[0].new) == (1, 1)
    assert _ids(timelock_inbox) == {"1_10_0"}
    assert _ids(morpho_inbox) == {"1_11_0"}  # the unmonitored vault stays out
    (row,) = morpho_inbox.events_since(0)
    assert row["contract_address"] == MORPHO_VAULT and row["operation_id"] is None
    with sqlite3.connect(morpho_inbox.path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "morpho_sub_events" in tables and not any(t.startswith("timelock_sub") for t in tables)


def test_streams_keep_independent_checkpoints(tmp_path: Path) -> None:
    envio = FakeEnvio()
    envio.set_head(1, 1)
    timelock_inbox, morpho_inbox = _inboxes(tmp_path)
    reconcile_all(envio.post, timelock_inbox, TIMELOCK_SCOPE, CONFIG)
    reconcile_all(envio.post, morpho_inbox, MORPHO_SCOPE, CONFIG)
    envio.add(make_morpho_event(1, 30))
    envio.fail_chain[1] = 1  # the next range query (timelock's) fails

    assert not reconcile_all(envio.post, timelock_inbox, TIMELOCK_SCOPE, CONFIG).ok
    assert reconcile_all(envio.post, morpho_inbox, MORPHO_SCOPE, CONFIG).ok

    timelock_checkpoint, morpho_checkpoint = timelock_inbox.get_checkpoint(1), morpho_inbox.get_checkpoint(1)
    assert timelock_checkpoint is not None and morpho_checkpoint is not None
    assert (timelock_checkpoint.block_number, morpho_checkpoint.block_number) == (1, 30)


def test_shared_chain_heads_refetch_when_stale_and_retry_after_errors() -> None:
    envio = FakeEnvio()
    envio.set_head(1, 5)
    clock = [0.0]
    heads = SharedChainHeads(envio.post, max_age=10.0, clock=lambda: clock[0])

    assert heads()[1].latest_processed_block == 5
    envio.set_head(1, 6)
    clock[0] = 9.0
    assert heads()[1].latest_processed_block == 5  # still fresh
    clock[0] = 20.0
    envio.fail_metadata = 1
    with pytest.raises(EnvioQueryError):
        heads()
    assert heads()[1].latest_processed_block == 6
    assert envio.metadata_reads == 3


def test_http_poster_keeps_one_session_per_thread() -> None:
    sessions: list[object] = []

    class Response:
        def json(self) -> dict:
            return {"data": {}}

    def fake_request(*_args: object, session: object, **_kwargs: object) -> Response:
        sessions.append(session)
        return Response()

    with patch.object(envio_module, "request_with_retry", side_effect=fake_request):
        post = http_poster("https://envio.example/v1/graphql")
        post("{ a }", {})
        post("{ b }", {})
        thread = threading.Thread(target=post, args=("{ c }", {}))
        thread.start()
        thread.join()

    assert sessions[0] is sessions[1]
    assert sessions[2] is not sessions[0]


# ── notices and reports ────────────────────────────────────────────────


def test_morpho_notice_names_the_vault_and_its_arguments(tmp_path: Path) -> None:
    _, inbox = _inboxes(tmp_path)
    notifier = RecordingNotifier()
    notices = PilotNotices(notifier, inbox, morpho_governance_scope().labels, NoticeConfig(), now=lambda: 0.0)
    inbox.observe([make_morpho_event(1, 26_000_000, 7)], SOURCE_WS, 1_700_000_005.0)

    asyncio.run(notices.new_events(SOURCE_WS, ["1_26000000_7"]))

    (text,) = notifier.posted
    assert text.startswith("🛰️ New Morpho governance event via WS, 5s after its block")
    assert "Yearn USDC V1 (morpho) on Mainnet: SubmitMarketRemoval 1_26000000_7" in text
    assert f"Vault: {MORPHO_VAULT} https://etherscan.io/address/{MORPHO_VAULT}" in text
    assert "Args: {" in text and "Operation ID" not in text


def test_report_is_titled_by_stream(tmp_path: Path) -> None:
    timelock_inbox, morpho_inbox = _inboxes(tmp_path)
    assert format_report(build_report(timelock_inbox, 1)).startswith("Timelock subscriber pilot report")
    assert format_report(build_report(morpho_inbox, 1)).startswith("Morpho governance subscriber pilot report")


# ── one process, several streams ───────────────────────────────────────


def _worker_config() -> WorkerConfig:
    return WorkerConfig(
        reconcile_interval=0.2,
        heartbeat_interval=0.05,
        backoff_initial=0.05,
        backoff_max=0.2,
        reconcile=CONFIG,
    )


async def _until(predicate, timeout: float = 5.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def test_run_workers_follows_both_streams_over_websocket(tmp_path: Path) -> None:
    notified: list[tuple[str, ...]] = []

    async def scenario() -> None:
        envio = FakeEnvio()
        envio.set_head(1, 1)
        server = FakeWSServer(envio)
        await server.start()
        timelock_inbox, morpho_inbox = _inboxes(tmp_path)
        heads = SharedChainHeads(envio.post)
        workers = [
            Worker(envio.post, server.url, timelock_inbox, TIMELOCK_SCOPE, _worker_config(), heads=heads),
            Worker(envio.post, server.url, morpho_inbox, MORPHO_SCOPE, _worker_config(), heads=heads),
        ]
        task = asyncio.create_task(run_workers(workers, watchdog_interval=0.05))
        try:
            await _until(lambda: all(w.ws_connected and w.last_reconcile_at is not None for w in workers))
            now = int(time.time())
            envio.add(make_event(1, 10, ts=now), make_morpho_event(1, 11, ts=now))
            await _until(lambda: _ids(timelock_inbox) == {"1_10_0"} and _ids(morpho_inbox) == {"1_11_0"})
            await asyncio.sleep(0.1)  # let the supervisor ping the watchdog
        finally:
            workers[0].stop()  # stopping one stream stops the process
            await asyncio.wait_for(task, 5)
            await server.stop()

        assert server.subscriptions >= 2  # one connection per stream
        assert {row["first_source"] for row in timelock_inbox.events_since(0)} == {SOURCE_WS}
        assert {row["first_source"] for row in morpho_inbox.events_since(0)} == {SOURCE_WS}
        for inbox, name in ((timelock_inbox, "timelock"), (morpho_inbox, "morpho")):
            heartbeat = inbox.status()["heartbeat"][0]
            assert heartbeat["stream"] == name

    with patch("utils.subscriber.worker.notify", side_effect=lambda *a: notified.append(a)):
        asyncio.run(scenario())

    kinds = [message[0] for message in notified]
    assert kinds.count("READY=1") == 1 and kinds.count("STOPPING=1") == 1
    assert "WATCHDOG=1" in kinds
    watchdog_status = next(m[1] for m in notified if m[0] == "WATCHDOG=1")
    assert "timelock:ws=" in watchdog_status and "morpho:ws=" in watchdog_status


def test_run_workers_withholds_the_watchdog_while_any_stream_is_dead(tmp_path: Path) -> None:
    notified: list[tuple[str, ...]] = []

    async def scenario() -> None:
        envio = FakeEnvio()
        envio.set_head(1, 1)
        timelock_inbox, morpho_inbox = _inboxes(tmp_path)
        config = WorkerConfig(transport="http", reconcile_interval=0.1, heartbeat_interval=0.05, reconcile=CONFIG)
        workers = [
            Worker(envio.post, None, timelock_inbox, TIMELOCK_SCOPE, config),
            Worker(envio.post, None, morpho_inbox, MORPHO_SCOPE, config),
        ]
        task = asyncio.create_task(run_workers(workers, watchdog_interval=0.02))
        try:
            await _until(lambda: all(w.last_reconcile_at is not None for w in workers))
            with patch.object(workers[1], "is_alive", return_value=False):
                notified.clear()
                await asyncio.sleep(0.15)
                assert all(m[0] != "WATCHDOG=1" for m in notified)
        finally:
            workers[0].stop()
            await asyncio.wait_for(task, 5)

    with patch("utils.subscriber.worker.notify", side_effect=lambda *a: notified.append(a)):
        asyncio.run(scenario())


def test_run_workers_surfaces_a_crashed_stream(tmp_path: Path) -> None:
    async def scenario() -> None:
        envio = FakeEnvio()
        timelock_inbox, morpho_inbox = _inboxes(tmp_path)
        config = WorkerConfig(transport="http", reconcile_interval=60, heartbeat_interval=0.05, reconcile=CONFIG)
        healthy = Worker(envio.post, None, timelock_inbox, TIMELOCK_SCOPE, config)
        broken = Worker(envio.post, None, morpho_inbox, MORPHO_SCOPE, config)

        async def crash() -> None:
            raise RuntimeError("boom")

        with patch.object(broken, "run", crash):
            await asyncio.wait_for(run_workers([healthy, broken]), 5)

    with patch("utils.subscriber.worker.notify"), pytest.raises(RuntimeError, match="boom"):
        asyncio.run(scenario())


def test_run_workers_withholds_watchdog_after_heartbeat_write_fails(tmp_path: Path) -> None:
    """A SQLite heartbeat failure must stop pings while reconciliation continues."""
    notified: list[tuple[str, ...]] = []

    async def scenario() -> None:
        envio = FakeEnvio()
        envio.set_head(1, 1)
        inbox, _ = _inboxes(tmp_path)
        config = WorkerConfig(transport="http", reconcile_interval=0.05, heartbeat_interval=0.02, reconcile=CONFIG)
        worker = Worker(envio.post, None, inbox, TIMELOCK_SCOPE, config)
        task = asyncio.create_task(run_workers([worker], watchdog_interval=0.01))
        try:
            await _until(lambda: "heartbeat" in inbox.status() and any(m[0] == "WATCHDOG=1" for m in notified))
            with patch.object(inbox, "set_status", side_effect=sqlite3.OperationalError("database is locked")) as write:
                await _until(lambda: write.called)
                await _until(lambda: not worker.is_alive())
                last_reconcile = worker.last_reconcile_at
                notified.clear()
                await _until(lambda: worker.last_reconcile_at != last_reconcile)
                await asyncio.sleep(0.05)
                assert not any(m[0] == "WATCHDOG=1" for m in notified)
        finally:
            worker.stop()
            await asyncio.wait_for(task, 5)

    with patch("utils.subscriber.worker.notify", side_effect=lambda *a: notified.append(a)):
        asyncio.run(scenario())


@pytest.mark.parametrize("transport", ["http", "ws"])
@pytest.mark.parametrize("after_first", [False, True], ids=["first-cycle", "later-cycle"])
def test_run_workers_withholds_watchdog_when_heartbeat_hangs(tmp_path: Path, transport: str, after_first: bool) -> None:
    """A pending heartbeat gets a bounded grace period, even with a healthy transport."""
    notified: list[tuple[str, ...]] = []
    blocked = threading.Event()
    release = threading.Event()

    async def scenario() -> None:
        envio = FakeEnvio()
        envio.set_head(1, 1)
        server = FakeWSServer(envio)
        await server.start()
        inbox, _ = _inboxes(tmp_path)
        config = WorkerConfig(transport=transport, reconcile_interval=0.05, heartbeat_interval=0.02, reconcile=CONFIG)
        worker = Worker(envio.post, server.url, inbox, TIMELOCK_SCOPE, config)
        set_status = inbox.set_status
        writes = 0

        def slow_write(key: str, value: dict, at: float) -> None:
            nonlocal writes
            writes += 1
            if after_first and writes == 1:
                clock.return_value = 1_040.0
            if writes > int(after_first):
                blocked.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release the blocked heartbeat write")
            set_status(key, value, at)

        with patch.object(inbox, "set_status", side_effect=slow_write):
            task = asyncio.create_task(run_workers([worker], watchdog_interval=0.01))
            try:
                await _until(blocked.is_set)
                await _until(lambda: any(m[0] == "WATCHDOG=1" for m in notified))
                deadline = 3 * config.heartbeat_interval + 120
                last_progress = 1_040 if after_first else 1_000
                clock.return_value = last_progress + deadline - 1
                assert worker.is_alive()
                clock.return_value = last_progress + deadline
                assert not worker.is_alive()
                with patch.object(worker, "now", return_value=worker.now() - 3_600):
                    assert not worker.is_alive()
                notified.clear()
                last_reconcile = worker.last_reconcile_at
                if transport == "http":
                    await _until(lambda: worker.last_reconcile_at != last_reconcile)
                await asyncio.sleep(0.05)
                assert not any(m[0] == "WATCHDOG=1" for m in notified)
                release.set()
                await _until(lambda: any(m[0] == "WATCHDOG=1" for m in notified))
                assert worker.is_alive()
            finally:
                release.set()
                worker.stop()
                await asyncio.wait_for(task, 5)
                await server.stop()

    with (
        patch("utils.subscriber.worker.notify", side_effect=lambda *a: notified.append(a)),
        patch("utils.subscriber.worker.monotonic", return_value=1_000.0) as clock,
    ):
        asyncio.run(scenario())


def test_run_workers_keeps_pinging_after_notifier_crashes(tmp_path: Path) -> None:
    """Notifier failure must not prevent fresh heartbeats from feeding the watchdog."""
    notified: list[tuple[str, ...]] = []

    async def scenario() -> None:
        envio = FakeEnvio()
        envio.set_head(1, 1)
        inbox, _ = _inboxes(tmp_path)
        notifier = RecordingNotifier()
        notices = PilotNotices(notifier, inbox, {})
        config = WorkerConfig(transport="http", reconcile_interval=0.05, heartbeat_interval=0.02, reconcile=CONFIG)
        worker = Worker(envio.post, None, inbox, TIMELOCK_SCOPE, config, notices=notices)
        crashed = asyncio.Event()

        async def crash() -> None:
            crashed.set()
            raise RuntimeError("notifier failed")

        with patch.object(notifier, "run", side_effect=crash):
            task = asyncio.create_task(run_workers([worker], watchdog_interval=0.01))
            try:
                await asyncio.wait_for(crashed.wait(), 5)
                await _until(lambda: "heartbeat" in inbox.status())
                notified.clear()
                last_heartbeat = inbox.status()["heartbeat"][1]
                await _until(lambda: inbox.status()["heartbeat"][1] != last_heartbeat)
                await _until(lambda: any(m[0] == "WATCHDOG=1" for m in notified))
                assert worker.is_alive()
            finally:
                worker.stop()
                await asyncio.wait_for(task, 5)

    with patch("utils.subscriber.worker.notify", side_effect=lambda *a: notified.append(a)):
        asyncio.run(scenario())


def test_http_observation_of_both_streams_is_recorded_per_source(tmp_path: Path) -> None:
    envio = FakeEnvio()
    envio.set_head(1, 1)
    timelock_inbox, morpho_inbox = _inboxes(tmp_path)
    for inbox, scope in ((timelock_inbox, TIMELOCK_SCOPE), (morpho_inbox, MORPHO_SCOPE)):
        reconcile_all(envio.post, inbox, scope, CONFIG)
    envio.add(make_event(1, 10), make_morpho_event(1, 10, 1))
    for inbox, scope in ((timelock_inbox, TIMELOCK_SCOPE), (morpho_inbox, MORPHO_SCOPE)):
        reconcile_all(envio.post, inbox, scope, CONFIG)

    assert {row["first_source"] for row in morpho_inbox.events_since(0)} == {SOURCE_HTTP}
    assert {q["entity"] for q in envio.queries} == {"TimelockEvent", "MorphoGovernanceEvent"}


# ── CLI ────────────────────────────────────────────────────────────────


def test_parse_streams_keeps_order_and_rejects_unknown_names() -> None:
    assert parse_streams("timelock") == ["timelock"]
    assert parse_streams(" morpho, timelock ,morpho") == ["morpho", "timelock"]
    for bad in ("", "timelock,aave", " , "):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_streams(bad)


def test_heartbeat_problems() -> None:
    now = 1_000.0
    healthy = {
        "heartbeat": (
            {"heartbeat_interval": 30, "last_reconcile_ok": True, "transport": "both", "ws_connected": True},
            now - 10,
        )
    }
    assert heartbeat_problems(healthy, now) == []
    assert heartbeat_problems({}, now) == ["no heartbeat recorded"]
    stale = {
        "heartbeat": (
            {
                "heartbeat_interval": 30,
                "last_reconcile_ok": False,
                "transport": "both",
                "ws_connected": False,
                "chains": [{"chain_id": 999, "stalled": True}],
            },
            now - 200,
        )
    }
    assert heartbeat_problems(stale, now) == [
        "heartbeat is 200s old",
        "last reconcile failed",
        "WebSocket disconnected",
        "chain 999 head stalled",
    ]

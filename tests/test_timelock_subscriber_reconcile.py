"""Inbox and HTTP reconciliation tests for the continuous timelock subscriber."""

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from protocols.timelock.subscriber.envio import EnvioQueryError, fetch_range, range_query
from protocols.timelock.subscriber.inbox import SCHEMA_VERSION, SOURCE_HTTP, SOURCE_WS, EventInbox, event_fingerprint
from protocols.timelock.subscriber.reconcile import ReconcileConfig, build_scope, in_scope, reconcile_all
from tests.timelock_subscriber_fakes import OTHER_TIMELOCK, TIMELOCK, FakeEnvio, make_event

SCOPE = {1: {TIMELOCK}, 8453: {TIMELOCK}}
CONFIG = ReconcileConfig(
    page_size=10,
    max_pages=50,
    overlap_blocks={1: 5, 8453: 5},
    max_block_span={1: 100, 8453: 100},
)


@pytest.fixture
def inbox(tmp_path: Path) -> EventInbox:
    return EventInbox(str(tmp_path / "sub.db"))


def _event_ids(inbox: EventInbox) -> list[str]:
    return [row["event_id"] for row in inbox.events_since(0)]


def _checkpoint(inbox: EventInbox, chain_id: int) -> int:
    cp = inbox.get_checkpoint(chain_id)
    assert cp is not None
    return cp.block_number


def _seed(envio: FakeEnvio, inbox: EventInbox, head: int = 1) -> None:
    """Initialize checkpoints at ``head`` on every chain (no backfill)."""
    for chain_id in SCOPE:
        envio.set_head(chain_id, head)
    report = reconcile_all(envio.post, inbox, SCOPE, CONFIG)
    assert report.ok and all(c.initialized for c in report.chains)


# ── inbox ──────────────────────────────────────────────────────────────


def test_observe_counts_new_duplicate_changed_and_restored(inbox: EventInbox) -> None:
    event = make_event(1, 10)
    assert inbox.observe([event], SOURCE_WS, 100.0).new == 1
    # A repeated WS snapshot and the HTTP confirmation are duplicates, not new rows.
    assert inbox.observe([event], SOURCE_WS, 101.0).duplicate == 1
    assert inbox.observe([event], SOURCE_HTTP, 102.0).duplicate == 1

    replaced = make_event(1, 10, transactionHash="0x" + "11" * 32)
    assert inbox.observe([replaced], SOURCE_HTTP, 103.0).changed == 1
    assert inbox.mark_missing(1, 10, 10, set(), SOURCE_HTTP, 104.0) == ["1_10_0"]
    assert inbox.observe([replaced], SOURCE_HTTP, 105.0).restored == 1

    (row,) = inbox.events_since(0)
    assert row["first_source"] == SOURCE_WS
    assert row["ws_first_seen_at"] == 100.0 and row["http_first_seen_at"] == 102.0
    assert row["ws_observations"] == 2 and row["http_observations"] == 3
    assert row["revision"] == 3 and row["status"] == "active"
    assert [c["kind"] for c in inbox.changes_since(0)] == ["changed", "removed", "restored"]


def test_fingerprint_ignores_address_casing() -> None:
    lower = make_event(1, 10)
    checksummed = make_event(1, 10, timelockAddress="0x88Ba032be87d5EF1fbE87336B7090767F367BF73")
    assert event_fingerprint(lower) == event_fingerprint(checksummed)


def test_checkpoint_never_moves_backwards(inbox: EventInbox) -> None:
    inbox.set_checkpoint(1, 100, 100, now=10.0)
    inbox.set_checkpoint(1, 50, 90, now=20.0)
    cp = inbox.get_checkpoint(1)
    assert cp is not None
    assert (cp.block_number, cp.indexed_head, cp.head_changed_at) == (100, 100, 10.0)
    inbox.set_checkpoint(1, 120, 130, now=30.0)
    cp = inbox.get_checkpoint(1)
    assert cp is not None and (cp.block_number, cp.indexed_head, cp.head_changed_at) == (120, 130, 30.0)


def test_scope_filters_address_per_chain() -> None:
    scope = build_scope([type("T", (), {"address": TIMELOCK.upper().replace("0X", "0x"), "chain_id": 1})()])
    assert in_scope(make_event(1, 1), scope)
    assert not in_scope(make_event(8453, 1), scope)
    assert not in_scope(make_event(1, 1, address=OTHER_TIMELOCK), scope)


def test_range_query_inlines_numbers_and_orders_totally() -> None:
    query = range_query(1, 10, 20, 50, 100, since_ts=None)
    assert "chainId: { _eq: 1 }" in query
    assert "blockNumber: { _gt: 10, _lte: 20 }" in query
    assert "order_by: [{ blockNumber: asc }, { logIndex: asc }, { id: asc }]" in query
    assert "limit: 50" in query and "offset: 100" in query


def test_fetch_range_refuses_unbounded_ranges() -> None:
    envio = FakeEnvio()
    envio.add(*(make_event(1, b) for b in range(1, 31)))
    with pytest.raises(EnvioQueryError, match="exceeds 2 pages"):
        fetch_range(envio.post, 1, {TIMELOCK}, 0, 30, page_size=10, max_pages=2)


# ── reconcile ──────────────────────────────────────────────────────────


def test_first_reconcile_starts_at_head_without_backfill(inbox: EventInbox) -> None:
    envio = FakeEnvio()
    envio.add(make_event(1, 50), make_event(8453, 70))
    report = reconcile_all(envio.post, inbox, SCOPE, CONFIG)
    assert report.ok
    assert _event_ids(inbox) == []
    assert _checkpoint(inbox, 1) == 50
    assert _checkpoint(inbox, 8453) == 70


def test_first_reconcile_backfills_lookback(inbox: EventInbox) -> None:
    envio = FakeEnvio()
    now = 1_700_000_000
    envio.add(make_event(1, 10, ts=now - 7200), make_event(1, 20, ts=now - 60), make_event(8453, 5, ts=now - 30))
    config = ReconcileConfig(page_size=10, start_lookback_seconds=3600)
    report = reconcile_all(envio.post, inbox, SCOPE, config, now=lambda: float(now))
    assert report.ok
    assert sorted(_event_ids(inbox)) == ["1_20_0", "8453_5_0"]


def test_backlog_drains_in_bounded_chunks_without_duplicates(inbox: EventInbox) -> None:
    envio = FakeEnvio()
    _seed(envio, inbox)
    # 600 events on Mainnet: several per block, many sharing a timestamp, across
    # page boundaries (page 10) and chunk boundaries (span 100 blocks).
    events = [make_event(1, b, li, ts=1_700_000_000 + b // 7) for b in range(2, 302) for li in range(2)]
    envio.add(*events)

    # max_chunks 20 x span 100 covers the 300-block backlog in one cycle.
    report = reconcile_all(envio.post, inbox, SCOPE, CONFIG)
    mainnet = next(c for c in report.chains if c.chain_id == 1)
    assert report.ok and mainnet.caught_up
    assert mainnet.new == 600 and mainnet.changed == 0 and mainnet.removed == 0
    assert all(q["to"] - q["from"] <= 100 + 5 for q in envio.queries)  # bounded chunks (first one includes overlap)
    assert sorted(_event_ids(inbox)) == sorted(e["id"] for e in events)
    assert _checkpoint(inbox, 1) == 301

    # Rerun: only the overlap is re-read, and it yields duplicates, never new rows.
    report = reconcile_all(envio.post, inbox, SCOPE, CONFIG)
    mainnet = next(c for c in report.chains if c.chain_id == 1)
    assert mainnet.new == 0 and mainnet.duplicate == 10  # blocks 297..301, 2 logs each
    assert len(_event_ids(inbox)) == 600


def test_backlog_beyond_chunk_budget_resumes_next_cycle(inbox: EventInbox) -> None:
    envio = FakeEnvio()
    _seed(envio, inbox)
    envio.add(*(make_event(1, b) for b in range(2, 502)))
    config = ReconcileConfig(page_size=50, max_chunks_per_cycle=2, overlap_blocks={1: 5}, max_block_span={1: 100})
    first = reconcile_all(envio.post, inbox, {1: {TIMELOCK}}, config)
    assert not first.chains[0].caught_up and first.chains[0].to_block == 201
    second = reconcile_all(envio.post, inbox, {1: {TIMELOCK}}, config)
    third = reconcile_all(envio.post, inbox, {1: {TIMELOCK}}, config)
    assert second.chains[0].to_block == 401 and third.chains[0].caught_up
    assert len(_event_ids(inbox)) == 500


def test_chain_failure_does_not_hold_back_other_chains(inbox: EventInbox) -> None:
    envio = FakeEnvio()
    _seed(envio, inbox)
    envio.add(make_event(1, 10), make_event(8453, 10))
    envio.fail_chain[8453] = 1
    report = reconcile_all(envio.post, inbox, SCOPE, CONFIG)
    assert not report.ok
    assert _checkpoint(inbox, 1) == 10
    assert _checkpoint(inbox, 8453) == 1
    assert _event_ids(inbox) == ["1_10_0"]

    report = reconcile_all(envio.post, inbox, SCOPE, CONFIG)
    assert report.ok
    assert sorted(_event_ids(inbox)) == ["1_10_0", "8453_10_0"]


def test_metadata_outage_changes_nothing(inbox: EventInbox) -> None:
    envio = FakeEnvio()
    _seed(envio, inbox)
    envio.add(make_event(1, 10))
    envio.fail_metadata = 1
    report = reconcile_all(envio.post, inbox, SCOPE, CONFIG)
    assert report.error and report.chains == []
    assert _checkpoint(inbox, 1) == 1
    assert reconcile_all(envio.post, inbox, SCOPE, CONFIG).ok
    assert _event_ids(inbox) == ["1_10_0"]


def test_missing_chain_metadata_is_an_error(inbox: EventInbox) -> None:
    envio = FakeEnvio()
    envio.set_head(1, 5)
    report = reconcile_all(envio.post, inbox, SCOPE, CONFIG)
    errors = {c.chain_id: c.error for c in report.chains}
    assert errors == {1: None, 8453: "no chain_metadata row"}


def test_late_indexed_event_inside_overlap_is_recovered(inbox: EventInbox) -> None:
    envio = FakeEnvio()
    _seed(envio, inbox)
    envio.add(make_event(1, 20))
    reconcile_all(envio.post, inbox, SCOPE, CONFIG)
    # Block 18's row lands after the checkpoint passed it (late commit).
    envio.add(make_event(1, 18), advance_head=False)
    report = reconcile_all(envio.post, inbox, SCOPE, CONFIG)
    assert next(c for c in report.chains if c.chain_id == 1).new == 1
    assert sorted(_event_ids(inbox)) == ["1_18_0", "1_20_0"]


def test_reorg_replaced_and_dropped_events_are_recorded(inbox: EventInbox) -> None:
    envio = FakeEnvio()
    _seed(envio, inbox)
    envio.add(make_event(1, 20, 0), make_event(1, 20, 1), make_event(1, 21))
    reconcile_all(envio.post, inbox, SCOPE, CONFIG)

    # Reorg: block 20 has a new hash and its first log comes from another tx; the second log is gone.
    envio.add(make_event(1, 20, 0, blockHash="0x" + "dd" * 32, transactionHash="0x" + "ee" * 32))
    envio.remove("1_20_1")
    report = reconcile_all(envio.post, inbox, SCOPE, CONFIG)
    mainnet = next(c for c in report.chains if c.chain_id == 1)
    assert (mainnet.new, mainnet.changed, mainnet.removed) == (0, 1, 1)

    rows = {row["event_id"]: row for row in inbox.events_since(0)}
    assert rows["1_20_0"]["block_hash"] == "0x" + "dd" * 32
    statuses = {event_id: row["status"] for event_id, row in rows.items()}
    assert statuses == {"1_20_0": "active", "1_20_1": "removed", "1_21_0": "active"}
    kinds = sorted((c["event_id"], c["kind"]) for c in inbox.changes_since(0))
    assert kinds == [("1_20_0", "changed"), ("1_20_1", "removed")]

    # A rerun over the same blocks does not re-record the change.
    reconcile_all(envio.post, inbox, SCOPE, CONFIG)
    assert len(inbox.changes_since(0)) == 2


def test_restart_resumes_from_persisted_checkpoints(tmp_path: Path) -> None:
    path = str(tmp_path / "sub.db")
    envio = FakeEnvio()
    _seed(envio, EventInbox(path))
    envio.add(make_event(1, 30), make_event(8453, 40))
    reconcile_all(envio.post, EventInbox(path), SCOPE, CONFIG)

    # "Restart": a fresh inbox object on the same file, with more events indexed meanwhile.
    envio.add(make_event(1, 35), make_event(8453, 45))
    envio.queries.clear()
    restarted = EventInbox(path)
    report = reconcile_all(envio.post, restarted, SCOPE, CONFIG)
    assert report.ok
    froms = {q["chain_id"]: q["from"] for q in envio.queries if q["offset"] == 0}
    assert froms == {1: 30 - 5, 8453: 40 - 5}
    assert sorted(_event_ids(restarted)) == ["1_30_0", "1_35_0", "8453_40_0", "8453_45_0"]


def test_head_behind_checkpoint_does_not_rewind(inbox: EventInbox) -> None:
    envio = FakeEnvio()
    _seed(envio, inbox, head=100)
    envio.set_head(1, 40)  # indexer resync
    report = reconcile_all(envio.post, inbox, SCOPE, CONFIG)
    assert report.ok
    assert _checkpoint(inbox, 1) == 100


def test_reconcile_logs_timing(inbox: EventInbox) -> None:
    envio = FakeEnvio()
    _seed(envio, inbox)
    (entry, *_rest) = inbox.log_entries(0, ("reconcile",))
    assert entry["duration_ms"] is not None and entry["duration_ms"] >= 0


def test_concurrent_observers_never_collide_on_new_ids(inbox: EventInbox) -> None:
    """WS and HTTP threads observing the same unseen ids at once must not raise IntegrityError."""
    errors: list[BaseException] = []
    for round_ in range(20):
        batch = [make_event(1, round_ * 100 + i) for i in range(20)]
        barrier = threading.Barrier(2)

        def observe(source: str, batch: list[dict] = batch, barrier: threading.Barrier = barrier) -> None:
            barrier.wait()
            try:
                inbox.observe(batch, source, 1.0)
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)

        threads = [threading.Thread(target=observe, args=(s,)) for s in (SOURCE_WS, SOURCE_HTTP)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert errors == []
    rows = inbox.events_since(0)
    assert len(rows) == 400
    assert all(row["ws_observations"] == 1 and row["http_observations"] == 1 for row in rows)


# ── schema migration ───────────────────────────────────────────────────

# timelock_sub_events as created by the first pilot revision (schema v0, no block_hash).
_V0_EVENTS = """
CREATE TABLE timelock_sub_events (
    event_id TEXT PRIMARY KEY, chain_id INTEGER NOT NULL, timelock_address TEXT NOT NULL,
    block_number INTEGER NOT NULL, log_index INTEGER NOT NULL, block_timestamp INTEGER NOT NULL,
    transaction_hash TEXT NOT NULL, event_name TEXT, operation_id TEXT, fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL, first_source TEXT NOT NULL, ws_first_seen_at REAL, http_first_seen_at REAL,
    ws_observations INTEGER NOT NULL DEFAULT 0, http_observations INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE timelock_sub_checkpoints (
    chain_id INTEGER PRIMARY KEY, block_number INTEGER NOT NULL, indexed_head INTEGER NOT NULL,
    head_changed_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""


def _v0_database(path: Path) -> dict:
    """Create a v0 inbox holding one event (stored without blockHash) and one checkpoint."""
    legacy = make_event(1, 10)
    legacy.pop("blockHash")
    with sqlite3.connect(path) as conn:
        conn.executescript(_V0_EVENTS)
        conn.execute(
            "INSERT INTO timelock_sub_events (event_id, chain_id, timelock_address, block_number, log_index, "
            "block_timestamp, transaction_hash, event_name, operation_id, fingerprint, payload_json, first_seen_at, "
            "first_source, http_first_seen_at, http_observations, updated_at) "
            "VALUES (?, 1, ?, 10, 0, ?, ?, 'CallScheduled', ?, ?, ?, 5.0, 'http', 5.0, 1, 5.0)",
            (
                legacy["id"],
                TIMELOCK,
                legacy["blockTimestamp"],
                legacy["transactionHash"],
                legacy["operationId"],
                event_fingerprint(legacy),
                json.dumps(legacy, sort_keys=True),
            ),
        )
        conn.execute("INSERT INTO timelock_sub_checkpoints VALUES (1, 10, 10, 5.0, 5.0)")
    return legacy


def test_v0_database_is_migrated_in_place(tmp_path: Path) -> None:
    path = tmp_path / "sub.db"
    _v0_database(path)
    inbox = EventInbox(str(path))

    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "block_hash" in {r[1] for r in conn.execute("PRAGMA table_info(timelock_sub_events)")}
    (row,) = inbox.events_since(0)
    assert row["event_id"] == "1_10_0" and row["block_hash"] is None and row["first_seen_at"] == 5.0
    assert _checkpoint(inbox, 1) == 10

    # Re-observing it with the wider selection is a duplicate, not a reorg.
    result = inbox.observe([make_event(1, 10), make_event(1, 11)], SOURCE_WS, 20.0)
    assert (result.new, result.duplicate, result.changed) == (1, 1, 0)
    assert inbox.changes_since(0) == []
    rows = {r["event_id"]: r for r in inbox.events_since(0)}
    assert rows["1_10_0"]["block_hash"] == make_event(1, 10)["blockHash"]
    assert rows["1_10_0"]["revision"] == 0 and rows["1_10_0"]["ws_first_seen_at"] == 20.0

    # Reopening is a no-op, and a real change is still detected afterwards.
    reopened = EventInbox(str(path))
    replaced = make_event(1, 10, blockHash="0x" + "dd" * 32)
    assert reopened.observe([replaced], SOURCE_HTTP, 30.0).changed == 1


def test_widened_selection_does_not_hide_a_real_change(tmp_path: Path) -> None:
    path = tmp_path / "sub.db"
    _v0_database(path)
    inbox = EventInbox(str(path))
    # New field present AND the tx changed: a reorg, even though blockHash is new.
    result = inbox.observe([make_event(1, 10, transactionHash="0x" + "ee" * 32)], SOURCE_HTTP, 20.0)
    assert result.changed == 1
    assert [c["kind"] for c in inbox.changes_since(0)] == ["changed"]

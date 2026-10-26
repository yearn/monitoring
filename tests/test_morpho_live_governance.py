"""Tests for the live Morpho governance consumer (#448): rules parity, durable decisions, and delivery."""

import asyncio
import json
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from eth_abi import encode as abi_encode
from web3 import Web3

from protocols.morpho import live_rules
from protocols.morpho._alerts import LOSS_PREFIX, VaultAlert, render_vault_alerts
from protocols.morpho.config import VaultConfig
from protocols.morpho.governance import MarketGovernanceState
from protocols.morpho.governance_stream import MORPHO_GOVERNANCE_STREAM
from protocols.morpho.governance_v2 import ROLE_TYPE, V2GovernanceSnapshot
from protocols.morpho.live_governance import (
    BASELINE_STATUS_KEY,
    MODE_SHADOW,
    SHADOW_LABEL,
    LiveConfig,
    LiveGovernanceConsumer,
    ShadowChannelMissing,
    build_consumer,
    live_health,
    live_summary,
    send_shadow,
)
from protocols.morpho.live_rules import (
    REASON_ALERTED,
    REASON_NOT_NEW,
    REASON_RESOLUTION,
    GovernanceEvent,
    RpcBehindError,
    VaultEvaluation,
    evaluate_v1,
    evaluate_v2,
)
from protocols.morpho.live_store import (
    OUTCOME_ALERTED,
    OUTCOME_HISTORICAL,
    OUTCOME_NO_ALERT,
    STATUS_OBSERVED,
    STATUS_PENDING,
    STATUS_SENT,
    AlertRequest,
    LiveStore,
)
from protocols.morpho.v2_decoders import selector_of
from tests.timelock_subscriber_fakes import FakeEnvio, FakeWSServer, make_morpho_event
from utils.alert import AlertSeverity
from utils.cache import morpho_key
from utils.chains import Chain
from utils.subscriber.inbox import SOURCE_HTTP, SOURCE_WS, EventInbox
from utils.subscriber.reconcile import ReconcileConfig
from utils.subscriber.worker import Worker, WorkerConfig

V1_VAULT = "0x68Aea7b82Df6CcdF76235D46445Ed83f85F845A3"  # Yearn USDC V1, mainnet
V2_VAULT = "0xaA8d9E2aBa210639cE6C7cE21385e7c673ACa6f3"  # Yearn USDC V2, mainnet
ADAPTER = "0x" + "aa" * 20
NEW_OWNER = "0x" + "bb" * 20
MARKET = "0x" + "ab" * 32
METADATA = {"name": "WETH/USDC", "lltv": 86 * 10**16, "loan_decimals": 6, "loan_symbol": "USDC"}
OTHER_VAULT = "0x" + "f9" * 20
V1_CONFIG = VaultConfig("Yearn USDC", V1_VAULT, 1)
V2_CONFIG = VaultConfig("Yearn USDC", V2_VAULT, 1)
VALID_AT = 2_000_000_000


@pytest.fixture(autouse=True)
def configured_vaults() -> Iterator[None]:
    """Resolve vaults from synthetic configs, so the consumer tests don't depend on config.py's vault list."""
    configs = {V1_VAULT.lower(): V1_CONFIG, OTHER_VAULT: VaultConfig("Other", OTHER_VAULT, 1)}
    with patch(
        "protocols.morpho.live_governance.vault_config",
        side_effect=lambda chain, address, version: configs.get(address.lower()),
    ):
        yield


def _calldata(sig: str, types: list[str], values: list[Any]) -> str:
    return "0x" + (selector_of(sig) + abi_encode(types, values)).hex()


def _event(name: str, args: dict[str, Any], block: int = 100, log: int = 0, vault: str = V1_VAULT, version: int = 1):
    payload = make_morpho_event(
        1, block, log, address=vault, vaultVersion=version, eventName=name, argsJson=json.dumps(args)
    )
    return GovernanceEvent.from_payload(payload)


class _Batch(list):
    def add(self, value: Any) -> None:
        self.append(value)


def _client(head: int = 1_000) -> MagicMock:
    """Client whose batch returns the values added to it (each fake ``call`` returns its value directly)."""
    client = MagicMock()
    client.eth.block_number = head

    @contextmanager
    def batch_requests() -> Iterator[_Batch]:
        yield _Batch()

    client.batch_requests.side_effect = batch_requests
    client.execute_batch.side_effect = lambda batch: list(batch)
    return client


@contextmanager
def _v1_cache(values: dict[tuple[str, str], int] | None = None) -> Iterator[MagicMock]:
    """Patch the V1 cache (reads from ``values``, writes recorded) and market metadata."""
    values = values or {}
    write = MagicMock()
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "protocols.morpho.governance.get_last_executed_morpho_from_file",
                side_effect=lambda vault, market, kind: values.get((market, kind), 0),
            )
        )
        stack.enter_context(patch("protocols.morpho.governance.write_last_executed_morpho_to_file", write))
        stack.enter_context(patch("protocols.morpho.governance.fetch_market_metadata", return_value=METADATA))
        stack.enter_context(
            patch("protocols.morpho.governance.fetch_market_info", return_value=("WETH/USDC (86.00%)", 6))
        )
        yield write


def _state(**overrides: Any) -> MarketGovernanceState:
    fields: dict[str, Any] = {
        "vault_address": V1_VAULT,
        "market_id": MARKET,
        "pending_cap": 0,
        "pending_cap_timestamp": 0,
        "current_cap": 0,
        "removable_at": 0,
    }
    fields.update(overrides)
    return MarketGovernanceState(**fields)


# ── V1 rules ───────────────────────────────────────────────────────────


def test_v1_submit_cap_alerts_like_the_daily_job_without_writing_its_cache() -> None:
    state = _state(pending_cap=5 * 10**12, pending_cap_timestamp=VALID_AT)
    with (
        _v1_cache() as write,
        patch.object(live_rules.governance, "load_market_governance_states", return_value=[state]),
    ):
        result = evaluate_v1(_client(), Chain.MAINNET, V1_CONFIG, [_event("SubmitCap", {"id": MARKET, "cap": "1"})])

    (alert,) = result.alerts
    assert alert.severity == AlertSeverity.MEDIUM and "➕ Adding new market [WETH/USDC (86.00%)]" in alert.body
    assert result.reasons == {"1_100_0": REASON_ALERTED}
    assert result.header.startswith("[Yearn USDC](")
    write.assert_not_called()


def test_v1_removal_of_a_supplied_market_is_a_standalone_loss() -> None:
    state = _state(removable_at=VALID_AT, supply_shares=10**12, supply_assets=10**12, vault_total_assets=10**13)
    with _v1_cache(), patch.object(live_rules.governance, "load_market_governance_states", return_value=[state]):
        result = evaluate_v1(_client(), Chain.MAINNET, V1_CONFIG, [_event("SubmitMarketRemoval", {"id": MARKET})])

    (alert,) = result.alerts
    assert alert.standalone and alert.severity == AlertSeverity.HIGH
    (severity, message), *_ = render_vault_alerts(result.header, result.alerts)
    assert message.startswith(LOSS_PREFIX) and severity == AlertSeverity.HIGH


def test_v1_operation_no_longer_pending_or_already_alerted_is_not_new() -> None:
    revoked = _state()  # pendingCap / removableAt read back as 0
    already = _state(pending_cap=5, pending_cap_timestamp=VALID_AT)
    for state, cache in ((revoked, {}), (already, {(MARKET, "pending_cap"): VALID_AT})):
        with (
            _v1_cache(cache),
            patch.object(live_rules.governance, "load_market_governance_states", return_value=[state]),
        ):
            result = evaluate_v1(_client(), Chain.MAINNET, V1_CONFIG, [_event("SubmitCap", {"id": MARKET})])
        assert result.alerts == [] and result.reasons == {"1_100_0": REASON_NOT_NEW}


def test_v1_cap_and_removal_on_one_market_are_checked_once() -> None:
    state = _state(pending_cap=5, pending_cap_timestamp=VALID_AT, removable_at=VALID_AT)
    events = [_event("SubmitCap", {"id": MARKET}), _event("SubmitMarketRemoval", {"id": MARKET}, log=1)]
    with (
        _v1_cache(),
        patch.object(live_rules.governance, "load_market_governance_states", return_value=[state]) as load,
    ):
        result = evaluate_v1(_client(), Chain.MAINNET, V1_CONFIG, events)

    assert len(load.call_args.args[1]) == 1  # one market read
    assert len(result.alerts) == 2  # one cap section, one removal section: not doubled
    assert set(result.reasons.values()) == {REASON_ALERTED}


def test_rules_wait_for_an_rpc_behind_the_event_block() -> None:
    with pytest.raises(RpcBehindError):
        evaluate_v1(_client(head=99), Chain.MAINNET, V1_CONFIG, [_event("SubmitCap", {"id": MARKET}, block=100)])


# ── V2 rules ───────────────────────────────────────────────────────────


def _snapshot(adapter_assets: int = 0, owner: str = NEW_OWNER) -> V2GovernanceSnapshot:
    return V2GovernanceSnapshot(
        name="Yearn USDC",
        address=V2_VAULT,
        chain=Chain.MAINNET,
        owner=str(Web3.to_checksum_address(owner)),
        curator=str(Web3.to_checksum_address("0x" + "cc" * 20)),
        sentinels=[],
        allocators=[],
        adapters=[str(Web3.to_checksum_address(ADAPTER))],
        asset_symbol="USDC",
        asset_decimals=6,
        total_assets=4_000_000 * 10**6,
        adapter_assets={str(Web3.to_checksum_address(ADAPTER)): adapter_assets},
    )


def _v2_client(executable_at: dict[bytes, int]) -> MagicMock:
    client = _client()
    contract = MagicMock()
    contract.functions.executableAt.side_effect = lambda data: MagicMock(
        call=lambda **_: executable_at.get(bytes(data), 0)
    )
    client.eth.contract.return_value = contract
    return client


@contextmanager
def _v2_cache(values: dict[str, str] | None = None) -> Iterator[MagicMock]:
    values = values or {}
    write = MagicMock()
    with (
        patch(
            "protocols.morpho.governance_v2.get_last_value_for_key_from_file",
            side_effect=lambda _f, key: values.get(key, 0),
        ),
        patch("protocols.morpho.governance_v2.write_last_value_to_file", write),
    ):
        yield write


def _submit(data: str, log: int = 0) -> GovernanceEvent:
    return _event("Submit", {"selector": data[:10], "data": data, "executableAt": str(VALID_AT)}, log=log,
                  vault=V2_VAULT, version=2)  # fmt: skip


REMOVE = _calldata("removeAdapter(address)", ["address"], [ADAPTER])
ABDICATE = _calldata("abdicate(bytes4)", ["bytes4"], [selector_of("setIsAllocator(address,bool)")])
ADD = _calldata("addAdapter(address)", ["address"], ["0x" + "dd" * 20])


def _bytes(data: str) -> bytes:
    return bytes.fromhex(data[2:])


def test_v2_remove_funded_adapter_is_a_standalone_loss_confirmed_on_chain() -> None:
    client = _v2_client({_bytes(REMOVE): VALID_AT})
    with _v2_cache() as write, patch.object(live_rules, "load_v2_snapshot", return_value=_snapshot(10**12)):
        result = evaluate_v2(client, Chain.MAINNET, V2_CONFIG, [_submit(REMOVE)])

    (alert,) = result.alerts
    assert alert.standalone and alert.severity == AlertSeverity.HIGH
    assert "removeAdapter" in alert.body and "1.00M USDC" in alert.body
    assert result.reasons == {"1_100_0": REASON_ALERTED}
    write.assert_not_called()


def test_v2_new_submissions_are_grouped_like_the_daily_job() -> None:
    client = _v2_client({_bytes(ABDICATE): VALID_AT, _bytes(ADD): VALID_AT})
    with _v2_cache(), patch.object(live_rules, "load_v2_snapshot", return_value=_snapshot()):
        result = evaluate_v2(client, Chain.MAINNET, V2_CONFIG, [_submit(ABDICATE), _submit(ADD, log=1)])

    (alert,) = result.alerts
    assert alert.severity == AlertSeverity.MEDIUM and alert.body.startswith("📥 Submitted 2 operations:")
    assert set(result.reasons.values()) == {REASON_ALERTED}


def test_v2_submit_already_accepted_or_revoked_is_not_new() -> None:
    client = _v2_client({})  # executableAt(data) == 0
    with _v2_cache(), patch.object(live_rules, "load_v2_snapshot", return_value=_snapshot(10**12)):
        result = evaluate_v2(client, Chain.MAINNET, V2_CONFIG, [_submit(REMOVE)])
    assert result.alerts == [] and result.reasons == {"1_100_0": REASON_NOT_NEW}


def test_v2_owner_change_uses_the_daily_role_cache_and_accept_is_left_to_the_daily_job() -> None:
    old_owner = "0x" + "ee" * 20
    events = [
        _event("SetOwner", {"newOwner": NEW_OWNER}, vault=V2_VAULT, version=2),
        _event("Accept", {"selector": ADD[:10], "data": ADD}, log=1, vault=V2_VAULT, version=2),
    ]
    with (
        _v2_cache({morpho_key(V2_VAULT.lower(), "owner", ROLE_TYPE): old_owner}),
        patch.object(live_rules, "load_v2_snapshot", return_value=_snapshot()),
    ):
        result = evaluate_v2(_v2_client({}), Chain.MAINNET, V2_CONFIG, events)

    (alert,) = result.alerts
    assert alert.severity == AlertSeverity.HIGH and "Owner changed" in alert.body
    assert result.reasons == {"1_100_0": REASON_ALERTED, "1_100_1": REASON_RESOLUTION}


def test_load_v2_snapshot_reads_roles_adapters_and_values_on_chain() -> None:
    client = _client()
    vault = MagicMock()
    vault.functions.owner.return_value.call.return_value = NEW_OWNER
    vault.functions.curator.return_value.call.return_value = "0x" + "cc" * 20
    vault.functions.asset.return_value.call.return_value = "0x" + "a0" * 20
    vault.functions.adaptersLength.return_value.call.return_value = 1
    vault.functions.adapters.return_value.call.return_value = ADAPTER
    vault.functions.totalAssets.return_value.call.side_effect = ValueError("execution reverted")
    adapter = MagicMock()
    adapter.functions.realAssets.return_value.call.return_value = 7
    client.eth.contract.side_effect = lambda address, abi: adapter if address.lower() == ADAPTER else vault

    with patch.object(live_rules, "fetch_erc20_metadata", return_value=MagicMock(symbol="USDC", decimals=6)):
        snapshot = live_rules.load_v2_snapshot(client, Chain.MAINNET, V2_CONFIG, 1_000)

    checksummed = str(Web3.to_checksum_address(ADAPTER))
    assert snapshot.adapters == [checksummed] and snapshot.adapter_assets == {checksummed: 7}
    assert snapshot.owner == Web3.to_checksum_address(NEW_OWNER) and snapshot.total_assets is None
    assert (snapshot.asset_symbol, snapshot.asset_decimals) == ("USDC", 6)


# ── consumer: decisions, history, retries ──────────────────────────────


class FakeEvaluator:
    """Stands in for the on-chain rules: one MEDIUM section per event, or a scripted failure."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.fail: list[Exception] = []

    def __call__(self, chain: Chain, vault: VaultConfig, version: int, events: list[GovernanceEvent]):
        self.calls.append((vault.address, [e.event_id for e in events]))
        if self.fail:
            raise self.fail.pop(0)
        return VaultEvaluation(
            header=f"[{vault.name}] on {chain.name}",
            alerts=[VaultAlert(AlertSeverity.MEDIUM, f"📥 Submitted {e.name}") for e in events],
            reasons={e.event_id: REASON_ALERTED for e in events},
        )


def _store(tmp_path: Path) -> LiveStore:
    return LiveStore(EventInbox(MORPHO_GOVERNANCE_STREAM, str(tmp_path / "morpho.db")))


def _observe(store: LiveStore, *rows: dict[str, Any], source: str = SOURCE_WS, at: float = 1_700_000_100.0) -> None:
    store.inbox.observe(list(rows), source, at)


def _consumer(store: LiveStore, evaluate: Any, clock: list[float], **config: Any) -> LiveGovernanceConsumer:
    return LiveGovernanceConsumer(
        store, LiveConfig(**config), evaluate=evaluate, send=MagicMock(), now=lambda: clock[0]
    )


def test_history_before_the_baseline_is_never_evaluated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    evaluate = FakeEvaluator()
    clock = [1_700_000_000.0]
    consumer = _consumer(store, evaluate, clock)
    consumer.load_baseline()
    _observe(store, make_morpho_event(1, 10, ts=1_699_999_000), make_morpho_event(1, 11, ts=1_700_000_050))

    assert consumer.process_once() == 2

    assert evaluate.calls == [(V1_VAULT, ["1_11_0"])]
    outcomes = {row["event_id"]: row["outcome"] for row in store.decisions_since(0)}
    assert outcomes == {"1_10_0": OUTCOME_HISTORICAL, "1_11_0": OUTCOME_ALERTED}
    assert store.inbox.status()[BASELINE_STATUS_KEY][0] == 1_700_000_000.0


def test_observe_mode_records_grouped_requests_and_sends_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    evaluate = FakeEvaluator()
    send = MagicMock()
    consumer = LiveGovernanceConsumer(store, LiveConfig(), evaluate=evaluate, send=send, now=lambda: 1_000.0)
    other_vault = OTHER_VAULT
    _observe(
        store, make_morpho_event(1, 10), make_morpho_event(1, 10, 1), make_morpho_event(1, 12, address=other_vault)
    )

    consumer.process_once()

    # per vault, in chain order
    assert evaluate.calls == [(V1_VAULT, ["1_10_0", "1_10_1"]), (OTHER_VAULT, ["1_12_0"])]
    alerts = store.alerts_since(0)
    assert [a["status"] for a in alerts] == [STATUS_OBSERVED, STATUS_OBSERVED]  # one message per vault
    assert json.loads(alerts[0]["event_ids"]) == ["1_10_0", "1_10_1"]
    assert store.pending_alerts() == [] and store.undecided() == []
    send.assert_not_called()


def test_unconfigured_vault_and_an_empty_evaluation_record_no_alert(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def nothing(chain, vault, version, events):  # noqa: ANN001, ANN202
        return VaultEvaluation(header="h", reasons={e.event_id: REASON_NOT_NEW for e in events})

    _observe(store, make_morpho_event(1, 10), make_morpho_event(1, 11, address="0x" + "99" * 20))
    _consumer(store, nothing, [1_000.0]).process_once()

    reasons = {row["event_id"]: (row["outcome"], row["reason"]) for row in store.decisions_since(0)}
    assert reasons == {
        "1_10_0": (OUTCOME_NO_ALERT, REASON_NOT_NEW),
        "1_11_0": (OUTCOME_NO_ALERT, "vault not in config.py"),
    }
    assert store.alerts_since(0) == []


def test_rpc_lag_and_failures_retry_with_backoff_without_blocking_other_vaults(tmp_path: Path) -> None:
    store = _store(tmp_path)
    evaluate = FakeEvaluator()
    evaluate.fail = [RpcBehindError("head behind"), RuntimeError("rpc down")]
    clock = [1_000.0]
    consumer = _consumer(store, evaluate, clock, retry_initial=5.0)
    _observe(store, make_morpho_event(1, 10))

    consumer.process_once()  # RPC behind
    consumer.process_once()  # still inside the retry delay: not called again
    clock[0] += 6
    consumer.process_once()  # fails
    clock[0] += 6
    consumer.process_once()  # inside the doubled delay (10s)
    clock[0] += 5
    consumer.process_once()  # succeeds

    assert len(evaluate.calls) == 3
    assert [row["outcome"] for row in store.decisions_since(0)] == [OUTCOME_ALERTED]


def test_restart_resumes_undecided_events_and_keeps_the_baseline(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = [1_700_000_000.0]
    _consumer(store, FakeEvaluator(), clock).process_once()
    _observe(store, make_morpho_event(1, 10, ts=1_700_000_010))

    clock[0] = 1_800_000_000.0  # restarted much later: the event is still after the stored baseline
    restarted = _consumer(_store(tmp_path), FakeEvaluator(), clock)
    restarted.process_once()

    assert restarted.baseline == 1_700_000_000.0
    assert [row["outcome"] for row in store.decisions_since(0)] == [OUTCOME_ALERTED]


def test_a_reorg_rewrite_is_evaluated_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    evaluate = FakeEvaluator()
    consumer = _consumer(store, evaluate, [1_000.0])
    _observe(store, make_morpho_event(1, 10))
    consumer.process_once()
    consumer.process_once()  # decided: nothing to do
    _observe(store, make_morpho_event(1, 10, blockHash="0x" + "dd" * 32), source=SOURCE_HTTP)
    consumer.process_once()

    assert [ids for _, ids in evaluate.calls] == [["1_10_0"], ["1_10_0"]]
    (decision,) = store.decisions_since(0)
    assert decision["revision"] == 1


# ── consumer: shadow delivery ──────────────────────────────────────────


def test_shadow_requests_are_delivered_in_order_and_failures_retried(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sent: list[int] = []
    attempts = {"n": 0}

    def send(request: AlertRequest) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("telegram 502")
        sent.append(request.id)

    consumer = LiveGovernanceConsumer(
        store,
        LiveConfig(mode=MODE_SHADOW, poll_interval=0.02, delivery_interval=0.01, retry_initial=0.05),
        evaluate=FakeEvaluator(),
        send=send,
        now=time.time,
    )
    other_vault = OTHER_VAULT
    _observe(
        store,
        make_morpho_event(1, 10, ts=int(time.time())),
        make_morpho_event(1, 11, ts=int(time.time()), address=other_vault),
    )

    async def scenario() -> None:
        task = asyncio.create_task(consumer.run())
        deadline = time.monotonic() + 5
        while len(sent) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    alerts = store.alerts_since(0)
    assert sent == [alerts[0]["id"], alerts[1]["id"]]  # the failed first request still went first
    assert [a["status"] for a in alerts] == [STATUS_SENT, STATUS_SENT]
    assert alerts[0]["attempts"] == 2 and alerts[1]["attempts"] == 1


def test_shadow_requires_the_errors_chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert build_consumer(_store(tmp_path), MODE_SHADOW).config.mode == "observe"  # conftest strips TELEGRAM_*
    request = AlertRequest(1, 1, V1_VAULT, MODE_SHADOW, "HIGH", "🚨 POTENTIAL VAULT LOSS — x", ["1_10_0"], 0)
    with pytest.raises(ShadowChannelMissing):
        send_shadow(request)  # conftest strips TELEGRAM_*

    monkeypatch.setenv("TELEGRAM_CHAT_ID_ERRORS", "-100123")
    with patch("protocols.morpho.live_governance.send_error_message") as send:
        send_shadow(request)
    text, label = send.call_args.args
    assert label == SHADOW_LABEL and text.startswith("shadow → morpho, HIGH, events 1_10_0")
    assert send.call_args.kwargs["alert_protocol"] == "yearn-internal"


def test_live_health_and_summary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = [1_000.0]
    consumer = LiveGovernanceConsumer(
        store, LiveConfig(mode=MODE_SHADOW), evaluate=FakeEvaluator(), now=lambda: clock[0]
    )
    consumer.load_baseline()
    _observe(store, make_morpho_event(1, 10, ts=1_000), at=1_005.0)
    clock[0] = 1_010.0
    consumer.process_once()

    assert live_health(store, 1_100.0) == []
    assert live_health(store, 2_000.0) == ["live: 1 shadow alert(s) undelivered, oldest 990s"]
    summary = live_summary(store, 1, 1_100.0)
    assert "decisions: 1 {'alerted': 1}" in summary and f"'{STATUS_PENDING}': 1" in summary
    assert "block → decision: p50 10.0s, p95 10.0s" in summary


# ── worker integration ─────────────────────────────────────────────────


def test_worker_wakes_the_consumer_after_a_websocket_ingest(tmp_path: Path) -> None:
    async def scenario() -> None:
        envio = FakeEnvio()
        envio.set_head(1, 1)
        server = FakeWSServer(envio)
        await server.start()
        store = _store(tmp_path)
        evaluate = FakeEvaluator()
        # A long poll interval: only the ingest wake-up can make the decision this fast.
        consumer = LiveGovernanceConsumer(store, LiveConfig(poll_interval=60), evaluate=evaluate)
        config = WorkerConfig(
            reconcile_interval=60,
            heartbeat_interval=0.05,
            backoff_initial=0.05,
            reconcile=ReconcileConfig(page_size=20, overlap_blocks={1: 5}, max_block_span={1: 50}),
        )
        worker = Worker(envio.post, server.url, store.inbox, {1: {V1_VAULT.lower()}}, config, consumer=consumer)
        task = asyncio.create_task(worker.run())
        try:
            deadline = time.monotonic() + 5
            while not (worker.ws_connected and consumer.baseline) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)  # let the consumer finish its first (empty) pass
            envio.add(make_morpho_event(1, 10, ts=int(time.time()) + 1))
            while not store.decisions_since(0) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert worker.is_alive()
        finally:
            worker.stop()
            await asyncio.wait_for(task, 5)
            await server.stop()
        assert [row["outcome"] for row in store.decisions_since(0)] == [OUTCOME_ALERTED]

    with patch("utils.subscriber.worker.notify"):
        asyncio.run(scenario())


def test_a_dead_consumer_makes_the_worker_unhealthy(tmp_path: Path) -> None:
    class Crashing:
        async def run(self) -> None:
            raise RuntimeError("bug")

        def wake(self) -> None:
            pass

    async def scenario() -> None:
        store = _store(tmp_path)
        config = WorkerConfig(transport="http", reconcile_interval=60, heartbeat_interval=0.05)
        worker = Worker(FakeEnvio().post, None, store.inbox, {1: {V1_VAULT.lower()}}, config, consumer=Crashing())
        task = asyncio.create_task(worker.run())
        try:
            deadline = time.monotonic() + 5
            while worker.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert not worker.is_alive()
        finally:
            worker.stop()
            await asyncio.wait_for(task, 5)

    with patch("utils.subscriber.worker.notify"):
        asyncio.run(scenario())

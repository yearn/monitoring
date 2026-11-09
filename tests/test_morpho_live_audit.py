"""Tests for the daily-versus-live audit (#449) and the production delivery bookkeeping around it."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from protocols.morpho import governance, governance_v2
from protocols.morpho._alerts import LiveExpectation, VaultAlert, VaultDiff, render_vault_alerts
from protocols.morpho.config import VaultConfig
from protocols.morpho.governance_stream import MORPHO_GOVERNANCE_STREAM
from protocols.morpho.governance_v2 import PendingConfig
from protocols.morpho.live_audit import (
    STATUS_DIVERGENT,
    STATUS_MATCHED,
    STATUS_OUT_OF_WINDOW,
    STATUS_WAITING,
    WAITING_STALE_SECONDS,
    WINDOW_SECONDS,
    AuditStore,
    audit_health,
    audit_summary,
    notify_divergences,
    record_daily_findings,
    resolve_findings,
)
from protocols.morpho.live_governance import (
    MODE_PRODUCTION,
    MODE_SHADOW,
    LiveConfig,
    LiveGovernanceConsumer,
    live_summary,
    shadow_texts,
)
from protocols.morpho.live_rules import REASON_ALERTED, REASON_NOT_NEW, VaultEvaluation
from protocols.morpho.live_store import (
    BASELINE_STATUS_KEY,
    OUTCOME_ALERTED,
    OUTCOME_HISTORICAL,
    OUTCOME_NO_ALERT,
    STATUS_OBSERVED,
    AlertRequest,
    Decision,
)
from tests.timelock_subscriber_fakes import make_morpho_event
from utils.alert import AlertSeverity
from utils.chains import Chain
from utils.paths import cache_path
from utils.subscriber.inbox import SOURCE_HTTP, SOURCE_WS, EventInbox
from utils.telegram import MAX_MESSAGE_LENGTH

VAULT = "0x68Aea7b82Df6CcdF76235D46445Ed83f85F845A3"
MARKET = "0x" + "ab" * 32
BASELINE = 1_700_000_000.0
OBSERVED = BASELINE + WINDOW_SECONDS + 1_000
EVENT_TS = int(OBSERVED - 100)
BLOCK = 100


@pytest.fixture(autouse=True)
def configured_vault() -> Iterator[None]:
    """Resolve the test vault without depending on config.py's vault list."""
    config = VaultConfig("Yearn USDC", VAULT, 1)
    with patch("protocols.morpho.live_governance.vault_config", return_value=config):
        with patch("protocols.morpho.live_governance.governance_lock", return_value=MagicMock()):
            yield


def _store(tmp_path: Path, baseline: float | None = BASELINE) -> AuditStore:
    store = AuditStore(EventInbox(MORPHO_GOVERNANCE_STREAM, str(tmp_path / "morpho.db")))
    if baseline is not None:
        store.inbox.set_status(BASELINE_STATUS_KEY, baseline, baseline)
    return store


def _finding(store: AuditStore, args: dict[str, Any] | None = None, at: float = OBSERVED) -> None:
    expected = LiveExpectation("SubmitMarketRemoval", {"id": MARKET} if args is None else args)
    store.add_findings(1, VAULT, [expected], BLOCK, at)


def _event(store: AuditStore, **overrides: Any) -> None:
    fields: dict[str, Any] = {"ts": EVENT_TS, "address": VAULT.lower(), **overrides}
    store.inbox.observe([make_morpho_event(1, BLOCK, **fields)], SOURCE_WS, OBSERVED - 90)


def _decide(store: AuditStore, outcome: str, at: float, reason: str = "why") -> None:
    store.record([Decision(e, outcome, reason) for e in store.undecided()], [], MODE_SHADOW, STATUS_OBSERVED, at)


def _status(store: AuditStore) -> str:
    resolve_findings(store, OBSERVED + 500)
    return str(store.findings_since(0)[0]["status"])


def _reconciled(store: AuditStore, block: int = BLOCK + 10) -> None:
    store.inbox.set_checkpoint(1, block, block, OBSERVED + 400)


def test_a_finding_the_live_path_alerted_is_matched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _event(store)
    _decide(store, OUTCOME_ALERTED, OBSERVED - 80)
    _finding(store)
    _reconciled(store)
    assert _status(store) == STATUS_MATCHED


def test_arguments_compare_across_case_and_integer_encoding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _event(store, eventName="SubmitTimelock", argsJson='{"newTimelock":"259200"}')
    _decide(store, OUTCOME_ALERTED, OBSERVED - 80)
    store.add_findings(
        1,
        VAULT.upper().replace("0X", "0x"),
        [LiveExpectation("SubmitTimelock", {"newTimelock": 259200})],
        BLOCK,
        OBSERVED,
    )
    _reconciled(store)
    assert _status(store) == STATUS_MATCHED


def test_no_event_is_divergent_only_once_reconciliation_passed_the_daily_block(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _finding(store)
    assert _status(store) == STATUS_WAITING  # no checkpoint at all yet

    _reconciled(store, BLOCK - 1)
    assert _status(store) == STATUS_WAITING  # indexer lag is neither agreement nor divergence

    _reconciled(store)
    assert _status(store) == STATUS_DIVERGENT
    assert "no indexed event through block" in store.findings_since(0)[0]["detail"]


def test_older_alert_cannot_mask_the_latest_matching_decision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _event(store, id="old", blockNumber=BLOCK - 1)
    _decide(store, OUTCOME_ALERTED, OBSERVED - 80)
    _finding(store)

    _reconciled(store, BLOCK - 1)
    assert _status(store) == STATUS_WAITING

    _event(store)
    _decide(store, OUTCOME_NO_ALERT, OBSERVED - 50, REASON_NOT_NEW)
    _reconciled(store)
    assert _status(store) == STATUS_DIVERGENT


def test_same_submit_arguments_from_another_transaction_cannot_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    args = {"data": "0x1234", "executableAt": 1000}
    _event(store, eventName="Submit", argsJson=json.dumps(args), transactionHash="0x" + "11" * 32)
    _decide(store, OUTCOME_ALERTED, OBSERVED - 80)
    store.add_findings(1, VAULT, [LiveExpectation("Submit", args, "0x" + "22" * 32)], BLOCK, OBSERVED)
    _reconciled(store)
    assert _status(store) == STATUS_DIVERGENT


def test_event_after_the_daily_snapshot_cannot_match_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _event(store, blockNumber=BLOCK + 1)
    _decide(store, OUTCOME_ALERTED, OBSERVED + 80)
    _finding(store)
    _reconciled(store)
    assert _status(store) == STATUS_DIVERGENT


def test_an_event_for_another_market_is_not_a_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _event(store)
    _decide(store, OUTCOME_ALERTED, OBSERVED - 80)
    _finding(store, {"id": "0x" + "cd" * 32})
    _reconciled(store)
    assert _status(store) == STATUS_DIVERGENT


def test_an_event_outside_the_window_is_not_a_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _event(store, ts=int(OBSERVED - WINDOW_SECONDS - 10))
    _decide(store, OUTCOME_ALERTED, OBSERVED - 80)
    _finding(store)
    _reconciled(store)
    assert _status(store) == STATUS_DIVERGENT


def test_a_reorged_event_is_not_a_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _event(store)
    _decide(store, OUTCOME_ALERTED, OBSERVED - 80)
    store.inbox.mark_missing(1, BLOCK, BLOCK, set(), SOURCE_HTTP, OBSERVED - 70)
    _finding(store)
    _reconciled(store)
    assert _status(store) == STATUS_DIVERGENT


def test_an_undecided_event_keeps_waiting(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _event(store)
    _finding(store)
    _reconciled(store)
    assert _status(store) == STATUS_WAITING


def test_evaluating_after_the_daily_alert_is_catching_up_not_divergence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _event(store)
    _decide(store, OUTCOME_NO_ALERT, OBSERVED + 50, REASON_NOT_NEW)
    _finding(store)
    _reconciled(store)
    assert _status(store) == STATUS_MATCHED


def test_deciding_no_alert_before_the_daily_alert_is_divergent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _event(store)
    _decide(store, OUTCOME_NO_ALERT, OBSERVED - 50, REASON_NOT_NEW)
    _finding(store)
    _reconciled(store)
    assert _status(store) == STATUS_DIVERGENT
    assert REASON_NOT_NEW in store.findings_since(0)[0]["detail"]


def test_findings_near_the_first_run_are_out_of_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _finding(store, at=BASELINE + WINDOW_SECONDS - 1)
    _reconciled(store)
    assert _status(store) == STATUS_OUT_OF_WINDOW


def test_a_historical_event_is_out_of_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _event(store)
    _decide(store, OUTCOME_HISTORICAL, OBSERVED - 80)
    _finding(store)
    _reconciled(store)
    assert _status(store) == STATUS_OUT_OF_WINDOW


def test_findings_wait_for_the_consumer_to_set_its_baseline(tmp_path: Path) -> None:
    store = _store(tmp_path, baseline=None)
    _finding(store)
    _reconciled(store)
    assert _status(store) == STATUS_WAITING


def test_divergences_are_notified_once_and_retried_after_a_failed_send(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _finding(store)
    _reconciled(store)
    resolve_findings(store, OBSERVED + 500)

    with pytest.raises(RuntimeError):
        notify_divergences(store, MagicMock(side_effect=RuntimeError("telegram down")), OBSERVED + 501)

    notify = MagicMock()
    assert notify_divergences(store, notify, OBSERVED + 502) == 1
    assert notify_divergences(store, notify, OBSERVED + 503) == 0
    text = notify.call_args.args[0]
    assert "[morpho-live-audit]" in text and f"https://etherscan.io/address/{VAULT.lower()}" in text


def test_health_reports_recent_divergence_and_stuck_findings(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _finding(store)
    assert audit_health(store, OBSERVED + 1) == []
    assert audit_health(store, OBSERVED + WAITING_STALE_SECONDS + 1) == ["audit: 1 finding(s) waiting for over 24h"]

    _reconciled(store)
    resolve_findings(store, OBSERVED + 500)
    assert audit_health(store, OBSERVED + 501) == ["audit: 1 divergent finding(s) in the last 7 days"]
    assert audit_health(store, OBSERVED + 8 * 86_400) == []

    summary = audit_summary(store, 1, OBSERVED + 501)
    assert "findings: 1 {'divergent': 1}" in summary and "SubmitMarketRemoval" in summary


# ── recording from the daily jobs ──────────────────────────────────────


_EXPECTING = [VaultAlert(AlertSeverity.MEDIUM, "x", expected=(LiveExpectation("SubmitCap", {"id": MARKET}),))]


def test_recording_is_skipped_without_an_inbox_and_reads_no_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    with patch("protocols.morpho.live_audit.ChainManager") as manager:
        record_daily_findings(Chain.MAINNET, VAULT, _EXPECTING)
    manager.get_client.assert_not_called()
    assert not Path(cache_path(MORPHO_GOVERNANCE_STREAM.db_filename)).exists()


def test_alerts_with_nothing_to_expect_record_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    path = cache_path(MORPHO_GOVERNANCE_STREAM.db_filename)
    store = AuditStore(EventInbox(MORPHO_GOVERNANCE_STREAM, path))
    record_daily_findings(Chain.MAINNET, VAULT, [VaultAlert(AlertSeverity.HIGH, "guardian")], 5)
    assert store.findings_since(0) == []


def test_recording_stores_the_block_and_reads_the_head_when_it_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    path = cache_path(MORPHO_GOVERNANCE_STREAM.db_filename)
    store = AuditStore(EventInbox(MORPHO_GOVERNANCE_STREAM, path))
    record_daily_findings(Chain.MAINNET, VAULT, _EXPECTING, 77, now=lambda: 5.0)
    with patch("protocols.morpho.live_audit.ChainManager") as manager:
        manager.get_client.return_value.eth.block_number = 88
        record_daily_findings(Chain.MAINNET, VAULT, _EXPECTING, now=lambda: 6.0)
    rows = store.findings_since(0)
    assert [(r["observed_block"], r["vault_address"], r["status"]) for r in rows] == [
        (77, VAULT.lower(), STATUS_WAITING),
        (88, VAULT.lower(), STATUS_WAITING),
    ]


def test_a_recording_failure_never_reaches_the_daily_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CACHE_DIR", str(tmp_path))
    AuditStore(EventInbox(MORPHO_GOVERNANCE_STREAM, cache_path(MORPHO_GOVERNANCE_STREAM.db_filename)))
    with patch("protocols.morpho.live_audit.AuditStore", side_effect=OSError("disk full")):
        record_daily_findings(Chain.MAINNET, VAULT, _EXPECTING, 1)


def _snapshot() -> MagicMock:
    return MagicMock(chain=Chain.MAINNET)


def test_v2_new_submissions_expect_their_submit_calldata() -> None:
    diff = VaultDiff()
    pending = [(PendingConfig(5, "f", bytes.fromhex(h), "0x" + "11" * 32), "op") for h in ("aa", "bb")]
    with patch.object(governance_v2, "_explorer_link", return_value="tx"):
        governance_v2.alert_pending_new(_snapshot(), pending, diff)
    assert diff.alerts[0].expected == (
        LiveExpectation("Submit", {"data": "0xaa", "executableAt": 5}, "0x" + "11" * 32),
        LiveExpectation("Submit", {"data": "0xbb", "executableAt": 5}, "0x" + "11" * 32),
    )


def test_v2_only_adapter_set_changes_are_audited() -> None:
    diff = VaultDiff()
    governance_v2._alert_set_diff("sentinels", {"0xA"}, set(), diff)
    governance_v2._alert_set_diff("allocators", set(), {"0xB"}, diff)
    governance_v2._alert_set_diff("adapters", {"0xC"}, {"0xD"}, diff)
    assert [a.expected for a in diff.alerts] == [
        (),
        (),
        (LiveExpectation("AddAdapter", {"account": "0xC"}), LiveExpectation("RemoveAdapter", {"account": "0xD"})),
    ]


def test_v2_role_changes_expect_the_new_holder() -> None:
    diff = VaultDiff()
    governance_v2._alert_role_change("owner", "0xA", "0xB", diff)
    governance_v2._alert_role_change("curator", "0xA", "0xC", diff)
    assert [a.expected for a in diff.alerts] == [
        (LiveExpectation("SetOwner", {"newOwner": "0xB"}),),
        (LiveExpectation("SetCurator", {"newCurator": "0xC"}),),
    ]


def test_v1_guardian_changes_are_not_audited() -> None:
    diff = VaultDiff()
    with patch.object(governance, "get_last_executed_morpho_from_file", return_value=0):
        governance.check_pending_role_change(MagicMock(address=VAULT), "guardian", 5, diff)
    assert diff.alerts[0].expected == ()


def _removal_state(supply_shares: int) -> MagicMock:
    return MagicMock(vault_address=VAULT, market_id=MARKET, removable_at=2_000_000_000, supply_shares=supply_shares)


def test_v1_new_removal_of_a_supplied_market_expects_its_event() -> None:
    diff = VaultDiff()
    with (
        patch.object(governance, "get_last_executed_morpho_from_file", return_value=0),
        patch.object(governance, "_removal_loss_body", return_value="body"),
    ):
        governance._check_market_removal(_removal_state(1), Chain.MAINNET, diff)
    assert diff.alerts[0].expected == (LiveExpectation("SubmitMarketRemoval", {"id": MARKET}),)


def test_v1_escalation_of_a_seen_removal_has_no_event_to_expect() -> None:
    state = _removal_state(1)
    diff = VaultDiff()
    last = {governance.REMOVABLE_AT_TYPE: state.removable_at}
    with (
        patch.object(governance, "get_last_executed_morpho_from_file", side_effect=lambda v, m, t: last.get(t, 0)),
        patch.object(governance, "_removal_loss_body", return_value="body"),
    ):
        governance._check_market_removal(state, Chain.MAINNET, diff)
    assert diff.alerts[0].standalone and diff.alerts[0].expected == ()


# ── production delivery bookkeeping ────────────────────────────────────


def _evaluate(chain: Chain, vault: VaultConfig, version: int, events: list[Any]) -> VaultEvaluation:
    return VaultEvaluation(
        header=f"[{vault.name}]",
        alerts=[
            VaultAlert(
                AlertSeverity.HIGH if index == 0 else AlertSeverity.MEDIUM,
                f"📥 Submitted {event.event_id}",
                standalone=index == 0 and len(events) > 1,
                source_ids=(event.event_id,),
            )
            for index, event in enumerate(events)
        ],
        reasons={e.event_id: REASON_ALERTED for e in events},
    )


def _production(store: AuditStore, publish: Any, clock: list[float]) -> LiveGovernanceConsumer:
    consumer = LiveGovernanceConsumer(
        store, LiveConfig(mode=MODE_PRODUCTION), evaluate=_evaluate, publish=publish, now=lambda: clock[0]
    )
    consumer.load_baseline()
    return consumer


def _sent_alert(store: AuditStore, publish: Any, clock: list[float]) -> LiveGovernanceConsumer:
    consumer = _production(store, publish, clock)
    store.inbox.observe([make_morpho_event(1, BLOCK, ts=990, address=VAULT.lower())], SOURCE_WS, 995.0)
    consumer.process_once()
    return consumer


def test_production_records_when_telegram_accepted_the_alert(tmp_path: Path) -> None:
    store = _store(tmp_path, baseline=900.0)
    clock = [1_000.0]

    def publish(severity: AlertSeverity, message: str) -> None:
        clock[0] += 2.0

    _sent_alert(store, publish, clock)
    (alert,) = store.alerts_since(0)
    assert alert["created_at"] == 1_000.0 and alert["sent_at"] == 1_002.0

    summary = live_summary(store, 1, 1_100.0)
    assert "block → decision: p50 10.0s" in summary
    assert "decision → Telegram: p50 2.0s, p95 2.0s" in summary
    assert "block → Telegram: p50 12.0s, p95 12.0s" in summary


def test_a_sent_production_alert_is_corrected_once_after_its_event_is_removed(tmp_path: Path) -> None:
    store = _store(tmp_path, baseline=900.0)
    clock = [1_000.0]
    publish = MagicMock()
    consumer = _sent_alert(store, publish, clock)
    publish.reset_mock()

    assert consumer.correct_reorged_alerts() == 0
    store.inbox.mark_missing(1, BLOCK, BLOCK, set(), SOURCE_HTTP, 1_001.0)

    assert consumer.correct_reorged_alerts() == 1
    severity, text = publish.call_args.args
    assert severity is AlertSeverity.LOW and "Correction to: [Yearn USDC]" in text and f"1_{BLOCK}_0" in text
    assert consumer.correct_reorged_alerts() == 0
    publish.assert_called_once()


def test_a_failed_correction_is_retried(tmp_path: Path) -> None:
    store = _store(tmp_path, baseline=900.0)
    publish = MagicMock()
    consumer = _sent_alert(store, publish, [1_000.0])
    store.inbox.mark_missing(1, BLOCK, BLOCK, set(), SOURCE_HTTP, 1_001.0)

    publish.side_effect = RuntimeError("telegram down")
    with pytest.raises(RuntimeError):
        consumer.correct_reorged_alerts()
    publish.side_effect = None
    assert consumer.correct_reorged_alerts() == 1


def test_a_rewritten_event_is_the_same_change_and_needs_no_correction(tmp_path: Path) -> None:
    store = _store(tmp_path, baseline=900.0)
    publish = MagicMock()
    consumer = _sent_alert(store, publish, [1_000.0])
    publish.reset_mock()
    rewritten = make_morpho_event(1, BLOCK, ts=990, address=VAULT.lower(), blockHash="0x" + "dd" * 32)
    store.inbox.observe([rewritten], SOURCE_HTTP, 1_001.0)

    assert consumer.correct_reorged_alerts() == 0
    publish.assert_not_called()


@pytest.mark.parametrize("changes", [{"transactionHash": "0x" + "dd" * 32}, {"argsJson": '{"id":"0x12"}'}])
def test_a_replaced_event_at_the_same_log_position_requires_correction(tmp_path: Path, changes: dict[str, str]) -> None:
    store = _store(tmp_path, baseline=900.0)
    consumer = _sent_alert(store, MagicMock(), [1_000.0])
    replacement = {**make_morpho_event(1, BLOCK, ts=990, address=VAULT.lower()), **changes}
    store.inbox.observe([replacement], SOURCE_HTTP, 1_001.0)
    # Another revision must not hide the alert's original source.
    store.inbox.observe([{**replacement, "blockHash": "0x" + "ee" * 32}], SOURCE_HTTP, 1_002.0)
    assert consumer.correct_reorged_alerts() == 1


def test_a_replacement_vault_is_used_for_live_evaluation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = make_morpho_event(1, BLOCK, address=VAULT.lower())
    replacement_vault = "0x" + "ee" * 20
    replacement = {**original, "vaultAddress": replacement_vault}
    store.inbox.observe([original], SOURCE_WS, OBSERVED - 90)
    store.inbox.observe([replacement], SOURCE_HTTP, OBSERVED - 80)
    (pending,) = store.undecided()
    assert pending.vault_address == replacement_vault


def test_a_source_transaction_reincluded_at_a_new_position_survives(tmp_path: Path) -> None:
    store = _store(tmp_path, baseline=900.0)
    consumer = _sent_alert(store, MagicMock(), [1_000.0])
    original = make_morpho_event(1, BLOCK, ts=990, address=VAULT.lower())
    moved = make_morpho_event(1, BLOCK + 1, ts=991, address=VAULT.lower(), transactionHash=original["transactionHash"])
    store.inbox.observe([moved], SOURCE_HTTP, 1_001.0)
    store.inbox.mark_missing(1, BLOCK, BLOCK, set(), SOURCE_HTTP, 1_001.0)
    assert consumer.correct_reorged_alerts() == 0


def test_reorg_corrects_only_the_message_with_the_removed_source(tmp_path: Path) -> None:
    store = _store(tmp_path, baseline=900.0)
    publish = MagicMock()
    consumer = _production(store, publish, [1_000.0])
    events = [make_morpho_event(1, block, ts=990, address=VAULT.lower()) for block in (BLOCK, BLOCK + 1)]
    store.inbox.observe(events, SOURCE_WS, 995.0)
    consumer.process_once()

    alerts = store.alerts_since(0)
    assert [json.loads(alert["event_ids"]) for alert in alerts] == [[events[0]["id"]], [events[1]["id"]]]
    publish.reset_mock()
    store.inbox.mark_missing(1, BLOCK, BLOCK, set(), SOURCE_HTTP, 1_001.0)
    assert consumer.correct_reorged_alerts() == 1
    publish.assert_called_once()
    assert "POTENTIAL VAULT LOSS" in publish.call_args.args[1]


def test_old_alerts_are_not_rechecked_for_reorgs(tmp_path: Path) -> None:
    store = _store(tmp_path, baseline=900.0)
    clock = [1_000.0]
    consumer = _sent_alert(store, MagicMock(), clock)
    store.inbox.mark_missing(1, BLOCK, BLOCK, set(), SOURCE_HTTP, 1_001.0)
    clock[0] += 2 * 86_400
    assert consumer.correct_reorged_alerts() == 0


# ── long lines ─────────────────────────────────────────────────────────


LONG_LINE = "x" * 5_073


def test_a_line_longer_than_a_telegram_message_is_wrapped_in_production_rendering() -> None:
    rendered = render_vault_alerts("[Vault]", [VaultAlert(AlertSeverity.MEDIUM, LONG_LINE)])
    assert len(rendered) == 2 and all(len(text) <= MAX_MESSAGE_LENGTH for _, text in rendered)
    assert sum(text.count("x") for _, text in rendered) == len(LONG_LINE)


def test_a_line_longer_than_a_telegram_message_is_wrapped_in_shadow_delivery() -> None:
    texts = shadow_texts(AlertRequest(1, 1, VAULT, MODE_SHADOW, "MEDIUM", LONG_LINE, ["1_1_0"], 0))
    assert len(texts) > 1 and all(len(text) <= MAX_MESSAGE_LENGTH for text in texts)
    assert sum(text.count("x") for text in texts) == len(LONG_LINE)


def test_wrapping_prefers_spaces_so_words_stay_whole() -> None:
    body = " ".join(["word"] * 2_000)
    rendered = render_vault_alerts("[Vault]", [VaultAlert(AlertSeverity.LOW, body)])
    assert all(len(text) <= MAX_MESSAGE_LENGTH for _, text in rendered)
    assert sum(text.split("\n\n", 1)[1].count("word") for _, text in rendered) == 2_000

"""Unit tests for Bedrock uniBTC state polling."""

from collections.abc import Sequence
from decimal import Decimal
from types import SimpleNamespace

import pytest

import protocols.unibtc.main as unibtc
from utils.alert import Alert, AlertSeverity


def stub_cache(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Replace unibtc cache reads and writes with an in-memory mapping."""
    cache: dict[str, str] = {}
    monkeypatch.setattr(
        unibtc,
        "get_last_value_for_key_from_file",
        lambda _filename, key: cache.get(key, 0),
    )
    monkeypatch.setattr(
        unibtc,
        "write_last_value_to_file",
        lambda _filename, key, value: cache.__setitem__(key, str(value)),
    )
    return cache


def make_state(**overrides: object) -> unibtc.UnibtcState:
    """Build a UnibtcState with snapshot-like defaults."""
    values: dict[str, object] = {
        "block_number": 25_981_132,
        "block_timestamp": 1_700_000_000,
        "total_supply": 298_112_556_288,
        "adequacy_ratio": 900,
        "chainlink_reserve_feeder": unibtc.POR_FEED,
        "unibtc_supply_feeder": unibtc.SUPPLY_FEEDER,
        "feeder_heartbeat": 86_400,
        "vault_out_of_service": False,
        "vault_paused": False,
        "router_paused": False,
        "por_answer": 4_640_515_622_996_713_140_279,
        "por_decimals": 18,
        "por_updated_at": 1_700_000_000 - 38_534,
        "feeder_supply": 384_574_449_304,
        "wbtc_total_debts": 75_152_598,
        "wbtc_total_cleared": 0,
        "vault_wbtc_balance": 46_065_725,
    }
    values.update(overrides)
    return unibtc.UnibtcState(**values)  # type: ignore[arg-type]


def make_client(responses: Sequence[object]) -> tuple[SimpleNamespace, list[object]]:
    """Build a batch-capable fake client and capture submitted calls."""
    added_calls: list[object] = []

    class ContractCall:
        def __init__(self, name: str) -> None:
            self.name = name

        def call(self, *, block_identifier: int) -> tuple[str, int]:
            return self.name, block_identifier

    class Batch:
        def __enter__(self) -> "Batch":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def add(self, call: object) -> None:
            added_calls.append(call)

        def execute(self) -> list[object]:
            return list(responses)

    class Functions:
        def totalSupply(self) -> ContractCall:
            return ContractCall("totalSupply")

        def adequacyRatio(self) -> ContractCall:
            return ContractCall("adequacyRatio")

        def chainlinkReserveFeeder(self) -> ContractCall:
            return ContractCall("chainlinkReserveFeeder")

        def uniBTCSupplyFeeder(self) -> ContractCall:
            return ContractCall("uniBTCSupplyFeeder")

        def feederHeartbeat(self) -> ContractCall:
            return ContractCall("feederHeartbeat")

        def outOfService(self) -> ContractCall:
            return ContractCall("outOfService")

        def paused(self) -> ContractCall:
            return ContractCall("paused")

        def latestRoundData(self) -> ContractCall:
            return ContractCall("latestRoundData")

        def decimals(self) -> ContractCall:
            return ContractCall("decimals")

        def totalTokenSupply(self) -> ContractCall:
            return ContractCall("totalTokenSupply")

        def tokenDebts(self, _token: str) -> ContractCall:
            return ContractCall("tokenDebts")

        def balanceOf(self, _owner: str) -> ContractCall:
            return ContractCall("balanceOf")

    contract = SimpleNamespace(functions=Functions())
    client = SimpleNamespace(
        eth=SimpleNamespace(
            block_number=25_981_132,
            get_block=lambda _n: {"timestamp": 1_700_000_000},
            contract=lambda **_kwargs: contract,
        ),
        batch_requests=Batch,
        execute_batch=lambda batch: batch.execute(),
    )
    return client, added_calls


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_mint_delta_1h_uses_latest_fresh_snapshot() -> None:
    now = 1_000_000
    snapshots = [(now - 4 * 3600, 100), (now - 3600, 110)]
    assert unibtc.mint_delta_1h(125, now, snapshots) == (15, 3600)


def test_mint_delta_1h_skips_stale_baseline() -> None:
    now = 1_000_000
    snapshots = [(now - 4 * 3600, 100)]
    assert unibtc.mint_delta_1h(200, now, snapshots) is None


def test_mint_delta_24h_picks_closest_to_one_day() -> None:
    now = 1_000_000
    snapshots = [(now - 21 * 3600, 100), (now - 24 * 3600, 90), (now - 35 * 3600, 50)]
    assert unibtc.mint_delta_24h(120, now, snapshots) == (30, 24 * 3600)


def test_mint_delta_24h_none_without_window() -> None:
    now = 1_000_000
    snapshots = [(now - 3600, 100)]
    assert unibtc.mint_delta_24h(200, now, snapshots) is None


def test_prune_snapshots_keeps_future_dated_entry() -> None:
    """A lagging RPC provider can move block_timestamp backwards; keep the snapshot."""
    now = 1_000_000
    snapshots = [(now + 600, 120), (now - 3600, 110), (now - 40 * 3600, 90)]
    assert unibtc.prune_snapshots(snapshots, now) == [(now + 600, 120), (now - 3600, 110)]


def test_mint_delta_ignores_future_dated_snapshot() -> None:
    now = 1_000_000
    snapshots = [(now + 600, 999), (now - 3600, 110)]
    assert unibtc.mint_delta_1h(125, now, snapshots) == (15, 3600)


def test_por_coverage_ratio_matches_snapshot() -> None:
    ratio = unibtc.por_coverage_ratio(4_640_515_622_996_713_140_279, 18, Decimal("4546.67793"))
    assert Decimal("1.020") < ratio < Decimal("1.021")


def test_feeder_ratio_matches_snapshot() -> None:
    ratio = unibtc.feeder_ratio(384_574_449_304, Decimal("4546.67793"))
    assert Decimal("0.845") < ratio < Decimal("0.846")


def test_feeder_ratio_drift() -> None:
    assert unibtc.feeder_ratio_drift(Decimal("0.9"), Decimal("1.0")) == Decimal("0.1")


def test_uncleared_wbtc_debt() -> None:
    state = make_state(wbtc_total_debts=75_152_598, wbtc_total_cleared=0)
    assert unibtc.uncleared_wbtc_debt(state) == 75_152_598


def test_reserve_gate_diffs_empty_on_baseline() -> None:
    assert unibtc.reserve_gate_diffs(make_state()) == []


def test_reserve_gate_diffs_lists_each_changed_field() -> None:
    state = make_state(
        adequacy_ratio=0,
        chainlink_reserve_feeder="0x0000000000000000000000000000000000000001",
        feeder_heartbeat=1,
    )
    diffs = unibtc.reserve_gate_diffs(state)
    assert len(diffs) == 3
    assert any("adequacyRatio" in line for line in diffs)
    assert any("chainlinkReserveFeeder" in line for line in diffs)
    assert any("feederHeartbeat" in line for line in diffs)


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------


def test_unexpected_minting_1h_is_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    now = 1_700_000_000
    unibtc.store_supply_snapshots([(now - 3600, 298_112_556_288)])

    unibtc.check_unexpected_minting(make_state(total_supply=298_112_556_288 + 10 * 10**8, block_timestamp=now))

    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.CRITICAL
    assert "1h" in alerts[0].message


def test_unexpected_minting_24h_is_high(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    now = 1_700_000_000
    unibtc.store_supply_snapshots([(now - 24 * 3600, 298_112_556_288)])

    unibtc.check_unexpected_minting(make_state(total_supply=298_112_556_288 + 2 * 10**8, block_timestamp=now))

    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.HIGH
    assert "24h" in alerts[0].message


def test_unexpected_minting_initializes_without_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_unexpected_minting(make_state())

    assert alerts == []
    assert unibtc.load_supply_snapshots() == [(1_700_000_000, 298_112_556_288)]


def test_unexpected_minting_24h_does_not_repeat_for_same_mint(monkeypatch: pytest.MonkeyPatch) -> None:
    """One mint stays in the 24h window for ~20 runs; it must alert only once."""
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    now = 1_700_000_000
    base = 298_112_556_288
    minted = base + 3 * 10**8
    unibtc.store_supply_snapshots([(now - 24 * 3600 - hour * 3600, base) for hour in range(6)])

    for hour in range(20):
        unibtc.check_unexpected_minting(make_state(total_supply=minted, block_timestamp=now + hour * 3600))

    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.HIGH


def test_unexpected_minting_realerts_after_further_growth(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    now = 1_700_000_000
    base = 298_112_556_288
    unibtc.store_supply_snapshots([(now - 24 * 3600, base)])

    unibtc.check_unexpected_minting(make_state(total_supply=base + 2 * 10**8, block_timestamp=now))
    unibtc.check_unexpected_minting(make_state(total_supply=base + 3 * 10**8, block_timestamp=now + 3600))
    unibtc.check_unexpected_minting(make_state(total_supply=base + 5 * 10**8, block_timestamp=now + 7200))

    assert len(alerts) == 2


def test_unexpected_minting_rearms_after_baseline_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale alert marker must not suppress a real mint after a polling gap."""
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    now = 1_700_000_000
    btc = 10**8
    base = 300 * btc
    unibtc.store_supply_snapshots([(now - 3600, base)])

    unibtc.check_unexpected_minting(make_state(total_supply=base + 11 * btc, block_timestamp=now))
    # Polling gap past the retention window, and supply falls back via redemptions.
    gap = now + 40 * 3600
    unibtc.check_unexpected_minting(make_state(total_supply=base, block_timestamp=gap))
    # A genuinely new mint, below the stale marker's level.
    unibtc.check_unexpected_minting(make_state(total_supply=base + 10 * btc, block_timestamp=gap + 3600))

    assert len(alerts) == 2
    assert all(alert.severity == AlertSeverity.CRITICAL for alert in alerts)


def test_mint_alert_due_rearms_on_missing_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = stub_cache(monkeypatch)
    cache[unibtc.CACHE_KEY_MINT_1H_ALERTED] = "31100000000"

    assert unibtc.mint_alert_due(unibtc.CACHE_KEY_MINT_1H_ALERTED, None, 30_000_000_000, 10**9) is False
    assert cache[unibtc.CACHE_KEY_MINT_1H_ALERTED] == "0"


def test_reserve_gate_alerts_on_each_distinct_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second tampered field must not be swallowed by the first alert's latch."""
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_reserve_gate(make_state(adequacy_ratio=0))
    unibtc.check_reserve_gate(make_state(adequacy_ratio=0))
    unibtc.check_reserve_gate(
        make_state(adequacy_ratio=0, unibtc_supply_feeder="0x0000000000000000000000000000000000000001")
    )

    assert len(alerts) == 2
    assert "uniBTCSupplyFeeder" in alerts[1].message


def test_paused_alerts_again_when_pause_set_shifts(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_paused(make_state(router_paused=True))
    unibtc.check_paused(make_state(router_paused=True))
    unibtc.check_paused(make_state(router_paused=True, vault_paused=True))

    assert len(alerts) == 2
    assert "Vault.paused" in alerts[1].message


def test_por_stale_follows_tightened_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    now = 1_700_000_000

    unibtc.check_por_stale(make_state(block_timestamp=now, por_updated_at=now - 3_700, feeder_heartbeat=3_600))

    assert len(alerts) == 1


def test_por_stale_threshold_capped_at_default() -> None:
    """A heartbeat widened by a compromised manager must not blind the check."""
    assert unibtc.por_stale_threshold(make_state(feeder_heartbeat=10 * 86_400)) == unibtc.POR_STALE_SECONDS
    assert unibtc.por_stale_threshold(make_state(feeder_heartbeat=0)) == unibtc.POR_STALE_SECONDS


def test_reserve_gate_alerts_once_until_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    changed = make_state(adequacy_ratio=0)

    unibtc.check_reserve_gate(changed)
    unibtc.check_reserve_gate(changed)
    unibtc.check_reserve_gate(make_state())
    unibtc.check_reserve_gate(changed)

    assert len(alerts) == 2
    assert all(alert.severity == AlertSeverity.CRITICAL for alert in alerts)


def test_paused_alerts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_paused(make_state(vault_out_of_service=True, router_paused=True))
    unibtc.check_paused(make_state(vault_out_of_service=True, router_paused=True))

    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.HIGH
    assert "outOfService" in alerts[0].message
    assert "Router.paused" in alerts[0].message


def test_por_coverage_critical_then_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    state = make_state(por_answer=4 * 10**18)

    unibtc.check_por_coverage(state, Decimal("5"))
    unibtc.check_por_coverage(state, Decimal("5"))

    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.CRITICAL


def test_por_coverage_high_then_escalates_to_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_por_coverage(make_state(por_answer=int(Decimal("4.55") * 10**18)), Decimal("4.54667793"))
    unibtc.check_por_coverage(make_state(por_answer=4 * 10**18), Decimal("5"))

    assert [alert.severity for alert in alerts] == [AlertSeverity.HIGH, AlertSeverity.CRITICAL]


def test_por_coverage_recovery_does_not_realert_high(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_por_coverage(make_state(por_answer=4 * 10**18), Decimal("5"))
    unibtc.check_por_coverage(make_state(por_answer=int(Decimal("4.55") * 10**18)), Decimal("4.54667793"))

    assert [alert.severity for alert in alerts] == [AlertSeverity.CRITICAL]


def test_por_stale_uses_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    now = 1_700_000_000

    unibtc.check_por_stale(make_state(block_timestamp=now, por_updated_at=now - 86_401))
    unibtc.check_por_stale(make_state(block_timestamp=now, por_updated_at=now - 86_401))

    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.HIGH


def test_supply_feeder_steady_state_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live feeder sits ~15% under the dashboard total; that must not alert."""
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_supply_feeder(make_state(), Decimal("4546.67793"))
    unibtc.check_supply_feeder(make_state(), Decimal("4546.67793"))
    unibtc.check_supply_feeder(make_state(feeder_supply=390_000_000_000), Decimal("4600"))

    assert alerts == []


def test_supply_feeder_ratio_jump_alerts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_supply_feeder(make_state(), Decimal("4546.67793"))
    hijacked = make_state(feeder_supply=384_574_449_304 // 2)
    unibtc.check_supply_feeder(hijacked, Decimal("4546.67793"))
    unibtc.check_supply_feeder(hijacked, Decimal("4546.67793"))

    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.HIGH
    assert "supply feeder wrong" in alerts[0].message


def test_supply_feeder_ratio_cannot_ratchet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated sub-threshold steps must not walk the anchor into unlimited drift."""
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    api = Decimal("4546.67793")
    feeder = 384_574_449_304

    unibtc.check_supply_feeder(make_state(feeder_supply=feeder), api)
    for step in range(4):
        feeder = feeder * 97 // 100
        unibtc.check_supply_feeder(
            make_state(feeder_supply=feeder, block_timestamp=1_700_000_000 + (step + 1) * 3600), api
        )

    assert len(alerts) == 1
    assert "supply feeder wrong" in alerts[0].message


def test_supply_feeder_anchor_refreshes_only_on_slow_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    cache = stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    api = Decimal("4546.67793")
    now = 1_700_000_000
    nudged = 384_574_449_304 * 98 // 100

    unibtc.check_supply_feeder(make_state(block_timestamp=now), api)
    anchor = cache[unibtc.CACHE_KEY_FEEDER_RATIO]
    unibtc.check_supply_feeder(make_state(feeder_supply=nudged, block_timestamp=now + 3600), api)
    assert cache[unibtc.CACHE_KEY_FEEDER_RATIO] == anchor, "in-band reading moved a fresh anchor"

    unibtc.check_supply_feeder(
        make_state(feeder_supply=nudged, block_timestamp=now + unibtc.FEEDER_ANCHOR_REFRESH_SECONDS), api
    )
    assert cache[unibtc.CACHE_KEY_FEEDER_RATIO] != anchor, "anchor never refreshes"
    # Advancing 7 days leaves feeder_supply unchanged, which legitimately trips the
    # separate 48h staleness check; only the ratio check must stay quiet here.
    assert not any("supply feeder wrong" in alert.message for alert in alerts)


def test_supply_feeder_baseline_not_relearned_while_alerting(monkeypatch: pytest.MonkeyPatch) -> None:
    """An anomalous ratio must not quietly become the new baseline."""
    alerts: list[Alert] = []
    cache = stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_supply_feeder(make_state(), Decimal("4546.67793"))
    baseline = cache[unibtc.CACHE_KEY_FEEDER_RATIO]
    unibtc.check_supply_feeder(make_state(feeder_supply=384_574_449_304 // 2), Decimal("4546.67793"))

    assert cache[unibtc.CACHE_KEY_FEEDER_RATIO] == baseline
    assert len(alerts) == 1


def test_supply_feeder_stale_after_48h(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    cache = stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    now = 1_700_000_000
    state = make_state(block_timestamp=now, feeder_supply=100)

    unibtc.check_supply_feeder(state, None)
    unibtc.check_supply_feeder(make_state(block_timestamp=now + 48 * 3600 + 1, feeder_supply=100), None)

    assert len(alerts) == 1
    assert "stale" in alerts[0].message
    assert cache[unibtc.CACHE_KEY_FEEDER_VALUE] == "100"


def test_redemptions_underfunded_waits_24h_and_growth(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    now = 1_700_000_000
    underfunded = make_state(
        block_timestamp=now,
        wbtc_total_debts=75_152_598,
        wbtc_total_cleared=0,
        vault_wbtc_balance=46_065_725,
    )

    unibtc.check_redemptions_underfunded(underfunded)
    unibtc.check_redemptions_underfunded(
        make_state(
            block_timestamp=now + 24 * 3600 + 1,
            wbtc_total_debts=75_152_598,
            wbtc_total_cleared=0,
            vault_wbtc_balance=46_065_725,
        )
    )
    unibtc.check_redemptions_underfunded(
        make_state(
            block_timestamp=now + 24 * 3600 + 1,
            wbtc_total_debts=80_000_000,
            wbtc_total_cleared=0,
            vault_wbtc_balance=46_065_725,
        )
    )

    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.HIGH
    assert "underfunded" in alerts[0].message


def test_redemptions_recover_rearms(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    now = 1_700_000_000

    unibtc.check_redemptions_underfunded(
        make_state(block_timestamp=now, wbtc_total_debts=80, wbtc_total_cleared=0, vault_wbtc_balance=10)
    )
    unibtc.check_redemptions_underfunded(
        make_state(
            block_timestamp=now + 24 * 3600 + 1,
            wbtc_total_debts=90,
            wbtc_total_cleared=0,
            vault_wbtc_balance=10,
        )
    )
    unibtc.check_redemptions_underfunded(
        make_state(block_timestamp=now + 25 * 3600, wbtc_total_debts=5, wbtc_total_cleared=0, vault_wbtc_balance=10)
    )
    unibtc.check_redemptions_underfunded(
        make_state(block_timestamp=now + 26 * 3600, wbtc_total_debts=80, wbtc_total_cleared=0, vault_wbtc_balance=10)
    )
    unibtc.check_redemptions_underfunded(
        make_state(
            block_timestamp=now + 50 * 3600 + 1,
            wbtc_total_debts=90,
            wbtc_total_cleared=0,
            vault_wbtc_balance=10,
        )
    )

    assert len(alerts) == 2


def test_peg_alerts_below_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_peg(Decimal("0.979"))
    unibtc.check_peg(Decimal("0.979"))
    unibtc.check_peg(Decimal("0.99"))
    unibtc.check_peg(Decimal("0.97"))

    assert len(alerts) == 2
    assert all(alert.severity == AlertSeverity.HIGH for alert in alerts)


def test_peg_skips_missing_price(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_peg(None)

    assert alerts == []


def test_fetch_api_total_supply_parses_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        unibtc,
        "fetch_json",
        lambda _url: {"code": 200, "data": {"total_supply": "4546.67793"}},
    )
    assert unibtc.fetch_api_total_supply() == Decimal("4546.67793")


def test_fetch_price_in_btc_prefers_coingecko_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        unibtc,
        "fetch_prices",
        lambda _keys: {
            "coingecko:universal-btc": Decimal("59545"),
            unibtc.BTC_USD_DEFILLAMA_KEY: Decimal("60000"),
        },
    )
    assert unibtc.fetch_price_in_btc() == Decimal("59545") / Decimal("60000")


def test_fetch_price_in_btc_skips_zero_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero quote is a bad feed, not a depeg — fall through to the next key."""
    errors: list[str] = []
    monkeypatch.setattr(unibtc, "send_error_message", lambda message, _protocol: errors.append(message))
    monkeypatch.setattr(
        unibtc,
        "fetch_prices",
        lambda _keys: {
            "coingecko:universal-btc": Decimal("0"),
            f"ethereum:{unibtc.UNIBTC}": Decimal("59545"),
            unibtc.BTC_USD_DEFILLAMA_KEY: Decimal("60000"),
        },
    )

    assert unibtc.fetch_price_in_btc() == Decimal("59545") / Decimal("60000")
    assert errors == []


def test_fetch_price_in_btc_none_when_all_quotes_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(unibtc, "send_error_message", lambda _message, _protocol: None)
    monkeypatch.setattr(
        unibtc,
        "fetch_prices",
        lambda _keys: {
            "coingecko:universal-btc": Decimal("0"),
            unibtc.BTC_USD_DEFILLAMA_KEY: Decimal("60000"),
        },
    )

    assert unibtc.fetch_price_in_btc() is None


def test_load_state_batches_all_calls_at_one_block() -> None:
    por_round = (1, 4_640_515_622_996_713_140_279, 1, 1_699_961_466, 1)
    responses = [
        298_112_556_288,
        900,
        unibtc.POR_FEED,
        unibtc.SUPPLY_FEEDER,
        86_400,
        False,
        False,
        False,
        por_round,
        18,
        384_574_449_304,
        (75_152_598, 0),
        46_065_725,
    ]
    client, added_calls = make_client(responses)

    state = unibtc.load_state(client)

    assert [call[0] for call in added_calls] == [
        "totalSupply",
        "adequacyRatio",
        "chainlinkReserveFeeder",
        "uniBTCSupplyFeeder",
        "feederHeartbeat",
        "outOfService",
        "paused",
        "paused",
        "latestRoundData",
        "decimals",
        "totalTokenSupply",
        "tokenDebts",
        "balanceOf",
    ]
    assert all(call[1] == 25_981_132 for call in added_calls)
    assert state.total_supply == 298_112_556_288
    assert state.wbtc_total_debts == 75_152_598
    assert state.vault_wbtc_balance == 46_065_725


def test_main_runs_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    state = make_state()
    observed: list[str] = []
    monkeypatch.setattr(unibtc.ChainManager, "get_client", lambda _chain: object())
    monkeypatch.setattr(unibtc, "load_state", lambda _client: state)
    monkeypatch.setattr(unibtc, "fetch_api_total_supply", lambda: Decimal("4546.67793"))
    monkeypatch.setattr(unibtc, "fetch_price_in_btc", lambda: Decimal("0.992417"))
    monkeypatch.setattr(unibtc, "check_unexpected_minting", lambda _state: observed.append("mint"))
    monkeypatch.setattr(unibtc, "check_reserve_gate", lambda _state: observed.append("gate"))
    monkeypatch.setattr(unibtc, "check_paused", lambda _state: observed.append("pause"))
    monkeypatch.setattr(unibtc, "check_por_coverage", lambda _state, _api: observed.append("por"))
    monkeypatch.setattr(unibtc, "check_por_stale", lambda _state: observed.append("stale"))
    monkeypatch.setattr(unibtc, "check_supply_feeder", lambda _state, _api: observed.append("feeder"))
    monkeypatch.setattr(unibtc, "check_redemptions_underfunded", lambda _state: observed.append("redeem"))
    monkeypatch.setattr(unibtc, "check_peg", lambda _price: observed.append("peg"))

    unibtc.main()

    assert observed == ["mint", "gate", "pause", "por", "stale", "feeder", "redeem", "peg"]

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


# Bedrock API per-chain supplies captured 2026-09-16 09:20Z, before the API dropped BOB.
API_SNAPSHOT_SUPPLIES = (
    ("Ethereum-uniBTC", 1, "2981.445271"),
    ("Optimism-uniBTC", 10, "3.66435741"),
    ("RootStock-uniBTC", 30, "1.6561039"),
    ("Binance-uniBTC", 56, "187.2519388"),
    ("B2-uniBTC", 223, "5.66818735"),
    ("Tac-uniBTC", 239, "0.11673062"),
    ("Merlin-uniBTC", 4200, "4.76274063"),
    ("IoTeX-uniBTC", 4689, "0.0011"),
    ("Mantle-uniBTC", 5000, "3.35804978"),
    ("Zeta-uniBTC", 7000, "0.02461641"),
    ("Base-uniBTC", 8453, "496.3873782"),
    ("Arbitrum-uniBTC", 42161, "1.30307482"),
    ("Hemi-uniBTC", 43111, "0.111336"),
    ("BOB-uniBTC", 60808, "701.5560332"),
    ("Bera-uniBTC", 80094, "151.6242213"),
    ("Taiko-uniBTC", 167000, "0.042935"),
    ("Aptos-uniBTC", 981141, "0.15403966"),
    ("Solana-uniBTC", 98114115, "7.57326742"),
)


def make_api(
    *,
    updated_at: int = 1_700_000_000,
    drop_chain: int | None = None,
    zero_chain: int | None = None,
) -> unibtc.ApiStats:
    """Build ApiStats from the 2026-09-16 snapshot, optionally dropping or zeroing a chain.

    ``total_supply`` is recomputed from the kept entries, matching how the real API
    understated its total when it dropped BOB.
    """
    supplies = tuple(
        unibtc.ChainSupply(name, chain_id, Decimal("0") if chain_id == zero_chain else Decimal(supply))
        for name, chain_id, supply in API_SNAPSHOT_SUPPLIES
        if chain_id != drop_chain
    )
    total = sum((entry.supply for entry in supplies), Decimal("0"))
    return unibtc.ApiStats(total_supply=total, updated_at=updated_at, chain_supplies=supplies)


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


def test_feeder_shortfall_matches_snapshot() -> None:
    """2026-09-16: feeder 3,845.33 vs API 4,546.70 — the feeder omits BOB."""
    shortfall = unibtc.feeder_shortfall(384_533_051_367, Decimal("4546.701382"))
    assert shortfall == Decimal("701.37086833")


def test_chain_matching_gap_names_bob_on_snapshot() -> None:
    match = unibtc.chain_matching_gap(Decimal("701.37086833"), make_api().chain_supplies)
    assert match is not None
    assert match.chain_id == 60808


def test_chain_matching_gap_none_when_no_chain_fits() -> None:
    assert unibtc.chain_matching_gap(Decimal("300"), make_api().chain_supplies) is None


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


class _FlakySender:
    """send_alert stand-in that raises for the first ``failures`` calls, like a Telegram outage."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.delivered: list[Alert] = []

    def __call__(self, alert: Alert) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("simulated delivery failure")
        self.delivered.append(alert)


def test_unexpected_minting_retries_after_delivery_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed send must not mark the mint as alerted (PR #362 review)."""
    stub_cache(monkeypatch)
    sender = _FlakySender(failures=1)
    monkeypatch.setattr(unibtc, "send_alert", sender)
    now = 1_700_000_000
    base = 300 * 10**8
    minted = base + 10 * 10**8
    unibtc.store_supply_snapshots([(now - 3600, base), (now - 24 * 3600, base)])

    with pytest.raises(RuntimeError):
        unibtc.check_unexpected_minting(make_state(total_supply=minted, block_timestamp=now))
    unibtc.check_unexpected_minting(make_state(total_supply=minted, block_timestamp=now + 600))

    assert [alert.severity for alert in sender.delivered] == [AlertSeverity.CRITICAL, AlertSeverity.HIGH]


def test_unexpected_minting_second_failure_keeps_first_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """1h delivered, 24h failed: only the 24h alert is retried."""
    stub_cache(monkeypatch)
    sender = _FlakySender(failures=0)
    now = 1_700_000_000
    base = 300 * 10**8
    minted = base + 10 * 10**8
    unibtc.store_supply_snapshots([(now - 3600, base), (now - 24 * 3600, base)])

    def fail_24h(alert: Alert) -> None:
        if "24h" in alert.message:
            raise RuntimeError("simulated delivery failure")
        sender(alert)

    monkeypatch.setattr(unibtc, "send_alert", fail_24h)
    with pytest.raises(RuntimeError):
        unibtc.check_unexpected_minting(make_state(total_supply=minted, block_timestamp=now))
    monkeypatch.setattr(unibtc, "send_alert", sender)
    unibtc.check_unexpected_minting(make_state(total_supply=minted, block_timestamp=now + 600))

    assert [alert.severity for alert in sender.delivered] == [AlertSeverity.CRITICAL, AlertSeverity.HIGH]


def test_mint_alert_due_does_not_record_send(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = stub_cache(monkeypatch)

    assert unibtc.mint_alert_due(unibtc.CACHE_KEY_MINT_1H_ALERTED, 10**9, 31_000_000_000, 10**9) is True
    assert unibtc.CACHE_KEY_MINT_1H_ALERTED not in cache


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


def test_supply_feeder_healthy_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A feeder tracking API supply (as through 2026-09-12) must not alert."""
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_supply_feeder(make_state(feeder_supply=454_649_306_457), make_api())

    assert alerts == []


def test_supply_feeder_gap_alerts_once_and_names_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2026-09-16 fault: feeder 3,845.33 vs API 4,546.70."""
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    broken = make_state(feeder_supply=384_533_051_367)

    unibtc.check_supply_feeder(broken, make_api())
    unibtc.check_supply_feeder(broken, make_api())

    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.HIGH
    assert "supply feeder wrong" in alerts[0].message
    assert "below" in alerts[0].message
    assert "BOB-uniBTC (chain 60808)" in alerts[0].message
    assert "omits" in alerts[0].message


def test_supply_feeder_gap_without_matching_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_supply_feeder(make_state(feeder_supply=430_000_000_000), make_api())

    assert len(alerts) == 1
    assert "No single chain's supply matches the gap" in alerts[0].message


def test_supply_feeder_gap_rearms_after_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_supply_feeder(make_state(feeder_supply=384_533_051_367), make_api())
    unibtc.check_supply_feeder(make_state(feeder_supply=454_649_306_457), make_api())
    unibtc.check_supply_feeder(make_state(feeder_supply=384_533_051_367), make_api())

    wrong = [alert for alert in alerts if "supply feeder wrong" in alert.message]
    assert len(wrong) == 2


def test_supply_feeder_gap_skipped_without_api(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_supply_feeder(make_state(feeder_supply=384_533_051_367), None)

    assert alerts == []


# ---------------------------------------------------------------------------
# API safeguards
# ---------------------------------------------------------------------------


def test_api_snapshot_passes_safeguards() -> None:
    assert unibtc.api_stats_problems(make_api(), make_state()) == []


def test_api_rejected_when_required_chain_missing() -> None:
    """The observed 2026-09-16 failure: BOB dropped from supplies, total understated."""
    api = make_api(drop_chain=60808)

    problems = unibtc.api_stats_problems(api, make_state())

    assert problems == ["BOB (chain 60808) missing from supplies"]


def test_api_rejected_when_required_chain_zero() -> None:
    problems = unibtc.api_stats_problems(make_api(zero_chain=8453), make_state())
    assert problems == ["Base (chain 8453) supply is 0"]


def test_api_rejected_when_stale() -> None:
    state = make_state()
    api = make_api(updated_at=state.block_timestamp - unibtc.API_MAX_AGE_SECONDS - 1)

    problems = unibtc.api_stats_problems(api, state)

    assert len(problems) == 1
    assert "old" in problems[0]


def test_api_accepts_timestamp_slightly_ahead_of_block() -> None:
    state = make_state()
    assert unibtc.api_stats_problems(make_api(updated_at=state.block_timestamp + 30), state) == []


def test_api_rejected_when_total_excludes_listed_chain() -> None:
    """Every required chain listed, Ethereum matches, but total omits BOB (PR #362 review)."""
    full = make_api()
    bob = next(entry.supply for entry in full.chain_supplies if entry.chain_id == 60808)
    api = unibtc.ApiStats(
        total_supply=full.total_supply - bob,
        updated_at=full.updated_at,
        chain_supplies=full.chain_supplies,
    )

    problems = unibtc.api_stats_problems(api, make_state())

    assert len(problems) == 1
    assert "differs from per-chain sum" in problems[0]


def test_api_accepts_rounding_between_total_and_sum() -> None:
    """Live totals differed from the per-chain sum by 4.2e-7 uniBTC."""
    full = make_api()
    api = unibtc.ApiStats(
        total_supply=full.total_supply + Decimal("0.00000042"),
        updated_at=full.updated_at,
        chain_supplies=full.chain_supplies,
    )
    assert unibtc.api_stats_problems(api, make_state()) == []


def test_api_rejected_when_mainnet_disagrees_with_chain() -> None:
    state = make_state(total_supply=250_000_000_000)

    problems = unibtc.api_stats_problems(make_api(), state)

    assert len(problems) == 1
    assert "differs from on-chain totalSupply" in problems[0]


def test_validate_api_stats_reports_and_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    errors: list[str] = []
    monkeypatch.setattr(unibtc, "send_error_message", lambda message, _protocol: errors.append(message))

    assert unibtc.validate_api_stats(make_api(drop_chain=60808), make_state()) is None
    assert len(errors) == 1
    assert "BOB (chain 60808) missing" in errors[0]


def test_validate_api_stats_passes_through_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(unibtc, "send_error_message", lambda _message, _protocol: pytest.fail("unexpected error"))
    api = make_api()
    assert unibtc.validate_api_stats(api, make_state()) is api
    assert unibtc.validate_api_stats(None, make_state()) is None


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


def test_supply_feeder_zero_value_still_goes_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """A feeder pinned at 0 must not read as a fresh observation every run.

    Zero is what the cache returns for an unset key, and it is also the single most
    dangerous reading: it satisfies the Vault mint gate outright. With the API down
    the ratio check is skipped, so staleness is the only thing watching.
    """
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)
    now = 1_700_000_000

    for day in range(4):
        unibtc.check_supply_feeder(make_state(feeder_supply=0, block_timestamp=now + day * 86_400), None)

    stale = [alert for alert in alerts if "supply feeder stale" in alert.message]
    assert len(stale) == 1


def test_feeder_zero_is_critical_on_first_run_without_api(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_supply_feeder(make_state(feeder_supply=0), None)
    unibtc.check_supply_feeder(make_state(feeder_supply=0, block_timestamp=1_700_003_600), None)

    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.CRITICAL
    assert "reports zero" in alerts[0].message


def test_feeder_zero_rearms_after_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_feeder_zero(make_state(feeder_supply=0))
    unibtc.check_feeder_zero(make_state())
    unibtc.check_feeder_zero(make_state(feeder_supply=0))

    assert [alert.severity for alert in alerts] == [AlertSeverity.CRITICAL, AlertSeverity.CRITICAL]


def test_feeder_zero_quiet_for_normal_supply(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_feeder_zero(make_state())

    assert alerts == []


def test_load_state_rejects_truncated_batch() -> None:
    client, _ = make_client([298_112_556_288, 900])

    with pytest.raises(RuntimeError, match="2 of 13 expected responses"):
        unibtc.load_state(client)


def test_batch_call_count_matches_load_state() -> None:
    """BATCH_CALL_COUNT is asserted against the real batch so it cannot drift."""
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

    unibtc.load_state(client)

    assert len(added_calls) == unibtc.BATCH_CALL_COUNT


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


@pytest.mark.parametrize(
    ("price", "band"),
    [
        (Decimal("0.9942"), "ok"),
        (Decimal("0.985"), "ok"),
        (Decimal("0.9849"), "high"),
        (Decimal("0.97"), "high"),
        (Decimal("0.9699"), "critical"),
    ],
)
def test_peg_band_boundaries(price: Decimal, band: str) -> None:
    assert unibtc.severity_band(price, unibtc.PEG_CRITICAL_FLOOR, unibtc.PEG_HIGH_FLOOR) == band


def test_peg_quiet_at_current_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """0.9942 was the live price on 2026-09-16 and sits in the normal range."""
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_peg(Decimal("0.994160279174270683"))

    assert alerts == []


def test_peg_high_once_then_escalates_to_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_peg(Decimal("0.98"))
    unibtc.check_peg(Decimal("0.978"))
    unibtc.check_peg(Decimal("0.9616"))
    unibtc.check_peg(Decimal("0.9616"))

    assert [alert.severity for alert in alerts] == [AlertSeverity.HIGH, AlertSeverity.CRITICAL]
    assert "0.985 WBTC" in alerts[0].message
    assert "0.97 WBTC" in alerts[1].message


def test_peg_partial_recovery_is_quiet_full_recovery_rearms(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_peg(Decimal("0.96"))
    unibtc.check_peg(Decimal("0.98"))
    unibtc.check_peg(Decimal("0.995"))
    unibtc.check_peg(Decimal("0.98"))

    assert [alert.severity for alert in alerts] == [AlertSeverity.CRITICAL, AlertSeverity.HIGH]


def test_peg_skips_missing_price(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts: list[Alert] = []
    stub_cache(monkeypatch)
    monkeypatch.setattr(unibtc, "send_alert", alerts.append)

    unibtc.check_peg(None)

    assert alerts == []


def test_fetch_api_stats_parses_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        unibtc,
        "fetch_json",
        lambda _url: {
            "code": 200,
            "data": {
                "time": 1_789_550_408_224,
                "total_supply": "4546.701382",
                "supplies": [
                    {"chain_id": 1, "name": "Ethereum-uniBTC", "supply": "2981.445271"},
                    {"chain_id": 60808, "name": "BOB-uniBTC", "supply": "701.5560332"},
                    {"chain_id": 8453, "name": "broken"},
                ],
            },
        },
    )

    stats = unibtc.fetch_api_stats()

    assert stats is not None
    assert stats.total_supply == Decimal("4546.701382")
    assert stats.updated_at == 1_789_550_408
    assert [entry.chain_id for entry in stats.chain_supplies] == [1, 60808]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"code": 500},
        {"data": {"total_supply": "4546.7"}},
        {"data": {"time": 1, "total_supply": "0", "supplies": []}},
        {"data": {"time": 1, "total_supply": "4546.7"}},
    ],
)
def test_fetch_api_stats_rejects_malformed(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    errors: list[str] = []
    monkeypatch.setattr(unibtc, "fetch_json", lambda _url: payload)
    monkeypatch.setattr(unibtc, "send_error_message", lambda message, _protocol: errors.append(message))

    assert unibtc.fetch_api_stats() is None
    assert len(errors) == 1


def test_fetch_price_in_wbtc_prefers_coingecko_key(monkeypatch: pytest.MonkeyPatch) -> None:
    requested_keys: list[str] = []

    def prices_for_keys(keys: list[str]) -> dict[str, Decimal]:
        requested_keys.extend(keys)
        return {
            "coingecko:universal-btc": Decimal("59545"),
            unibtc.WBTC_PRICE_KEY: Decimal("59000"),
        }

    monkeypatch.setattr(unibtc, "fetch_prices", prices_for_keys)

    assert unibtc.fetch_price_in_wbtc() == Decimal("59545") / Decimal("59000")
    assert requested_keys == [*unibtc.UNIBTC_PRICE_KEYS, unibtc.WBTC_PRICE_KEY]


def test_fetch_price_in_wbtc_skips_zero_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero uniBTC quote is a bad feed, so use the Ethereum token quote."""
    errors: list[str] = []
    monkeypatch.setattr(unibtc, "send_error_message", lambda message, _protocol: errors.append(message))
    monkeypatch.setattr(
        unibtc,
        "fetch_prices",
        lambda _keys: {
            "coingecko:universal-btc": Decimal("0"),
            f"ethereum:{unibtc.UNIBTC}": Decimal("59545"),
            unibtc.WBTC_PRICE_KEY: Decimal("59000"),
        },
    )

    assert unibtc.fetch_price_in_wbtc() == Decimal("59545") / Decimal("59000")
    assert errors == []


def test_fetch_price_in_wbtc_none_when_all_quotes_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(unibtc, "send_error_message", lambda _message, _protocol: None)
    monkeypatch.setattr(
        unibtc,
        "fetch_prices",
        lambda _keys: {
            "coingecko:universal-btc": Decimal("0"),
            unibtc.WBTC_PRICE_KEY: Decimal("59000"),
        },
    )

    assert unibtc.fetch_price_in_wbtc() is None


@pytest.mark.parametrize("wbtc_usd", [None, Decimal("0"), Decimal("-1")])
def test_fetch_price_in_wbtc_skips_invalid_reference(monkeypatch: pytest.MonkeyPatch, wbtc_usd: Decimal | None) -> None:
    errors: list[str] = []
    monkeypatch.setattr(unibtc, "send_error_message", lambda message, _protocol: errors.append(message))
    monkeypatch.setattr(
        unibtc,
        "fetch_prices",
        lambda _keys: {"coingecko:universal-btc": Decimal("59545"), unibtc.WBTC_PRICE_KEY: wbtc_usd},
    )

    assert unibtc.fetch_price_in_wbtc() is None
    assert errors == ["WBTC/USD price unavailable from DeFiLlama"]


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


def _stub_main(monkeypatch: pytest.MonkeyPatch, api: unibtc.ApiStats) -> dict[str, object]:
    """Stub main()'s I/O and record what the API-dependent checks receive."""
    state = make_state()
    received: dict[str, object] = {"observed": []}
    observed = received["observed"]
    assert isinstance(observed, list)
    monkeypatch.setattr(unibtc.ChainManager, "get_client", lambda _chain: object())
    monkeypatch.setattr(unibtc, "load_state", lambda _client: state)
    monkeypatch.setattr(unibtc, "fetch_api_stats", lambda: api)
    monkeypatch.setattr(unibtc, "send_error_message", lambda _message, _protocol: None)
    monkeypatch.setattr(unibtc, "fetch_price_in_wbtc", lambda: Decimal("0.992417"))
    monkeypatch.setattr(unibtc, "check_unexpected_minting", lambda _state: observed.append("mint"))
    monkeypatch.setattr(unibtc, "check_reserve_gate", lambda _state: observed.append("gate"))
    monkeypatch.setattr(unibtc, "check_paused", lambda _state: observed.append("pause"))

    def por(_state: object, supply: object) -> None:
        received["por_supply"] = supply
        observed.append("por")

    def feeder(_state: object, feeder_api: object) -> None:
        received["feeder_api"] = feeder_api
        observed.append("feeder")

    monkeypatch.setattr(unibtc, "check_por_coverage", por)
    monkeypatch.setattr(unibtc, "check_por_stale", lambda _state: observed.append("stale"))
    monkeypatch.setattr(unibtc, "check_supply_feeder", feeder)
    monkeypatch.setattr(unibtc, "check_redemptions_underfunded", lambda _state: observed.append("redeem"))
    monkeypatch.setattr(unibtc, "check_peg", lambda _price: observed.append("peg"))
    return received


def test_main_runs_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    api = make_api()
    received = _stub_main(monkeypatch, api)

    unibtc.main()

    assert received["observed"] == ["mint", "gate", "pause", "por", "stale", "feeder", "redeem", "peg"]
    assert received["por_supply"] == api.total_supply
    assert received["feeder_api"] is api


def test_main_withholds_rejected_api_from_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response missing BOB must reach neither PoR coverage nor the feeder gap check."""
    received = _stub_main(monkeypatch, make_api(drop_chain=60808))

    unibtc.main()

    assert received["por_supply"] is None
    assert received["feeder_api"] is None

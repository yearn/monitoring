from decimal import localcontext
from types import SimpleNamespace

import pytest

import protocols.cap.liquidity as liquidity
from utils.alert import Alert, AlertSeverity

type RpcCall = tuple[str, tuple[object, ...], int]


def run_monitor(
    monkeypatch: pytest.MonkeyPatch,
    asset_responses: list[object],
    *,
    current_supply: int = 1_000_000 * 10**18,
    previous_supply: int = 0,
) -> tuple[list[Alert], list[RpcCall], list[tuple[object, ...]]]:
    """Run the monitor with mocked RPC and cache, capturing calls and alerts."""
    alerts: list[Alert] = []
    calls: list[RpcCall] = []
    writes: list[tuple[object, ...]] = []
    assets = [f"asset-{i}" for i in range(len(asset_responses) // 5)]
    vault_responses: list[object] = [f"vault-{i}" for i in range(len(assets))]
    batches = iter([vault_responses, asset_responses])

    class ContractCall:
        def __init__(self, name: str, args: tuple[object, ...]) -> None:
            self.name = name
            self.args = args

        def call(self, *, block_identifier: int) -> object:
            call = (self.name, self.args, block_identifier)
            if self.name in {"assets", "totalSupply"}:
                calls.append(call)
                return assets if self.name == "assets" else current_supply
            return call

    class Batch:
        def __enter__(self) -> "Batch":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def add(self, call: RpcCall) -> None:
            calls.append(call)

        def execute(self) -> list[object]:
            return next(batches)

    functions = SimpleNamespace(
        **{
            name: lambda *args, name=name: ContractCall(name, args)
            for name in [
                "assets",
                "fractionalReserveVault",
                "maxWithdraw",
                "balanceOf",
                "decimals",
                "symbol",
                "totalSupplies",
                "totalSupply",
            ]
        }
    )
    contract = SimpleNamespace(functions=functions)
    client = SimpleNamespace(
        eth=SimpleNamespace(block_number=23_456_789, contract=lambda **_kwargs: contract),
        batch_requests=Batch,
    )
    monkeypatch.setattr(liquidity.ChainManager, "get_client", lambda _chain: client)
    monkeypatch.setattr(liquidity, "send_alert", alerts.append)
    monkeypatch.setattr(liquidity, "get_fresh_last_value_for_key_from_file", lambda *_args: previous_supply)
    monkeypatch.setattr(liquidity, "write_last_value_with_timestamp_to_file", lambda *args: writes.append(args))

    liquidity.main()

    return alerts, calls, writes


@pytest.mark.parametrize("precision", [18, 28])
@pytest.mark.parametrize(
    ("withdrawable_raw", "expected_severity"),
    [
        (0, AlertSeverity.CRITICAL),
        (3 * 10**18 - 1, AlertSeverity.CRITICAL),
        (3 * 10**18, AlertSeverity.HIGH),
        (3 * 10**18 + 1, AlertSeverity.HIGH),
        (10 * 10**18 - 1, AlertSeverity.HIGH),
        (10 * 10**18, None),
        (10 * 10**18 + 1, None),
        (100 * 10**18, None),
    ],
)
def test_liquidity_severity_boundaries(
    monkeypatch: pytest.MonkeyPatch, withdrawable_raw: int, expected_severity: AlertSeverity | None, precision: int
) -> None:
    with localcontext(prec=precision) as context:
        alerts, _, _ = run_monitor(monkeypatch, [withdrawable_raw, 0, 18, "USDC", 100 * 10**18])
        assert context.prec == precision

    assert [alert.severity for alert in alerts] == ([] if expected_severity is None else [expected_severity])
    assert all(alert.protocol == "cap" for alert in alerts)


def test_liquidity_aggregates_assets_with_different_decimals_at_one_block(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts, calls, writes = run_monitor(
        monkeypatch,
        [
            2_000_000,
            1_000_000,
            6,
            "USDC",
            40_000_000,
            2 * 10**18,
            10**18,
            18,
            "wWTGXX",
            60 * 10**18,
        ],
    )

    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.HIGH
    assert "USDC: 3.000000" in alerts[0].message
    assert "wWTGXX: 3.000000" in alerts[0].message
    assert "Total withdrawable: 6.000000" in alerts[0].message
    assert "Total TVL: 100.000000" in alerts[0].message
    assert "Withdrawable / TVL: 6.00%" in alerts[0].message
    assert {call[2] for call in calls} == {23_456_789}
    assert [call[1] for call in calls if call[0] == "totalSupplies"] == [("asset-0",), ("asset-1",)]
    assert writes == [(liquidity.cache_filename, liquidity.CACHE_KEY_LAST_SUPPLY, 1_000_000 * 10**18)]


@pytest.mark.parametrize(
    ("withdrawable", "tvl", "expected_severity"),
    [
        (6_000_000, 20_000_000, None),
        (20_000_000, 1_000_000_000, AlertSeverity.CRITICAL),
    ],
)
def test_liquidity_threshold_scales_with_tvl(
    monkeypatch: pytest.MonkeyPatch, withdrawable: int, tvl: int, expected_severity: AlertSeverity | None
) -> None:
    alerts, _, _ = run_monitor(monkeypatch, [withdrawable * 10**6, 0, 6, "USDC", tvl * 10**6])

    assert [alert.severity for alert in alerts] == ([] if expected_severity is None else [expected_severity])


@pytest.mark.parametrize("asset_responses", [[], [0, 0, 6, "USDC", 0]])
def test_zero_tvl_skips_liquidity_alert_and_updates_supply(
    monkeypatch: pytest.MonkeyPatch, asset_responses: list[object]
) -> None:
    alerts, _, writes = run_monitor(monkeypatch, asset_responses)

    assert alerts == []
    assert writes == [(liquidity.cache_filename, liquidity.CACHE_KEY_LAST_SUPPLY, 1_000_000 * 10**18)]


def test_missing_total_supplies_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="USDC totalSupplies"):
        run_monitor(monkeypatch, [1_000_000, 0, 6, "USDC", None])


def test_large_mint_monitoring_still_runs_with_zero_tvl(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts, _, writes = run_monitor(
        monkeypatch,
        [0, 0, 6, "USDC", 0],
        current_supply=105 * 10**18,
        previous_supply=100 * 10**18,
    )

    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.LOW
    assert "Supply increase: 5.00 cUSD" in alerts[0].message
    assert writes == [(liquidity.cache_filename, liquidity.CACHE_KEY_LAST_SUPPLY, 105 * 10**18)]

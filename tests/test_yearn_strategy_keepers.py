from decimal import Decimal
from types import SimpleNamespace

import pytest
from web3 import Web3

from protocols.yearn import check_strategy_keepers as mod
from utils.chains import Chain

ETH_KEY = "coingecko:ethereum"
STRATEGY_A = Web3.to_checksum_address("0x00000000000000000000000000000000000000a1")
STRATEGY_B = Web3.to_checksum_address("0x00000000000000000000000000000000000000a2")
STRATEGY_C = Web3.to_checksum_address("0x00000000000000000000000000000000000000a3")
KEEPER_EOA = Web3.to_checksum_address("0x00000000000000000000000000000000000000e1")
KEEPER_CONTRACT = Web3.to_checksum_address("0x00000000000000000000000000000000000000c1")


def test_select_keeper_strategies_accepts_v2_names_and_deduplicates() -> None:
    selected = mod.select_keeper_strategies(
        [
            {"chain_id": 1, "address": STRATEGY_A, "name": "StrategyCurveBoostedFactory"},
            {"chain_id": 1, "address": STRATEGY_A.lower(), "name": "StrategyCurveBoostedFactory"},
            {"chain_id": 10, "address": STRATEGY_B, "name": "StrategyVelodrome"},
            {"chain_id": 8453, "address": STRATEGY_C, "name": "StrategyAerodrome"},
            {"chain_id": 42161, "address": STRATEGY_A, "name": "StrategyArbitrum"},
            {"chain_id": 250, "address": STRATEGY_A, "name": "Unsupported"},
        ]
    )
    assert {chain: [strategy.address for strategy in items] for chain, items in selected.items()} == {
        Chain.MAINNET: [STRATEGY_A],
        Chain.OPTIMISM: [STRATEGY_B],
        Chain.BASE: [STRATEGY_C],
        Chain.ARBITRUM: [STRATEGY_A],
    }


class FakeBatch:
    def __init__(self, client: "FakeClient") -> None:
        self.calls: list = []
        self._client = client

    def __enter__(self) -> "FakeBatch":
        self._client.in_batch = True
        return self

    def __exit__(self, *args: object) -> None:
        self._client.in_batch = False

    def add(self, call: object) -> None:
        self.calls.append(call)


class FakeClient:
    """RPC boundary fake that only accepts v2 asset calls and defers batch calls."""

    def __init__(
        self,
        keepers: dict[str, str],
        contracts: set[str],
        balances: dict[str, int],
        empty: frozenset[str] = frozenset(),
    ) -> None:
        self.batches: list[list] = []
        self.in_batch = False
        self._keepers = keepers
        self._empty = empty
        self.eth = SimpleNamespace(
            get_code=lambda address: self._batched(b"\x01" if address in contracts else b""),
            get_balance=lambda address: self._batched(balances[address]),
        )

    def _batched(self, value: object) -> object:
        assert self.in_batch, "RPC call built outside batch_requests() would run unbatched"
        return value

    def get_contract(self, address: str, abi: list) -> SimpleNamespace:
        return SimpleNamespace(
            functions=SimpleNamespace(
                keeper=lambda: SimpleNamespace(call=lambda: self._batched(self._keepers[address])),
                estimatedTotalAssets=lambda: SimpleNamespace(
                    call=lambda: self._batched(0 if address in self._empty else 10**18)
                ),
            )
        )

    def batch_requests(self) -> FakeBatch:
        return FakeBatch(self)

    def execute_batch(self, batch: FakeBatch) -> list:
        self.batches.append(batch.calls)
        return batch.calls


def test_collect_keeper_wallets_deduplicates_and_skips_contract_keepers(monkeypatch) -> None:
    client = FakeClient(
        keepers={STRATEGY_A: KEEPER_EOA, STRATEGY_B: KEEPER_EOA, STRATEGY_C: KEEPER_CONTRACT},
        contracts={KEEPER_CONTRACT},
        balances={KEEPER_EOA: 10**18},
    )
    monkeypatch.setattr(mod.ChainManager, "get_client", lambda chain: client)
    strategies = [
        mod.KeeperStrategy(Chain.MAINNET, STRATEGY_A, "A"),
        mod.KeeperStrategy(Chain.MAINNET, STRATEGY_B, "B"),
        mod.KeeperStrategy(Chain.MAINNET, STRATEGY_C, "C"),
    ]
    result = mod.collect_keeper_wallets(Chain.MAINNET, strategies)
    assert result.wallets == {KEEPER_EOA: mod.KeeperWallet(KEEPER_EOA, {"A", "B"}, 10**18)}
    assert [len(calls) for calls in client.batches] == [6, 2, 1]


def test_collect_keeper_wallets_skips_empty_strategies(monkeypatch) -> None:
    client = FakeClient(
        keepers={STRATEGY_A: KEEPER_EOA, STRATEGY_B: STRATEGY_C},
        contracts=set(),
        balances={KEEPER_EOA: 10**18},
        empty=frozenset({STRATEGY_B}),
    )
    monkeypatch.setattr(mod.ChainManager, "get_client", lambda chain: client)
    result = mod.collect_keeper_wallets(
        Chain.BASE,
        [mod.KeeperStrategy(Chain.BASE, STRATEGY_A, "A"), mod.KeeperStrategy(Chain.BASE, STRATEGY_B, "B")],
    )
    assert result.wallets == {KEEPER_EOA: mod.KeeperWallet(KEEPER_EOA, {"A"}, 10**18)}
    assert [len(calls) for calls in client.batches] == [4, 1, 1]


def test_collect_keeper_wallets_with_only_contract_keepers_never_reads_balances(monkeypatch) -> None:
    client = FakeClient(keepers={STRATEGY_A: KEEPER_CONTRACT}, contracts={KEEPER_CONTRACT}, balances={})
    monkeypatch.setattr(mod.ChainManager, "get_client", lambda chain: client)
    result = mod.collect_keeper_wallets(Chain.MAINNET, [mod.KeeperStrategy(Chain.MAINNET, STRATEGY_A, "A")])
    assert result.wallets == {}
    assert mod.evaluate([result], {}) == []


def _result(chain: Chain, balance_wei: int) -> mod.ChainResult:
    return mod.ChainResult(chain, {KEEPER_EOA: mod.KeeperWallet(KEEPER_EOA, {"StrategyCurve"}, balance_wei)})


@pytest.mark.parametrize("chain", [Chain.MAINNET, Chain.OPTIMISM, Chain.BASE, Chain.ARBITRUM])
def test_evaluate_alerts_below_minimum_but_not_at_minimum(chain: Chain) -> None:
    minimum = 5 * 10**15 if chain == Chain.MAINNET else 10**15
    assert mod.evaluate([_result(chain, minimum)], {ETH_KEY: Decimal("2000")}) == []
    issues = mod.evaluate([_result(chain, minimum - 1)], {ETH_KEY: Decimal("2000")})
    assert [issue.code for issue in issues] == [f"low:{chain.chain_id}:{KEEPER_EOA.lower()}"]
    assert f"{chain.explorer_url}/address/{KEEPER_EOA}" in issues[0].message
    assert "StrategyCurve" in issues[0].message


@pytest.mark.parametrize("prices", [{}, {ETH_KEY: Decimal("0")}, {ETH_KEY: Decimal("-1")}])
def test_evaluate_reports_unavailable_prices(prices: dict[str, Decimal]) -> None:
    assert [i.code for i in mod.evaluate([_result(Chain.MAINNET, 0)], prices)] == ["price:1"]


def test_should_send_alert_dedupes_and_reminds_daily() -> None:
    state = '{"fingerprint": "low:1:0xabc", "last_alert": 1000}'
    assert not mod.should_send_alert("", None, 1000)
    assert mod.should_send_alert("low:1:0xabc", None, 1000)
    assert mod.should_send_alert("low:1:0xabc", "invalid json", 1000)
    assert not mod.should_send_alert("low:1:0xabc", state, 1000 + mod.ALERT_REMINDER_SECONDS - 1)
    assert mod.should_send_alert("low:1:0xabc", state, 1000 + mod.ALERT_REMINDER_SECONDS)
    assert mod.should_send_alert("low:1:0xabc|low:1:0xdef", state, 1001)


def test_main_dry_run_does_not_send_alerts_or_write_state(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["check_strategy_keepers.py", "--dry-run"])
    monkeypatch.setattr(
        mod, "fetch_kong_v2_strategies", lambda: [{"chain_id": 10, "address": STRATEGY_A, "name": "StrategyVelodrome"}]
    )
    monkeypatch.setattr(mod, "collect_keeper_wallets", lambda *args: _result(Chain.OPTIMISM, 0))
    monkeypatch.setattr(mod, "fetch_prices", lambda keys: {ETH_KEY: Decimal("2000")})

    def unexpected(*args, **kwargs):
        pytest.fail("Dry run must not send alerts or access stored state")

    monkeypatch.setattr(mod, "send_alert", unexpected)
    monkeypatch.setattr(mod.store, "state_get", unexpected)
    monkeypatch.setattr(mod.store, "state_set", unexpected)
    mod.main()


def test_main_reports_rpc_failure_and_checks_other_chains(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["check_strategy_keepers.py"])
    monkeypatch.setattr(
        mod,
        "fetch_kong_v2_strategies",
        lambda: [
            {"chain_id": 1, "address": STRATEGY_A, "name": "StrategyCurve"},
            {"chain_id": 8453, "address": STRATEGY_B, "name": "StrategyAero"},
        ],
    )

    def collect(chain, strategies):
        if chain == Chain.MAINNET:
            raise ConnectionError("RPC unavailable")
        return _result(chain, 0)

    monkeypatch.setattr(mod, "collect_keeper_wallets", collect)
    monkeypatch.setattr(mod, "fetch_prices", lambda keys: {ETH_KEY: Decimal("2000")})
    monkeypatch.setattr(mod, "resolve_channel", lambda *args: "maintenance")
    monkeypatch.setattr(mod.store, "state_get", lambda *args: None)
    sent = []
    stored = []
    monkeypatch.setattr(mod, "send_alert", lambda alert, **kwargs: sent.append(alert))
    monkeypatch.setattr(mod.store, "state_set", lambda *args: stored.append(args))
    mod.main()
    assert len(sent) == 1
    assert sent[0].protocol == "yearn-internal"
    assert sent[0].channel == "maintenance"
    assert sent[0].severity == mod.AlertSeverity.MEDIUM
    assert "Yearn V2 Keeper Gas Warning" in sent[0].message
    assert "MAINNET: monitor error RPC unavailable" in sent[0].message
    assert f"https://basescan.org/address/{KEEPER_EOA}" in sent[0].message
    assert stored[0][0] == "yearn_v2_keepers_alerts"

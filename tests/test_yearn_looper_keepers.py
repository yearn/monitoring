from decimal import Decimal
from types import SimpleNamespace

from web3 import Web3

from protocols.yearn import check_looper_keepers as mod
from utils.chains import Chain

ETH_KEY = "coingecko:ethereum"
POL_KEY = "coingecko:polygon-ecosystem-token"

LOOPER_A = "0x00000000000000000000000000000000000000a1"
LOOPER_B = "0x00000000000000000000000000000000000000a2"
LOOPER_C = "0x00000000000000000000000000000000000000a3"
KEEPER_EOA = Web3.to_checksum_address("0x00000000000000000000000000000000000000e1")
KEEPER_CONTRACT = Web3.to_checksum_address("0x00000000000000000000000000000000000000c1")
BOT = Web3.to_checksum_address("0x00000000000000000000000000000000000000b1")
MULTISIG = Web3.to_checksum_address("0x00000000000000000000000000000000000000f1")


def _strategy(address: str, name: str, chain_id: int = 1, is_shutdown: bool = False) -> dict:
    return {"chain_id": chain_id, "address": address, "name": name, "is_shutdown": is_shutdown}


def _allowed_log(keeper: str, caller: str, allowed: bool, block: int, index: int = 0) -> dict:
    return {
        "address": keeper,
        "blockNumber": block,
        "logIndex": index,
        "topics": [
            bytes.fromhex(mod.ALLOWED_SET_TOPIC[2:]),
            bytes(12) + bytes.fromhex(caller[2:]),
            (1 if allowed else 0).to_bytes(32, "big"),
        ],
    }


def test_select_loopers_filters_name_shutdown_and_unsupported_chains() -> None:
    loopers = mod.select_loopers(
        [
            _strategy(LOOPER_A, "wstETH/WETH Spark Looper"),
            _strategy(LOOPER_A, "wstETH/WETH Spark Looper"),
            _strategy(LOOPER_B, "Katana vbUSDC Morpho LooperStrategy", chain_id=Chain.POLYGON.chain_id),
            _strategy(LOOPER_C, "PT siUSD Morpho Looper", is_shutdown=True),
            _strategy(LOOPER_C, "USDC Lender"),
            _strategy(LOOPER_C, "spUSDG/USDG Morpho Looper", chain_id=4663),
        ]
    )

    assert {chain: [looper.address for looper in items] for chain, items in loopers.items()} == {
        Chain.MAINNET: [Web3.to_checksum_address(LOOPER_A)],
        Chain.POLYGON: [Web3.to_checksum_address(LOOPER_B)],
    }


def test_latest_allowed_callers_replays_events_in_order() -> None:
    other = "0x00000000000000000000000000000000000000b2"
    logs = [
        _allowed_log(KEEPER_CONTRACT, BOT, False, block=20),
        _allowed_log(KEEPER_CONTRACT, BOT, True, block=10),
        _allowed_log(KEEPER_CONTRACT, other, True, block=10, index=1),
    ]

    assert mod.latest_allowed_callers(logs) == {KEEPER_CONTRACT: {Web3.to_checksum_address(other)}}


class FakeBatch:
    def __init__(self) -> None:
        self.calls: list = []

    def __enter__(self) -> "FakeBatch":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def add(self, call: object) -> None:
        self.calls.append(call)


class FakeClient:
    """Web3Client stand-in whose calls resolve eagerly; batches just collect the results."""

    def __init__(self, keepers: dict[str, str], contracts: set[str], balances: dict[str, int], logs: list) -> None:
        self.batches: list[list] = []
        self.logs_requests: list[dict] = []
        self._keepers = {Web3.to_checksum_address(k): v for k, v in keepers.items()}
        self.eth = SimpleNamespace(
            get_code=lambda address: b"\x01" if address in contracts else b"",
            get_balance=lambda address: balances[address],
            get_logs=self._get_logs,
        )
        self._logs = logs

    def _get_logs(self, params: dict) -> list:
        self.logs_requests.append(params)
        return self._logs

    def get_contract(self, address: str, abi: list) -> SimpleNamespace:
        keeper = self._keepers[address]
        return SimpleNamespace(functions=SimpleNamespace(keeper=lambda: SimpleNamespace(call=lambda: keeper)))

    def batch_requests(self) -> FakeBatch:
        return FakeBatch()

    def execute_batch(self, batch: FakeBatch) -> list:
        self.batches.append(batch.calls)
        return batch.calls

    def execute(self, operation, *args, **kwargs):
        return operation(*args, **kwargs)


def test_collect_keeper_wallets_dedupes_and_batches(monkeypatch) -> None:
    client = FakeClient(
        keepers={LOOPER_A: KEEPER_EOA, LOOPER_B: KEEPER_CONTRACT, LOOPER_C: KEEPER_CONTRACT},
        contracts={KEEPER_CONTRACT, MULTISIG},
        balances={KEEPER_EOA: 10**18, BOT: 2 * 10**18},
        logs=[
            _allowed_log(KEEPER_CONTRACT, BOT, True, block=1),
            _allowed_log(KEEPER_CONTRACT, MULTISIG, True, block=2),
        ],
    )
    monkeypatch.setattr(mod.ChainManager, "get_client", lambda chain: client)
    monkeypatch.setattr(mod, "EXTRA_KEEPER_CALLERS", {Chain.MAINNET: {KEEPER_CONTRACT: (KEEPER_EOA, BOT)}})
    loopers = [
        mod.Looper(Chain.MAINNET, Web3.to_checksum_address(LOOPER_A), "A"),
        mod.Looper(Chain.MAINNET, Web3.to_checksum_address(LOOPER_B), "B"),
        mod.Looper(Chain.MAINNET, Web3.to_checksum_address(LOOPER_C), "C"),
    ]

    result = mod.collect_keeper_wallets(Chain.MAINNET, loopers)

    assert {address: wallet.strategies for address, wallet in result.wallets.items()} == {
        KEEPER_EOA: {"A", "B", "C"},
        BOT: {"B", "C"},
    }
    assert {address: wallet.balance_wei for address, wallet in result.wallets.items()} == {
        KEEPER_EOA: 10**18,
        BOT: 2 * 10**18,
    }
    assert result.keepers_without_callers == {}
    # keeper(), keeper code, caller code, balances: one batch each, no repeated addresses.
    assert [len(calls) for calls in client.batches] == [3, 2, 3, 2]
    assert client.logs_requests[0]["address"] == [KEEPER_CONTRACT]


def test_collect_keeper_wallets_flags_contract_keeper_with_only_multisig_callers(monkeypatch) -> None:
    client = FakeClient(
        keepers={LOOPER_A: KEEPER_CONTRACT},
        contracts={KEEPER_CONTRACT, MULTISIG},
        balances={},
        logs=[_allowed_log(KEEPER_CONTRACT, MULTISIG, True, block=1)],
    )
    monkeypatch.setattr(mod.ChainManager, "get_client", lambda chain: client)
    monkeypatch.setattr(mod, "EXTRA_KEEPER_CALLERS", {})

    result = mod.collect_keeper_wallets(
        Chain.MAINNET, [mod.Looper(Chain.MAINNET, Web3.to_checksum_address(LOOPER_A), "A")]
    )

    assert result.wallets == {}
    assert result.keepers_without_callers == {KEEPER_CONTRACT: {"A"}}


def _result(chain: Chain, balance_wei: int, without_callers: dict | None = None) -> mod.ChainResult:
    wallet = mod.KeeperWallet(KEEPER_EOA, {"Looper"}, balance_wei)
    return mod.ChainResult(chain, {KEEPER_EOA: wallet}, without_callers or {})


def test_evaluate_uses_five_dollars_on_mainnet_and_one_dollar_elsewhere() -> None:
    prices = {ETH_KEY: Decimal("2000"), POL_KEY: Decimal("0.5")}
    # 0.002 ETH = $4 on mainnet (below $5); 2.5 POL = $1.25 on Polygon (above $1).
    issues = mod.evaluate(
        [_result(Chain.MAINNET, 2 * 10**15), _result(Chain.POLYGON, 25 * 10**17)],
        prices,
    )

    assert [issue.code for issue in issues] == [f"low:1:{KEEPER_EOA.lower()}"]
    assert f"https://etherscan.io/address/{KEEPER_EOA}" in issues[0].message
    assert "($4.00 < $5)" in issues[0].message

    # 0.0004 ETH = $0.80 on Base (below $1); 0.003 ETH = $6 on mainnet (above $5).
    issues = mod.evaluate([_result(Chain.BASE, 4 * 10**14), _result(Chain.MAINNET, 3 * 10**15)], prices)
    assert [issue.code for issue in issues] == [f"low:8453:{KEEPER_EOA.lower()}"]


def test_evaluate_reports_missing_price_and_keepers_without_callers() -> None:
    issues = mod.evaluate(
        [_result(Chain.MAINNET, 10**18, {KEEPER_CONTRACT: {"Looper"}}), _result(Chain.POLYGON, 10**18)],
        {ETH_KEY: Decimal("2000")},
    )

    assert [issue.code for issue in issues] == [f"nocaller:1:{KEEPER_CONTRACT.lower()}", "price:137"]


def test_should_send_alert_dedupes_and_reminds_daily() -> None:
    state = '{"fingerprint": "low:1:0xabc", "last_alert": 1000}'

    assert not mod.should_send_alert("", None, 1000)
    assert mod.should_send_alert("low:1:0xabc", None, 1000)
    assert not mod.should_send_alert("low:1:0xabc", state, 1000 + mod.ALERT_REMINDER_SECONDS - 1)
    assert mod.should_send_alert("low:1:0xabc", state, 1000 + mod.ALERT_REMINDER_SECONDS)
    assert mod.should_send_alert("low:1:0xabc|low:1:0xdef", state, 1001)

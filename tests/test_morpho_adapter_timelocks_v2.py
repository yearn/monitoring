"""Behavior tests for on-chain MorphoMarketV1AdapterV2 timelock monitoring."""

import unittest
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

from web3 import Web3

from protocols.morpho import adapter_timelocks_v2 as adapters
from protocols.morpho._alerts import VaultDiff
from protocols.morpho.adapter_timelocks_v2 import AdapterTimelockState, MarketBalance, PendingBurn
from utils.alert import AlertSeverity
from utils.chains import Chain

ADAPTER = Web3.to_checksum_address("0x" + "22" * 20)
MORPHO = Web3.to_checksum_address("0x" + "33" * 20)
MARKET_A = bytes.fromhex("ab" * 32)
MARKET_B = bytes.fromhex("cd" * 32)
MARKET_A_HEX = Web3.to_hex(MARKET_A)
EXECUTABLE_AT = 2_000_000_000
METADATA = {"name": "WETH/USDC", "loan_symbol": "USDC", "loan_decimals": 6, "lltv": 860000000000000000}


def _state(
    burns: list[PendingBurn] | None = None,
    balances: list[MarketBalance] | None = None,
    burn_timelock: int = 3 * 86400,
) -> AdapterTimelockState:
    return AdapterTimelockState(
        adapter=ADAPTER, burn_timelock=burn_timelock, pending_burns=burns or [], balances=balances or []
    )


def _burn(shares: int, assets: int = 0, executable_at: int = EXECUTABLE_AT) -> PendingBurn:
    return PendingBurn(market_id=MARKET_A_HEX, executable_at=executable_at, shares=shares, assets=assets)


def _balance(adapter_shares: int, morpho_shares: int) -> MarketBalance:
    # 1e6 shares per asset unit, like a fresh Morpho Blue market.
    return MarketBalance(MARKET_A_HEX, adapter_shares, morpho_shares, 10**12, 10**18)


class _Cache:
    """In-memory stand-in for the morpho cache file."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def patches(self) -> Any:
        return (
            patch(
                "protocols.morpho.adapter_timelocks_v2.get_last_value_for_key_from_file",
                side_effect=lambda _f, key: self.values.get(key, 0),
            ),
            patch(
                "protocols.morpho.adapter_timelocks_v2.write_last_value_to_file",
                side_effect=lambda _f, key, value: self.values.__setitem__(key, str(value)),
            ),
        )


class TestAdapterDiff(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = _Cache()

    def _run(self, state: AdapterTimelockState, now: int = 1_000_000_000) -> VaultDiff:
        diff = VaultDiff()
        read, write = self.cache.patches()
        with (
            read,
            write,
            patch("protocols.morpho.adapter_timelocks_v2.fetch_market_metadata", return_value=METADATA),
            patch("protocols.morpho.adapter_timelocks_v2.datetime") as fake_datetime,
        ):
            fake_datetime.now.return_value = datetime.fromtimestamp(now)
            adapters.diff_adapter(Chain.MAINNET, 10_000_000 * 10**6, state, diff)
            diff.commit()
        return diff

    def test_pending_burn_with_shares_is_critical_then_reminded_once(self) -> None:
        state = _state([_burn(shares=10**12, assets=2_500_000 * 10**6)])

        first = self._run(state, now=EXECUTABLE_AT - 3 * 86400)
        quiet = self._run(state, now=EXECUTABLE_AT - 2 * 86400)
        reminder = self._run(state, now=EXECUTABLE_AT - 3600)
        after = self._run(state, now=EXECUTABLE_AT - 60)

        self.assertEqual(len(first.alerts), 1)
        alert = first.alerts[0]
        self.assertEqual(alert.severity, AlertSeverity.CRITICAL)
        self.assertTrue(alert.standalone)
        self.assertIn("`burnShares` on market [WETH/USDC]", alert.body)
        self.assertIn("2.50M USDC (25.00% of vault)", alert.body)
        self.assertIn(f"[{ADAPTER}](", alert.body)
        self.assertEqual(quiet.alerts, [])
        self.assertEqual(len(reminder.alerts), 1)
        self.assertIn("Reminder", reminder.alerts[0].body)
        self.assertEqual(after.alerts, [])

    def test_shares_worth_zero_assets_are_still_critical(self) -> None:
        """Criticality follows shares, so a stale/zero valuation can't downgrade a real burn."""
        diff = self._run(_state([_burn(shares=1, assets=0)]))

        self.assertEqual(diff.alerts[0].severity, AlertSeverity.CRITICAL)

    def test_pending_burn_without_shares_is_medium(self) -> None:
        diff = self._run(_state([_burn(shares=0)]))

        self.assertEqual([a.severity for a in diff.alerts], [AlertSeverity.MEDIUM])
        self.assertFalse(diff.alerts[0].standalone)

    def test_resolved_burn_reports_executed_and_clears_its_cursor(self) -> None:
        self._run(_state([_burn(shares=5)]), now=EXECUTABLE_AT - 86400 * 3)
        diff = self._run(_state(), now=EXECUTABLE_AT + 60)

        self.assertEqual(len(diff.alerts), 1)
        self.assertEqual(diff.alerts[0].severity, AlertSeverity.LOW)
        self.assertIn("was executed", diff.alerts[0].body)
        # A re-submit with the same executableAt alerts again.
        again = self._run(_state([_burn(shares=5)]), now=EXECUTABLE_AT - 86400 * 3)
        self.assertEqual(again.alerts[0].severity, AlertSeverity.CRITICAL)

    def test_balanced_accounting_is_silent(self) -> None:
        self.assertEqual(self._run(_state(balances=[_balance(10**12, 10**12)])).alerts, [])

    def test_executed_burn_is_caught_from_the_share_gap_once(self) -> None:
        """Catches a burn submitted with padded calldata, which the pending check can't see."""
        burned = _state(balances=[_balance(0, 3 * 10**12)])

        first = self._run(burned)
        again = self._run(burned)

        self.assertEqual(len(first.alerts), 1)
        alert = first.alerts[0]
        self.assertEqual(alert.severity, AlertSeverity.CRITICAL)
        self.assertTrue(alert.standalone)
        self.assertIn("supply written off on market [WETH/USDC]", alert.body)
        self.assertIn("3.00 USDC", alert.body)
        self.assertIn("accounts for 0 shares but holds 3,000,000,000,000", alert.body)
        self.assertEqual(again.alerts, [])

    def test_a_larger_gap_alerts_again(self) -> None:
        self._run(_state(balances=[_balance(2 * 10**12, 3 * 10**12)]))
        bigger = self._run(_state(balances=[_balance(0, 3 * 10**12)]))

        self.assertEqual([a.severity for a in bigger.alerts], [AlertSeverity.CRITICAL])

    def test_markets_with_a_position_are_remembered(self) -> None:
        empty = MarketBalance(Web3.to_hex(MARKET_B), 0, 0, 1, 1)
        self._run(_state(balances=[_balance(0, 5), empty]))

        read, _write = self.cache.patches()
        with read:
            self.assertEqual(adapters._known_markets(ADAPTER), {MARKET_A})

    def test_burn_timelock_seeds_silently_then_alerts_high_on_decrease(self) -> None:
        seed = self._run(_state(burn_timelock=3 * 86400))
        increase = self._run(_state(burn_timelock=7 * 86400))
        decrease = self._run(_state(burn_timelock=0))

        self.assertEqual(seed.alerts, [])
        self.assertEqual(increase.alerts, [])
        self.assertEqual(len(decrease.alerts), 1)
        self.assertEqual(decrease.alerts[0].severity, AlertSeverity.HIGH)
        self.assertIn("7d → 0s", decrease.alerts[0].body)


class _FakeBatch:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def add(self, call: Any) -> None:
        self.calls.append(call)


class _FakeClient:
    """Minimal batching client whose contract calls only resolve inside a batch context.

    Mirrors web3's batching: a ``.call()`` made before entering ``batch_requests()``
    would run eagerly, so the fake rejects it. Only the listed functions exist, so
    a call to ``expectedSupplyAssets`` (IRM-dependent, may revert) or ``get_logs``
    fails the test.
    """

    def __init__(self, values: dict[tuple[str, Any], Any]) -> None:
        self.values = values
        self.in_batch = False
        names = ("marketIdsLength", "timelock", "morpho", "marketIds", "executableAt", "supplyShares")
        functions = MagicMock(spec=list(names))
        for name in names:
            setattr(functions, name, self._bound(name))
        blue_functions = MagicMock(spec=["position", "market"])
        blue_functions.position = self._bound("position")
        blue_functions.market = self._bound("market")
        adapter = MagicMock(functions=functions, address=ADAPTER)
        blue = MagicMock(functions=blue_functions)
        self.eth = MagicMock(spec=["contract"])
        self.eth.contract.side_effect = lambda address, abi: adapter if address == ADAPTER else blue

    def _bound(self, name: str) -> Any:
        def bind(*args: Any) -> Any:
            def call(**_kwargs: Any) -> Any:
                if not self.in_batch:
                    raise AssertionError(f"{name}.call() ran outside a batch context")
                return self.values[(name, args[0] if args else None)]

            return MagicMock(call=call)

        return bind

    @contextmanager
    def batch_requests(self) -> Iterator[_FakeBatch]:
        self.in_batch = True
        try:
            yield _FakeBatch()
        finally:
            self.in_batch = False

    def execute_batch(self, batch: _FakeBatch) -> list[Any]:
        return batch.calls


def _values(held: list[bytes], executable_at: dict[bytes, int], adapter_shares: dict[bytes, int]) -> dict:
    values: dict[tuple[str, Any], Any] = {
        ("marketIdsLength", None): len(held),
        ("timelock", adapters.BURN_SHARES_SELECTOR): 259200,
        ("morpho", None): MORPHO,
    }
    for index, market in enumerate(held):
        values[("marketIds", index)] = market
    for market in (MARKET_A, MARKET_B):
        values[("executableAt", adapters.burn_shares_calldata(market))] = executable_at.get(market, 0)
        values[("supplyShares", market)] = adapter_shares.get(market, 0)
        values[("position", market)] = (2 * 10**12, 0, 0)
        values[("market", market)] = (2 * 10**6, 2 * 10**12, 0, 0, 0, 0)
    return values


class TestLoadAdapterState(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = _Cache()

    def _load(self, client: _FakeClient) -> AdapterTimelockState:
        read, write = self.cache.patches()
        with read, write:
            return adapters.load_adapter_state(client, ADAPTER, block_number=1)

    def test_reads_pending_burns_and_balances_for_held_markets(self) -> None:
        client = _FakeClient(
            _values([MARKET_A, MARKET_B], {MARKET_A: EXECUTABLE_AT}, {MARKET_A: 2 * 10**12, MARKET_B: 2 * 10**12})
        )

        state = self._load(client)

        self.assertEqual(state.burn_timelock, 259200)
        self.assertEqual(state.pending_burns, [PendingBurn(MARKET_A_HEX, EXECUTABLE_AT, 2 * 10**12, 2_000_000)])
        self.assertEqual([b.unaccounted_shares for b in state.balances], [0, 0])

    def test_remembered_market_is_still_checked_after_leaving_market_ids(self) -> None:
        """A burned market drops out of marketIds once deallocated; its share gap must still be seen."""
        self.cache.values[adapters._key(ADAPTER, "known", adapters.KNOWN_MARKETS_TYPE)] = MARKET_A.hex()
        client = _FakeClient(_values([MARKET_B], {}, {MARKET_B: 2 * 10**12}))

        state = self._load(client)

        gaps = {b.market_id: b.unaccounted_shares for b in state.balances}
        self.assertEqual(gaps, {MARKET_A_HEX: 2 * 10**12, Web3.to_hex(MARKET_B): 0})

    def test_burn_shares_calldata_matches_abi_encoding(self) -> None:
        self.assertEqual(
            adapters.burn_shares_calldata(MARKET_A),
            bytes(Web3.keccak(text="burnShares(bytes32)")[:4]) + MARKET_A,
        )


if __name__ == "__main__":
    unittest.main()

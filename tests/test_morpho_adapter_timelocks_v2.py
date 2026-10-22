"""Behavior tests for on-chain MorphoMarketV1AdapterV2 timelock monitoring."""

import unittest
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

from web3 import Web3

from protocols.morpho import adapter_timelocks_v2 as adapters
from protocols.morpho._alerts import VaultDiff
from protocols.morpho.adapter_timelocks_v2 import AdapterTimelockState, PendingBurn
from utils.alert import AlertSeverity
from utils.chains import Chain

ADAPTER = Web3.to_checksum_address("0x" + "22" * 20)
MARKET_A = bytes.fromhex("ab" * 32)
MARKET_B = bytes.fromhex("cd" * 32)
EXECUTABLE_AT = 2_000_000_000
METADATA = {"name": "WETH/USDC", "loan_symbol": "USDC", "loan_decimals": 6, "lltv": 860000000000000000}


def _state(burns: list[PendingBurn], burn_timelock: int = 3 * 86400) -> AdapterTimelockState:
    return AdapterTimelockState(adapter=ADAPTER, burn_timelock=burn_timelock, pending_burns=burns)


def _burn(assets: int, executable_at: int = EXECUTABLE_AT) -> PendingBurn:
    return PendingBurn(market_id=Web3.to_hex(MARKET_A), executable_at=executable_at, assets=assets)


class TestAdapterDiff(unittest.TestCase):
    def setUp(self) -> None:
        self.cache: dict[str, str] = {}

    def _run(self, state: AdapterTimelockState, now: int) -> VaultDiff:
        diff = VaultDiff()
        with (
            patch(
                "protocols.morpho.adapter_timelocks_v2.get_last_value_for_key_from_file",
                side_effect=lambda _f, key: self.cache.get(key, 0),
            ),
            patch(
                "protocols.morpho.adapter_timelocks_v2.write_last_value_to_file",
                side_effect=lambda _f, key, value: self.cache.__setitem__(key, str(value)),
            ),
            patch("protocols.morpho.adapter_timelocks_v2.fetch_market_metadata", return_value=METADATA),
            patch("protocols.morpho.adapter_timelocks_v2.datetime") as fake_datetime,
        ):
            fake_datetime.now.return_value = datetime.fromtimestamp(now)
            adapters.diff_adapter(Chain.MAINNET, 10_000_000 * 10**6, state, diff)
            diff.commit()
        return diff

    def test_pending_burn_with_assets_is_critical_then_reminded_once(self) -> None:
        state = _state([_burn(2_500_000 * 10**6)])

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

    def test_pending_burn_without_assets_is_medium(self) -> None:
        diff = self._run(_state([_burn(0)]), now=1_000_000_000)

        self.assertEqual([a.severity for a in diff.alerts], [AlertSeverity.MEDIUM])
        self.assertFalse(diff.alerts[0].standalone)

    def test_resolved_burn_reports_executed_and_clears_its_cursor(self) -> None:
        self._run(_state([_burn(5)]), now=EXECUTABLE_AT - 86400 * 3)
        diff = self._run(_state([]), now=EXECUTABLE_AT + 60)

        self.assertEqual(len(diff.alerts), 1)
        self.assertEqual(diff.alerts[0].severity, AlertSeverity.LOW)
        self.assertIn("was executed", diff.alerts[0].body)
        # A re-submit with the same executableAt alerts again.
        again = self._run(_state([_burn(5)]), now=EXECUTABLE_AT - 86400 * 3)
        self.assertEqual(again.alerts[0].severity, AlertSeverity.CRITICAL)

    def test_burn_timelock_seeds_silently_then_alerts_high_on_decrease(self) -> None:
        seed = self._run(_state([], burn_timelock=3 * 86400), now=1_000_000_000)
        increase = self._run(_state([], burn_timelock=7 * 86400), now=1_000_000_000)
        decrease = self._run(_state([], burn_timelock=0), now=1_000_000_000)

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
    would run eagerly, so the fake rejects it.
    """

    def __init__(self, values: dict[tuple[str, Any], Any]) -> None:
        self.values = values
        self.in_batch = False
        functions = MagicMock()
        for name in ("marketIdsLength", "timelock", "marketIds", "executableAt", "expectedSupplyAssets"):
            getattr(functions, name).side_effect = self._bound(name)
        self.functions = functions
        self.eth = MagicMock()
        self.eth.contract.return_value = MagicMock(functions=functions, address=ADAPTER)

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


class TestLoadAdapterState(unittest.TestCase):
    def test_reads_pending_burns_only_for_held_markets(self) -> None:
        client = _FakeClient(
            {
                ("marketIdsLength", None): 2,
                ("timelock", adapters.BURN_SHARES_SELECTOR): 259200,
                ("marketIds", 0): MARKET_A,
                ("marketIds", 1): MARKET_B,
                ("executableAt", adapters.burn_shares_calldata(MARKET_A)): EXECUTABLE_AT,
                ("executableAt", adapters.burn_shares_calldata(MARKET_B)): 0,
                ("expectedSupplyAssets", MARKET_A): 7,
                ("expectedSupplyAssets", MARKET_B): 9,
            }
        )

        state = adapters.load_adapter_state(client, ADAPTER, block_number=1)

        self.assertEqual(state.burn_timelock, 259200)
        self.assertEqual(state.pending_burns, [PendingBurn(Web3.to_hex(MARKET_A), EXECUTABLE_AT, 7)])

    def test_burn_shares_calldata_matches_abi_encoding(self) -> None:
        self.assertEqual(
            adapters.burn_shares_calldata(MARKET_A),
            bytes(Web3.keccak(text="burnShares(bytes32)")[:4]) + MARKET_A,
        )


if __name__ == "__main__":
    unittest.main()

"""Behavior tests for on-chain MorphoMarketV1AdapterV2 timelock monitoring."""

import unittest
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

from eth_abi import encode as abi_encode
from web3 import Web3

from protocols.morpho import adapter_timelocks_v2 as adapters
from protocols.morpho._alerts import VaultDiff
from protocols.morpho.adapter_timelocks_v2 import AdapterTimelockState, PendingBurn
from utils.alert import AlertSeverity
from utils.chains import Chain

ADAPTER = Web3.to_checksum_address("0x" + "22" * 20)
MORPHO = Web3.to_checksum_address("0x" + "33" * 20)
MARKET_A = bytes.fromhex("ab" * 32)
MARKET_B = bytes.fromhex("cd" * 32)
EXECUTABLE_AT = 2_000_000_000
METADATA = {"name": "WETH/USDC", "loan_symbol": "USDC", "loan_decimals": 6, "lltv": 860000000000000000}
CANONICAL_A = adapters.burn_shares_calldata(MARKET_A)
# Same burn, but with a trailing byte: Solidity still executes it, under a different executableAt key.
PADDED_A = CANONICAL_A + b"\x00"


def _state(burns: list[PendingBurn], burn_timelock: int = 3 * 86400) -> AdapterTimelockState:
    return AdapterTimelockState(adapter=ADAPTER, burn_timelock=burn_timelock, pending_burns=burns)


def _burn(shares: int, assets: int = 0, executable_at: int = EXECUTABLE_AT, data: bytes = CANONICAL_A) -> PendingBurn:
    return PendingBurn(data=data, executable_at=executable_at, shares=shares, assets=assets)


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

    def _run(self, state: AdapterTimelockState, now: int) -> VaultDiff:
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
        self.assertNotIn("non-canonical", alert.body)
        self.assertEqual(quiet.alerts, [])
        self.assertEqual(len(reminder.alerts), 1)
        self.assertIn("Reminder", reminder.alerts[0].body)
        self.assertEqual(after.alerts, [])

    def test_shares_worth_zero_assets_are_still_critical(self) -> None:
        """Criticality follows shares, so a stale/zero valuation can't downgrade a real burn."""
        diff = self._run(_state([_burn(shares=1, assets=0)]), now=1_000_000_000)

        self.assertEqual(diff.alerts[0].severity, AlertSeverity.CRITICAL)

    def test_non_canonical_burn_shows_exact_calldata_to_revoke(self) -> None:
        diff = self._run(_state([_burn(shares=5, assets=5, data=PADDED_A)]), now=1_000_000_000)

        body = diff.alerts[0].body
        self.assertEqual(diff.alerts[0].severity, AlertSeverity.CRITICAL)
        self.assertIn("non-canonical calldata (1 trailing bytes)", body)
        self.assertIn(f"`0x{PADDED_A.hex()}`", body)

    def test_canonical_and_padded_burns_of_one_market_alert_separately(self) -> None:
        diff = self._run(_state([_burn(shares=5, assets=5), _burn(shares=5, assets=5, data=PADDED_A)]), now=1)

        self.assertEqual([a.severity for a in diff.alerts], [AlertSeverity.CRITICAL, AlertSeverity.CRITICAL])

    def test_pending_burn_without_shares_is_medium(self) -> None:
        diff = self._run(_state([_burn(shares=0)]), now=1_000_000_000)

        self.assertEqual([a.severity for a in diff.alerts], [AlertSeverity.MEDIUM])
        self.assertFalse(diff.alerts[0].standalone)

    def test_resolved_burn_reports_executed_and_clears_its_cursor(self) -> None:
        self._run(_state([_burn(shares=5)]), now=EXECUTABLE_AT - 86400 * 3)
        diff = self._run(_state([]), now=EXECUTABLE_AT + 60)

        self.assertEqual(len(diff.alerts), 1)
        self.assertEqual(diff.alerts[0].severity, AlertSeverity.LOW)
        self.assertIn("was executed", diff.alerts[0].body)
        self.assertIn(Web3.to_hex(MARKET_A), diff.alerts[0].body)
        # A re-submit with the same executableAt alerts again.
        again = self._run(_state([_burn(shares=5)]), now=EXECUTABLE_AT - 86400 * 3)
        self.assertEqual(again.alerts[0].severity, AlertSeverity.CRITICAL)

    def test_pending_calldata_and_scan_cursor_are_persisted(self) -> None:
        state = AdapterTimelockState(
            adapter=ADAPTER,
            burn_timelock=1,
            pending_burns=[_burn(shares=5, data=PADDED_A)],
            scanned_to_block=1234,
        )
        self._run(state, now=1)

        read, _write = self.cache.patches()
        with read:
            self.assertEqual(adapters._tracked_data(ADAPTER), {PADDED_A})
            self.assertEqual(
                adapters._read_int(adapters._key(ADAPTER, "cursor", adapters.SUBMIT_LOG_CURSOR_TYPE)), 1234
            )

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


def _submit_log(data: bytes) -> dict[str, Any]:
    return {"data": abi_encode(["bytes", "uint256"], [data, EXECUTABLE_AT])}


class _FakeClient:
    """Minimal batching client whose contract calls only resolve inside a batch context.

    Mirrors web3's batching: a ``.call()`` made before entering ``batch_requests()``
    would run eagerly, so the fake rejects it. Functions missing from ``values``
    (e.g. ``expectedSupplyAssets``, which may revert via the IRM) fail loudly.
    """

    def __init__(self, values: dict[tuple[str, Any], Any], logs: dict[tuple[int, int], list[dict[str, Any]]]) -> None:
        self.values = values
        self.logs = logs
        self.log_requests: list[tuple[int, int]] = []
        self.in_batch = False
        names = ("marketIdsLength", "timelock", "morpho", "marketIds", "executableAt", "supplyShares", "market")
        functions = MagicMock(spec=list(names))
        for name in names:
            setattr(functions, name, self._bound(name))
        self.eth = MagicMock()
        self.eth.contract.return_value = MagicMock(functions=functions, address=ADAPTER)
        self.eth.get_logs.side_effect = self._get_logs

    def _bound(self, name: str) -> Any:
        def bind(*args: Any) -> Any:
            def call(**_kwargs: Any) -> Any:
                if not self.in_batch:
                    raise AssertionError(f"{name}.call() ran outside a batch context")
                return self.values[(name, args[0] if args else None)]

            return MagicMock(call=call)

        return bind

    def _get_logs(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        assert params["topics"] == [adapters.SUBMIT_TOPIC, adapters.BURN_SHARES_TOPIC]
        span = (params["fromBlock"], params["toBlock"])
        self.log_requests.append(span)
        return self.logs.get(span, [])

    @contextmanager
    def batch_requests(self) -> Iterator[_FakeBatch]:
        self.in_batch = True
        try:
            yield _FakeBatch()
        finally:
            self.in_batch = False

    def execute_batch(self, batch: _FakeBatch) -> list[Any]:
        return batch.calls


def _values(executable_at: dict[bytes, int]) -> dict[tuple[str, Any], Any]:
    values: dict[tuple[str, Any], Any] = {
        ("marketIdsLength", None): 2,
        ("timelock", adapters.BURN_SHARES_SELECTOR): 259200,
        ("morpho", None): MORPHO,
        ("marketIds", 0): MARKET_A,
        ("marketIds", 1): MARKET_B,
        ("supplyShares", MARKET_A): 2 * 10**12,
        ("market", MARKET_A): (2 * 10**6, 2 * 10**12, 0, 0, 0, 0),
    }
    for data in (CANONICAL_A, adapters.burn_shares_calldata(MARKET_B), PADDED_A):
        values[("executableAt", data)] = executable_at.get(data, 0)
    return values


class TestLoadAdapterState(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = _Cache()

    def _load(self, client: _FakeClient, *, creation_block: int = 1, head: int = 100) -> AdapterTimelockState:
        read, write = self.cache.patches()
        with read, write, patch.dict(adapters.SUBMIT_LOG_BLOCK_RANGE_BY_CHAIN, {Chain.MAINNET: 50}):
            return adapters.load_adapter_state(client, Chain.MAINNET, ADAPTER, creation_block, head)

    def test_canonical_pending_burn_is_found_without_logs_and_valued_from_stored_shares(self) -> None:
        client = _FakeClient(_values({CANONICAL_A: EXECUTABLE_AT}), logs={})

        state = self._load(client)

        self.assertEqual(state.burn_timelock, 259200)
        self.assertEqual(state.pending_burns, [PendingBurn(CANONICAL_A, EXECUTABLE_AT, 2 * 10**12, 2_000_000)])
        # Scanned from the creation block in 50-block chunks, up to the head.
        self.assertEqual(client.log_requests, [(1, 50), (51, 100)])
        self.assertEqual((state.scanned_to_block, state.lag_blocks), (100, 0))

    def test_non_canonical_submission_is_found_through_its_submit_log(self) -> None:
        """The canonical key stays zero; only the logged calldata reveals the pending burn."""
        client = _FakeClient(_values({PADDED_A: EXECUTABLE_AT}), logs={(51, 100): [_submit_log(PADDED_A)]})

        state = self._load(client)

        self.assertEqual([b.data for b in state.pending_burns], [PADDED_A])
        self.assertEqual(state.pending_burns[0].market_id, Web3.to_hex(MARKET_A))

    def test_tracked_calldata_is_re_read_once_its_log_is_behind_the_cursor(self) -> None:
        self.cache.values[adapters._key(ADAPTER, "tracked", adapters.TRACKED_BURN_DATA_TYPE)] = PADDED_A.hex()
        self.cache.values[adapters._key(ADAPTER, "cursor", adapters.SUBMIT_LOG_CURSOR_TYPE)] = "100"
        client = _FakeClient(_values({PADDED_A: EXECUTABLE_AT}), logs={})

        state = self._load(client, head=120)

        self.assertEqual([b.data for b in state.pending_burns], [PADDED_A])
        # Resumes just below the cursor (reorg overlap), not from the creation block.
        self.assertEqual(client.log_requests, [(50, 99), (100, 120)])

    def test_backfill_stops_at_request_budget_and_reports_lag(self) -> None:
        client = _FakeClient(_values({}), logs={})

        with patch.object(adapters, "MAX_SUBMIT_LOG_REQUESTS_PER_RUN", 1):
            state = self._load(client, head=1000)

        self.assertEqual(client.log_requests, [(1, 50)])
        self.assertEqual((state.scanned_to_block, state.lag_blocks), (50, 950))

    def test_failed_log_scan_keeps_canonical_detection_and_reports_the_error(self) -> None:
        client = _FakeClient(_values({CANONICAL_A: EXECUTABLE_AT}), logs={})
        client.eth.get_logs.side_effect = ValueError("block range too large")

        state = self._load(client)

        self.assertEqual([b.data for b in state.pending_burns], [CANONICAL_A])
        self.assertIn("block range too large", state.scan_error)
        # No progress recorded: the next run rescans from the creation block.
        self.assertEqual(state.scanned_to_block, 0)

    def test_burn_shares_calldata_matches_abi_encoding(self) -> None:
        self.assertEqual(CANONICAL_A, bytes(Web3.keccak(text="burnShares(bytes32)")[:4]) + MARKET_A)


if __name__ == "__main__":
    unittest.main()

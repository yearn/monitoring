import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from eth_abi import encode as abi_encode
from web3 import Web3

from protocols.morpho import governance_v2
from protocols.morpho._shared import MorphoV2MonitoringError
from protocols.morpho.governance_v2 import PendingConfig, V2GovernanceSnapshot
from protocols.morpho.v2_decoders import submit_data_key
from utils.chains import Chain

A1 = "0x" + "11" * 20
VAULT = "0x" + "aa" * 20


def _selector(sig: str) -> bytes:
    return bytes(Web3.keccak(text=sig)[:4])


def _build(sig: str, types: list[str], values: list[Any]) -> bytes:
    return _selector(sig) + bytes(abi_encode(types, values))


def _snapshot(pending_configs: list[PendingConfig]) -> V2GovernanceSnapshot:
    return V2GovernanceSnapshot(
        name="Sentora PaypalUSD Main",
        address=Web3.to_checksum_address(VAULT),
        chain=Chain.MAINNET,
        owner="",
        curator="",
        sentinels=[],
        allocators=[],
        adapters=[],
        pending_configs=pending_configs,
    )


class TestMorphoV2GovernancePendingLabels(unittest.TestCase):
    def test_resolved_pending_alert_uses_cached_function_name(self) -> None:
        state: dict[str, str] = {}

        def read_value(_filename: str, key: str) -> str | int:
            return state.get(key, 0)

        def write_value(_filename: str, key: str, value: object) -> None:
            state[key] = str(value)

        data = _build("addAdapter(address)", ["address"], [A1])
        data_hash = submit_data_key(data)
        pc = PendingConfig(valid_at=1, function_name="addAdapter", data=data, tx_hash="0x" + "12" * 32)

        sent: list[Any] = []

        with (
            patch("protocols.morpho.governance_v2.get_last_value_for_key_from_file", side_effect=read_value),
            patch("protocols.morpho.governance_v2.write_last_value_to_file", side_effect=write_value),
            patch("protocols.morpho.governance_v2.send_alert", side_effect=sent.append),
        ):
            # First run: the pending config appears and is alerted as a Submit.
            governance_v2.diff_and_alert(_snapshot([pc]))
            # Second run: the cached pending op is gone, so the resolved branch fires.
            governance_v2.diff_and_alert(_snapshot([]))

        function_key = governance_v2.morpho_key(VAULT.lower(), data_hash, governance_v2.PENDING_FUNCTION_TYPE)
        self.assertEqual(state[function_key], "addAdapter")

        self.assertEqual(len(sent), 2)
        # sent[0] is the original Submit; the resolved alert is the second one.
        message = sent[1].message
        self.assertIn("Pending operation `addAdapter()` was executed", message)
        self.assertNotIn(Web3.to_checksum_address(A1), message)
        self.assertNotIn(f"`{data_hash[:10]}…`", message)
        self.assertIn("was executed", message)

    def test_resolved_pending_alert_without_cached_function_keeps_hash_only_message(self) -> None:
        data_hash = "3d6d72861e" + "0" * 54
        snapshot = _snapshot([])
        diff = governance_v2._VaultDiff()
        governance_v2._alert_pending_resolved(data_hash, 1, "", diff)

        sent: list[Any] = []
        with patch("protocols.morpho.governance_v2.send_alert", side_effect=sent.append):
            governance_v2._send_vault_alerts(snapshot, diff.alerts)

        self.assertEqual(len(sent), 1)
        message = sent[0].message
        self.assertIn(f"Pending operation `{data_hash[:10]}…` was executed", message)
        self.assertNotIn(f"(`{data_hash[:10]}…`)", message)


class TestMorphoV2GovernancePendingGrouping(unittest.TestCase):
    def test_multiple_new_pending_grouped_into_single_alert(self) -> None:
        state: dict[str, str] = {}

        def read_value(_filename: str, key: str) -> str | int:
            return state.get(key, 0)

        def write_value(_filename: str, key: str, value: object) -> None:
            state[key] = str(value)

        tx = "0x" + "12" * 32
        data_a = _build("addAdapter(address)", ["address"], [A1])
        data_b = _build("removeAdapter(address)", ["address"], [A1])
        pcs = [
            PendingConfig(valid_at=100, function_name="addAdapter", data=data_a, tx_hash=tx),
            PendingConfig(valid_at=100, function_name="removeAdapter", data=data_b, tx_hash=tx),
        ]

        with (
            patch("protocols.morpho.governance_v2.get_last_value_for_key_from_file", side_effect=read_value),
            patch("protocols.morpho.governance_v2.write_last_value_to_file", side_effect=write_value),
            patch("protocols.morpho.governance_v2.send_alert") as send,
        ):
            governance_v2.diff_and_alert(_snapshot(pcs))

        # Both submissions collapse into one Telegram message.
        self.assertEqual(send.call_count, 1)
        message = send.call_args.args[0].message
        self.assertIn("Submitted 2 operations:", message)
        self.assertIn("addAdapter", message)
        self.assertIn("removeAdapter", message)
        # Shared execution time / tx are rendered once in the footer.
        self.assertEqual(message.count("⏰ Executable at:"), 1)
        self.assertEqual(message.count("🔗 Tx:"), 1)

    def test_single_new_pending_uses_unnumbered_format(self) -> None:
        state: dict[str, str] = {}

        with (
            patch(
                "protocols.morpho.governance_v2.get_last_value_for_key_from_file",
                side_effect=lambda _f, key: state.get(key, 0),
            ),
            patch(
                "protocols.morpho.governance_v2.write_last_value_to_file",
                side_effect=lambda _f, key, value: state.__setitem__(key, str(value)),
            ),
            patch("protocols.morpho.governance_v2.send_alert") as send,
        ):
            pc = PendingConfig(
                valid_at=100,
                function_name="addAdapter",
                data=_build("addAdapter(address)", ["address"], [A1]),
                tx_hash="0x" + "12" * 32,
            )
            governance_v2.diff_and_alert(_snapshot([pc]))

        self.assertEqual(send.call_count, 1)
        message = send.call_args.args[0].message
        self.assertIn("📥 Submitted: addAdapter", message)
        self.assertNotIn("operations:", message)


class TestMorphoV2GovernanceVaultGrouping(unittest.TestCase):
    """One Telegram message per vault, covering every diff category."""

    def _run(self, snapshot: V2GovernanceSnapshot, state: dict[str, Any]) -> list[Any]:
        sent: list[Any] = []
        with (
            patch(
                "protocols.morpho.governance_v2.get_last_value_for_key_from_file",
                side_effect=lambda _f, key: state.get(key, 0),
            ),
            patch("protocols.morpho.governance_v2.write_last_value_to_file"),
            patch("protocols.morpho.governance_v2.send_alert", side_effect=sent.append),
        ):
            governance_v2.diff_and_alert(snapshot)
        return sent

    def test_every_category_collapses_into_one_message(self) -> None:
        snapshot = V2GovernanceSnapshot(
            name="Test Vault",
            address=Web3.to_checksum_address(VAULT),
            chain=Chain.MAINNET,
            owner="0x" + "bb" * 20,
            curator="0x" + "cc" * 20,
            sentinels=[],
            allocators=[],
            adapters=["0x" + "dd" * 20],
            pending_configs=[
                PendingConfig(valid_at=100, function_name="addAdapter", data=b"\x01" * 4, tx_hash="0x" + "11" * 32),
                PendingConfig(valid_at=200, function_name="addAdapter", data=b"\x02" * 4, tx_hash="0x" + "22" * 32),
            ],
        )
        # Seed a non-empty "before" for every set/role so each category diffs.
        old = "0x" + "ee" * 20
        state = {
            governance_v2.morpho_key(VAULT.lower(), "owner", governance_v2.ROLE_TYPE): "0x" + "ff" * 20,
            governance_v2.morpho_key(VAULT.lower(), "curator", governance_v2.ROLE_TYPE): snapshot.curator.lower(),
            governance_v2.morpho_key(VAULT.lower(), "sentinels", governance_v2.SET_TYPE): old.lower(),
            governance_v2.morpho_key(VAULT.lower(), "allocators", governance_v2.SET_TYPE): old.lower(),
            governance_v2.morpho_key(VAULT.lower(), "adapters", governance_v2.SET_TYPE): old.lower(),
        }

        sent = self._run(snapshot, state)

        self.assertEqual(len(sent), 1, f"expected 1 grouped alert, got {len(sent)}")
        alert = sent[0]
        # Highest severity of the group wins: owner change (HIGH) over pending
        # (MEDIUM) and set diffs (LOW).
        self.assertEqual(alert.severity, governance_v2.AlertSeverity.HIGH)
        message = alert.message
        self.assertEqual(message.count("V2 [Test Vault]"), 1)
        self.assertIn("Submitted 2 operations:", message)
        self.assertIn("Owner changed", message)
        self.assertIn("sentinels changed", message)
        self.assertIn("allocators changed", message)
        self.assertIn("adapters changed", message)

    def test_no_diffs_emit_no_message(self) -> None:
        snapshot = V2GovernanceSnapshot(
            name="Quiet Vault",
            address=Web3.to_checksum_address(VAULT),
            chain=Chain.MAINNET,
            owner="0x" + "ff" * 20,
            curator="0x" + "ff" * 20,
            sentinels=[],
            allocators=[],
            adapters=[],
            pending_configs=[],
        )
        state = {
            governance_v2.morpho_key(VAULT.lower(), "owner", governance_v2.ROLE_TYPE): snapshot.owner.lower(),
            governance_v2.morpho_key(VAULT.lower(), "curator", governance_v2.ROLE_TYPE): snapshot.curator.lower(),
        }

        self.assertEqual(self._run(snapshot, state), [])

    def test_low_severity_only_group_stays_low(self) -> None:
        snapshot = V2GovernanceSnapshot(
            name="Low Vault",
            address=Web3.to_checksum_address(VAULT),
            chain=Chain.MAINNET,
            owner="0x" + "ff" * 20,
            curator="0x" + "ff" * 20,
            sentinels=[],
            allocators=["0x" + "aa" * 20],
            adapters=[],
            pending_configs=[],
        )
        state = {
            governance_v2.morpho_key(VAULT.lower(), "owner", governance_v2.ROLE_TYPE): snapshot.owner.lower(),
            governance_v2.morpho_key(VAULT.lower(), "curator", governance_v2.ROLE_TYPE): snapshot.curator.lower(),
            # Non-empty baseline so the allocator diff is not silent seeding.
            governance_v2.morpho_key(VAULT.lower(), "allocators", governance_v2.SET_TYPE): "0x" + "ee" * 20,
        }

        sent = self._run(snapshot, state)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].severity, governance_v2.AlertSeverity.LOW)

    def test_oversized_group_splits_into_numbered_parts(self) -> None:
        """Sections beyond one Telegram message split instead of being truncated.

        Each pending op carries its own executable-at and tx line when the batch
        does not share them, so enough of them exceed the 4096-char cap.
        """
        pending = [
            PendingConfig(
                valid_at=1800000000 + i,
                function_name="increaseTimelock",
                data=bytes([i]) * 4,
                tx_hash="0x" + f"{i:02x}" * 32,
            )
            for i in range(30)
        ]
        # One section per op: distinct validAt/tx keeps them from collapsing, and
        # a separate diff category per op is not needed to exceed the cap.
        diff = governance_v2._VaultDiff()
        snapshot = _snapshot(pending)
        for pc in pending:
            governance_v2._alert_pending_new(snapshot, [(pc, "increaseTimelock(setSendAssetsGate → 604800s)")], diff)

        sent: list[Any] = []
        with patch("protocols.morpho.governance_v2.send_alert", side_effect=sent.append):
            governance_v2._send_vault_alerts(snapshot, diff.alerts)

        self.assertGreater(len(sent), 1, "oversized group should split into multiple messages")
        for index, alert in enumerate(sent, start=1):
            self.assertLessEqual(len(alert.message), 4096)
            self.assertIn(f"V2 [{snapshot.name}]", alert.message)
            self.assertIn(f"({index}/{len(sent)})", alert.message)
        combined = "".join(a.message for a in sent)
        self.assertEqual(combined.count("📥 Submitted:"), len(pending))
        for pc in pending:
            self.assertIn(pc.tx_hash, combined)

    def test_oversized_pending_section_splits_without_losing_operations(self) -> None:
        """One batched pending section must not be truncated by Telegram."""
        pending = [
            PendingConfig(
                valid_at=1800000000 + i,
                function_name="increaseTimelock",
                data=bytes([i]) * 4,
                tx_hash="0x" + f"{i:02x}" * 32,
            )
            for i in range(30)
        ]
        snapshot = _snapshot(pending)
        diff = governance_v2._VaultDiff()
        operations: list[tuple[PendingConfig, str]] = []
        for i, pc in enumerate(pending):
            label = f"increaseTimelock(setSendAssetsGate → {604800 + i}s)"
            operations.append((pc, label))
        governance_v2._alert_pending_new(snapshot, operations, diff)

        self.assertEqual(len(diff.alerts), 1, "the regression requires one oversized section")
        self.assertGreater(len(diff.alerts[0].body), governance_v2.MAX_MESSAGE_LENGTH)

        sent: list[Any] = []
        with patch("protocols.morpho.governance_v2.send_alert", side_effect=sent.append):
            governance_v2._send_vault_alerts(snapshot, diff.alerts)

        self.assertGreater(len(sent), 1)
        for alert in sent:
            self.assertLessEqual(len(alert.message), governance_v2.MAX_MESSAGE_LENGTH)
        combined = "".join(alert.message for alert in sent)
        self.assertEqual(combined.count("  • increaseTimelock"), len(pending))
        for pc in pending:
            self.assertIn(pc.tx_hash, combined)

    def test_cache_writes_are_deferred_until_the_send_succeeds(self) -> None:
        """A failed send must leave the cache untouched so the next run retries.

        Writing cursors during the diff pass would mark the change as alerted
        even though nothing was delivered, and ``main`` reports the failure
        without ever re-sending it.
        """
        pc = PendingConfig(
            valid_at=1800000000,
            function_name="addAdapter",
            data=_build("addAdapter(address)", ["address"], [A1]),
            tx_hash="0x" + "12" * 32,
        )
        snapshot = _snapshot([pc])

        with (
            patch(
                "protocols.morpho.governance_v2.get_last_value_for_key_from_file",
                side_effect=lambda _f, _key: 0,
            ),
            patch("protocols.morpho.governance_v2.write_last_value_to_file") as write,
            patch("protocols.morpho.governance_v2.send_alert", side_effect=RuntimeError("telegram down")),
        ):
            with self.assertRaises(RuntimeError):
                governance_v2.diff_and_alert(snapshot)
            write.assert_not_called()

        # Same snapshot, working Telegram: the cursors are committed this time.
        sent: list[Any] = []
        with (
            patch(
                "protocols.morpho.governance_v2.get_last_value_for_key_from_file",
                side_effect=lambda _f, _key: 0,
            ),
            patch("protocols.morpho.governance_v2.write_last_value_to_file") as write,
            patch("protocols.morpho.governance_v2.send_alert", side_effect=sent.append),
        ):
            governance_v2.diff_and_alert(snapshot)

        self.assertEqual(len(sent), 1)
        written_keys = {call.args[1] for call in write.call_args_list}
        self.assertIn(governance_v2.morpho_key(VAULT.lower(), pc.data_hash, governance_v2.PENDING_TYPE), written_keys)


class TestMorphoV2GovernanceFetch(unittest.TestCase):
    def test_fetch_fails_if_api_omits_configured_vaults(self) -> None:
        response = MagicMock()
        response.json.return_value = {"data": {"vaultV2s": {"items": []}}}

        with (
            patch("protocols.morpho._shared.request_with_retry", return_value=response),
            self.assertRaisesRegex(MorphoV2MonitoringError, "omitted configured Vault V2 governance"),
        ):
            governance_v2.fetch_governance_snapshots()


if __name__ == "__main__":
    unittest.main()

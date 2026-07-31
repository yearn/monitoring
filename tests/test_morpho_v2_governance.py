import unittest
from unittest.mock import patch

from eth_abi import encode as abi_encode
from web3 import Web3

from protocols.morpho import governance_v2
from protocols.morpho.governance_v2 import PendingConfig, V2GovernanceSnapshot
from protocols.morpho.v2_decoders import submit_data_key
from utils.chains import Chain

A1 = "0x" + "11" * 20
VAULT = "0x" + "aa" * 20


def _selector(sig: str) -> bytes:
    return bytes(Web3.keccak(text=sig)[:4])


def _build(sig: str, types: list[str], values: list) -> bytes:
    return _selector(sig) + abi_encode(types, values)


def _snapshot(pending_configs: list[PendingConfig]) -> V2GovernanceSnapshot:
    return V2GovernanceSnapshot(
        name="Sentora PaypalUSD Main",
        address=Web3.to_checksum_address(VAULT),
        chain=Chain.MAINNET,
        risk_level=3,
        owner="",
        curator="",
        sentinels=[],
        allocators=[],
        adapters=[],
        pending_configs=pending_configs,
    )


class TestMorphoV2GovernancePendingLabels(unittest.TestCase):
    def test_resolved_pending_alert_uses_cached_function_name(self):
        state: dict[str, str] = {}

        def read_value(_filename: str, key: str):
            return state.get(key, 0)

        def write_value(_filename: str, key: str, value):
            state[key] = str(value)

        data = _build("addAdapter(address)", ["address"], [A1])
        data_hash = submit_data_key(data)
        pc = PendingConfig(valid_at=1, function_name="addAdapter", data=data, tx_hash="0x" + "12" * 32)

        sent_calls: list = []

        def capture(alert):
            sent_calls.append(alert)

        with (
            patch("protocols.morpho.governance_v2.get_last_value_for_key_from_file", side_effect=read_value),
            patch("protocols.morpho.governance_v2.write_last_value_to_file", side_effect=write_value),
            patch("protocols.morpho.governance_v2.send_alert", side_effect=capture),
        ):
            # First call: pending config appears, buffered, then flushed as one alert.
            alerts: list = []
            governance_v2._diff_pending(_snapshot([pc]), alerts)
            governance_v2._send_vault_alerts(_snapshot([pc]), alerts)
            # Second call: the cached pending op is no longer present, so the
            # resolved-pending branch fires.
            alerts2: list = []
            governance_v2._diff_pending(_snapshot([]), alerts2)
            governance_v2._send_vault_alerts(_snapshot([]), alerts2)

        function_key = governance_v2.morpho_key(VAULT.lower(), data_hash, governance_v2.PENDING_FUNCTION_TYPE)
        self.assertEqual(state[function_key], "addAdapter")

        self.assertEqual(len(sent_calls), 2)
        # The second alert (resolved) is the one we assert on — it's the one with
        # "was executed". The first one is the original Submit.
        resolved_alert = sent_calls[1]
        self.assertIn("Pending operation `addAdapter()` was executed", resolved_alert.message)
        self.assertNotIn(Web3.to_checksum_address(A1), resolved_alert.message)
        self.assertNotIn(f"`{data_hash[:10]}…`", resolved_alert.message)
        self.assertIn("was executed", resolved_alert.message)

    def test_resolved_pending_alert_without_cached_function_keeps_hash_only_message(self):
        data_hash = "3d6d72861e" + "0" * 54
        snapshot = _snapshot([])
        alerts: list = []
        governance_v2._alert_pending_resolved(snapshot, data_hash, 1, "", alerts)
        # The buffered body is what the previous test checked, but now the alert
        # is built into a grouped message — flush it and inspect the body.
        sent: list = []
        with patch("protocols.morpho.governance_v2.send_alert", side_effect=sent.append):
            governance_v2._send_vault_alerts(snapshot, alerts)
        self.assertEqual(len(sent), 1)
        message = sent[0].message
        self.assertIn(f"Pending operation `{data_hash[:10]}…` was executed", message)
        self.assertNotIn(f"(`{data_hash[:10]}…`)", message)

    def test_multiple_alerts_for_one_vault_are_grouped_into_single_message(self):
        """A vault with several simultaneous changes should fire ONE Telegram message.

        3 new pending submits + 1 owner change + 1 adapter swap = 1 alert with
        5 sections under one header. Verifies the grouping refactor.
        """
        snapshot = V2GovernanceSnapshot(
            name="Test Vault",
            address=Web3.to_checksum_address(VAULT),
            chain=Chain.MAINNET,
            risk_level=1,
            owner="0x" + "bb" * 20,  # current owner
            curator="0x" + "cc" * 20,
            sentinels=[],
            allocators=[],
            adapters=["0x" + "dd" * 20],  # current adapter
            pending_configs=[
                PendingConfig(valid_at=100, function_name="addAdapter", data=b"\x01" * 4, tx_hash="0x" + "11" * 32),
                PendingConfig(valid_at=200, function_name="addAdapter", data=b"\x02" * 4, tx_hash="0x" + "22" * 32),
                PendingConfig(valid_at=300, function_name="addAdapter", data=b"\x03" * 4, tx_hash="0x" + "33" * 32),
            ],
        )

        sent: list = []
        # Seed every cache key with a non-empty "before" state so the diff fires
        # for owner, sentinels, allocators, and adapters (not just the pending ones).
        old_adapter = "0x" + "ee" * 20
        state: dict = {
            governance_v2.morpho_key(VAULT.lower(), "owner", "v2_role"): "0x" + "ff" * 20,
            governance_v2.morpho_key(VAULT.lower(), "curator", "v2_role"): (snapshot.curator or "").lower(),
            governance_v2.morpho_key(VAULT.lower(), "sentinels", "v2_set"): old_adapter.lower(),
            governance_v2.morpho_key(VAULT.lower(), "allocators", "v2_set"): old_adapter.lower(),
            governance_v2.morpho_key(VAULT.lower(), "adapters", "v2_set"): old_adapter.lower(),
        }
        with (
            patch(
                "protocols.morpho.governance_v2.get_last_value_for_key_from_file",
                side_effect=lambda _f, k: state.get(k, 0),
            ),
            patch("protocols.morpho.governance_v2.write_last_value_to_file"),
            patch("protocols.morpho.governance_v2.send_alert", side_effect=sent.append),
        ):
            governance_v2.diff_and_alert(snapshot)

        # Exactly ONE Telegram message should have been sent (the grouped alert).
        self.assertEqual(len(sent), 1, f"expected 1 grouped alert, got {len(sent)}")
        alert = sent[0]
        # Highest severity wins: 1 owner change (HIGH) > 3 pending (MEDIUM) > 1 set (LOW).
        self.assertEqual(alert.severity, governance_v2.AlertSeverity.HIGH)
        # Header once + 3 submitted sections + 1 owner change + 3 set changes.
        message = alert.message
        self.assertIn("V2 [Test Vault]", message)
        self.assertIn("📥 Submitted", message)
        self.assertEqual(message.count("📥 Submitted:"), 3)
        self.assertIn("Owner changed", message)
        self.assertIn("sentinels changed", message)
        self.assertIn("allocators changed", message)
        self.assertIn("adapters changed", message)
        # All three pending Txs are present.
        for tx in ("0x" + "11" * 32, "0x" + "22" * 32, "0x" + "33" * 32):
            self.assertIn(tx, message)

    def test_no_alerts_emits_no_message(self):
        """Vault with no diffs should not produce a Telegram message at all."""
        snapshot = V2GovernanceSnapshot(
            name="Quiet Vault",
            address=Web3.to_checksum_address(VAULT),
            chain=Chain.MAINNET,
            risk_level=1,
            owner="0x" + "ff" * 20,
            curator="0x" + "ff" * 20,
            sentinels=[],
            allocators=[],
            adapters=[],
            pending_configs=[],
        )
        sent: list = []
        # Seed every cache key the diff functions consult, so no diffs fire.
        state: dict = {
            governance_v2.morpho_key(VAULT.lower(), "owner", "v2_role"): (snapshot.owner or "").lower(),
            governance_v2.morpho_key(VAULT.lower(), "curator", "v2_role"): (snapshot.curator or "").lower(),
            governance_v2.morpho_key(VAULT.lower(), "sentinels", "v2_set"): "",
            governance_v2.morpho_key(VAULT.lower(), "allocators", "v2_set"): "",
            governance_v2.morpho_key(VAULT.lower(), "adapters", "v2_set"): "",
        }
        with (
            patch(
                "protocols.morpho.governance_v2.get_last_value_for_key_from_file",
                side_effect=lambda _f, k: state.get(k, 0),
            ),
            patch("protocols.morpho.governance_v2.write_last_value_to_file"),
            patch("protocols.morpho.governance_v2.send_alert", side_effect=sent.append),
        ):
            governance_v2.diff_and_alert(snapshot)
        self.assertEqual(sent, [])

    def test_only_low_severity_changes_emit_low_severity_alert(self):
        """Pure allocator/pending-resolved diffs → LOW, not the high of HIGH/CRITICAL fallback."""
        snapshot = V2GovernanceSnapshot(
            name="Low Vault",
            address=Web3.to_checksum_address(VAULT),
            chain=Chain.MAINNET,
            risk_level=1,
            owner="0x" + "ff" * 20,
            curator="0x" + "ff" * 20,
            sentinels=[],
            allocators=["0x" + "aa" * 20],  # newly added
            adapters=[],
            pending_configs=[],
        )
        sent: list = []
        # Seed the allocators set with a non-empty "before" so the diff actually fires
        # (first-run cache seeding is silent by design).
        state: dict = {
            governance_v2.morpho_key(VAULT.lower(), "owner", "v2_role"): (snapshot.owner or "").lower(),
            governance_v2.morpho_key(VAULT.lower(), "curator", "v2_role"): (snapshot.curator or "").lower(),
            governance_v2.morpho_key(VAULT.lower(), "sentinels", "v2_set"): "",
            governance_v2.morpho_key(VAULT.lower(), "allocators", "v2_set"): "0x" + "ee" * 20,
            governance_v2.morpho_key(VAULT.lower(), "adapters", "v2_set"): "",
        }
        with (
            patch(
                "protocols.morpho.governance_v2.get_last_value_for_key_from_file",
                side_effect=lambda _f, k: state.get(k, 0),
            ),
            patch("protocols.morpho.governance_v2.write_last_value_to_file"),
            patch("protocols.morpho.governance_v2.send_alert", side_effect=sent.append),
        ):
            governance_v2.diff_and_alert(snapshot)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].severity, governance_v2.AlertSeverity.LOW)


if __name__ == "__main__":
    unittest.main()

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

        with (
            patch("protocols.morpho.governance_v2.get_last_value_for_key_from_file", side_effect=read_value),
            patch("protocols.morpho.governance_v2.write_last_value_to_file", side_effect=write_value),
            patch("protocols.morpho.governance_v2.send_alert") as send,
        ):
            governance_v2._diff_pending(_snapshot([pc]))
            send.reset_mock()

            governance_v2._diff_pending(_snapshot([]))

        function_key = governance_v2.morpho_key(VAULT.lower(), data_hash, governance_v2.PENDING_FUNCTION_TYPE)
        self.assertEqual(state[function_key], "addAdapter")

        alert = send.call_args.args[0]
        self.assertIn("Pending operation `addAdapter()` was executed", alert.message)
        self.assertNotIn(Web3.to_checksum_address(A1), alert.message)
        self.assertNotIn(f"`{data_hash[:10]}…`", alert.message)
        self.assertIn("was executed", alert.message)

    def test_resolved_pending_alert_without_cached_function_keeps_hash_only_message(self) -> None:
        data_hash = "3d6d72861e" + "0" * 54

        with patch("protocols.morpho.governance_v2.send_alert") as send:
            governance_v2._alert_pending_resolved(_snapshot([]), data_hash, 1, "")

        alert = send.call_args.args[0]
        self.assertIn(f"Pending operation `{data_hash[:10]}…` was executed", alert.message)
        self.assertNotIn(f"(`{data_hash[:10]}…`)", alert.message)


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
            governance_v2._diff_pending(_snapshot(pcs))

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
            governance_v2._diff_pending(_snapshot([pc]))

        self.assertEqual(send.call_count, 1)
        message = send.call_args.args[0].message
        self.assertIn("📥 Submitted: addAdapter", message)
        self.assertNotIn("operations:", message)


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

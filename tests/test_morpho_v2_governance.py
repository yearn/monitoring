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

    def test_resolved_pending_alert_without_cached_function_keeps_hash_only_message(self):
        data_hash = "3d6d72861e" + "0" * 54

        with patch("protocols.morpho.governance_v2.send_alert") as send:
            governance_v2._alert_pending_resolved(_snapshot([]), data_hash, 1, "")

        alert = send.call_args.args[0]
        self.assertIn(f"Pending operation `{data_hash[:10]}…` was executed", alert.message)
        self.assertNotIn(f"(`{data_hash[:10]}…`)", alert.message)


if __name__ == "__main__":
    unittest.main()

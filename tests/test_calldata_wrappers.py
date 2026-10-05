"""Tests for utils/calldata/wrappers.py — unwrapping governance wrapper calls."""

import unittest
from unittest.mock import patch

from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector
from eth_utils import to_checksum_address as _cs

from utils.calldata.wrappers import InnerCall, is_wrapper_call, unwrap_calls, unwrap_executed_calls

ORACLE = _cs("0xcd7f45566bc0e7303fb92a93969bb4d3f6e662bb")
CUSD = _cs("0xcccc62962d17b8914c62d74ffb843d73b2a3cccc")
NEW_ORACLE_IMPL = _cs("0x4607a3cb26190c51d05429082944fb8b2703b622")
NEW_CUSD_IMPL = _cs("0xbdaa34082f1a1a3c32190a672bc9dd1b69791b97")
ZERO32 = b"\x00" * 32


def encode_call(sig: str, types: list[str], vals: list) -> str:
    return "0x" + function_signature_to_4byte_selector(sig).hex() + encode(types, vals).hex()


def upgrade_to_and_call(impl: str) -> str:
    return encode_call("upgradeToAndCall(address,bytes)", ["address", "bytes"], [impl, b""])


def execute_batch(targets: list[str], payloads: list[str]) -> str:
    """The CAP Safe tx: TimelockController.executeBatch of two upgradeToAndCall calls."""
    return encode_call(
        "executeBatch(address[],uint256[],bytes[],bytes32,bytes32)",
        ["address[]", "uint256[]", "bytes[]", "bytes32", "bytes32"],
        [targets, [0] * len(targets), [bytes.fromhex(p[2:]) for p in payloads], ZERO32, ZERO32],
    )


class TestUnwrapCalls(unittest.TestCase):
    def test_execute_batch_yields_each_call_in_order(self) -> None:
        data = execute_batch([ORACLE, CUSD], [upgrade_to_and_call(NEW_ORACLE_IMPL), upgrade_to_and_call(NEW_CUSD_IMPL)])
        self.assertEqual(
            unwrap_calls(data),
            [
                InnerCall(ORACLE, upgrade_to_and_call(NEW_ORACLE_IMPL), "executeBatch call 1"),
                InnerCall(CUSD, upgrade_to_and_call(NEW_CUSD_IMPL), "executeBatch call 2"),
            ],
        )

    def test_schedule_batch(self) -> None:
        data = encode_call(
            "scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)",
            ["address[]", "uint256[]", "bytes[]", "bytes32", "bytes32", "uint256"],
            [[ORACLE], [0], [bytes.fromhex(upgrade_to_and_call(NEW_ORACLE_IMPL)[2:])], ZERO32, ZERO32, 86400],
        )
        self.assertEqual(unwrap_calls(data), [InnerCall(ORACLE, upgrade_to_and_call(NEW_ORACLE_IMPL), "scheduleBatch")])

    def test_single_schedule_and_execute(self) -> None:
        payload = bytes.fromhex(upgrade_to_and_call(NEW_ORACLE_IMPL)[2:])
        schedule = encode_call(
            "schedule(address,uint256,bytes,bytes32,bytes32,uint256)",
            ["address", "uint256", "bytes", "bytes32", "bytes32", "uint256"],
            [ORACLE, 0, payload, ZERO32, ZERO32, 86400],
        )
        execute = encode_call(
            "execute(address,uint256,bytes,bytes32,bytes32)",
            ["address", "uint256", "bytes", "bytes32", "bytes32"],
            [ORACLE, 0, payload, ZERO32, ZERO32],
        )
        self.assertEqual(unwrap_calls(schedule), [InnerCall(ORACLE, upgrade_to_and_call(NEW_ORACLE_IMPL), "schedule")])
        self.assertEqual(unwrap_calls(execute), [InnerCall(ORACLE, upgrade_to_and_call(NEW_ORACLE_IMPL), "execute")])

    def test_compound_queue_with_signature_prepends_its_selector(self) -> None:
        args = encode(["address", "bytes"], [NEW_ORACLE_IMPL, b""])
        data = encode_call(
            "queueTransaction(address,uint256,string,bytes,uint256)",
            ["address", "uint256", "string", "bytes", "uint256"],
            [ORACLE, 0, "upgradeToAndCall(address,bytes)", args, 1_800_000_000],
        )
        self.assertEqual(
            unwrap_calls(data), [InnerCall(ORACLE, upgrade_to_and_call(NEW_ORACLE_IMPL), "queueTransaction")]
        )

    def test_compound_execute_with_empty_signature_uses_data_as_is(self) -> None:
        payload = bytes.fromhex(upgrade_to_and_call(NEW_ORACLE_IMPL)[2:])
        data = encode_call(
            "executeTransaction(address,uint256,string,bytes,uint256)",
            ["address", "uint256", "string", "bytes", "uint256"],
            [ORACLE, 0, "", payload, 1_800_000_000],
        )
        self.assertEqual(
            unwrap_calls(data), [InnerCall(ORACLE, upgrade_to_and_call(NEW_ORACLE_IMPL), "executeTransaction")]
        )

    def test_maple_schedule_proposals(self) -> None:
        data = encode_call(
            "scheduleProposals(address[],bytes[])",
            ["address[]", "bytes[]"],
            [[ORACLE], [bytes.fromhex(upgrade_to_and_call(NEW_ORACLE_IMPL)[2:])]],
        )
        self.assertEqual(
            unwrap_calls(data), [InnerCall(ORACLE, upgrade_to_and_call(NEW_ORACLE_IMPL), "scheduleProposals")]
        )

    def test_non_wrapper_and_empty_calldata(self) -> None:
        self.assertEqual(unwrap_calls(upgrade_to_and_call(NEW_ORACLE_IMPL)), [])
        self.assertEqual(unwrap_calls("0x"), [])
        self.assertEqual(unwrap_calls(""), [])
        self.assertFalse(is_wrapper_call(""))

    def test_malformed_wrapper_calldata_is_empty_not_an_error(self) -> None:
        truncated = execute_batch([ORACLE], [upgrade_to_and_call(NEW_ORACLE_IMPL)])[:80]
        self.assertTrue(is_wrapper_call(truncated))
        self.assertEqual(unwrap_calls(truncated), [])

    def test_unwrapping_never_hits_the_network(self) -> None:
        data = execute_batch([ORACLE], [upgrade_to_and_call(NEW_ORACLE_IMPL)])
        with patch("utils.calldata.decoder.fetch_json", side_effect=AssertionError("4byte lookup")):
            self.assertEqual(len(unwrap_calls(data)), 1)


if __name__ == "__main__":
    unittest.main()


class TestUnwrapExecutedCalls(unittest.TestCase):
    """Only execute-type wrappers run their inner calls in the same transaction."""

    def test_execute_batch_inner_calls_run_now(self) -> None:
        data = execute_batch([ORACLE, CUSD], [upgrade_to_and_call(NEW_ORACLE_IMPL), upgrade_to_and_call(NEW_CUSD_IMPL)])
        self.assertEqual(unwrap_executed_calls(data), unwrap_calls(data))

    def test_schedule_batch_inner_calls_do_not_run_now(self) -> None:
        data = encode_call(
            "scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)",
            ["address[]", "uint256[]", "bytes[]", "bytes32", "bytes32", "uint256"],
            [[ORACLE], [0], [bytes.fromhex(upgrade_to_and_call(NEW_ORACLE_IMPL)[2:])], ZERO32, ZERO32, 86400],
        )
        self.assertEqual(unwrap_executed_calls(data), [])

    def test_non_wrapper(self) -> None:
        self.assertEqual(unwrap_executed_calls(upgrade_to_and_call(NEW_ORACLE_IMPL)), [])
        self.assertEqual(unwrap_executed_calls(""), [])

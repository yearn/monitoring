"""Tests for the timelock-execution protocol-context adapter."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall
from utils.llm import timelock_execution_context
from utils.llm.timelock_execution_context import (
    TimelockExecutionContext,
    format_timelock_execution_report,
    operation_id,
    resolve_timelock_execution_context,
)

EXECUTOR = "0xF8f60BF9456A6e0141149Db2DD6f02C60da5779B"
TIMELOCK = "0x88Ba032be87d5EF1fbE87336B7090767F367BF73"
RECOVERY = "0xd7a540ba3626c0aa66e7DB4088971d0CD64695B6"
WETH2 = "0xAc37729B76db6438CE62042AE1270ee574CA7571"
ZERO32 = b"\x00" * 32
WEEK = 7 * 86400
READY_AT = 1_790_961_779  # 2026-10-02 17:22:59 UTC

# The executeBatch Strategist Safe nonce 3356 sent to Yearn's TimelockExecutor.
PAYLOADS = [
    "c2e73cca000000000000000000000000fac55fafd0b55bfb8dd41f735efcc195ada9891f"
    "0000000000000000000000000000000000000000000000000000000000000001",
    "b9ddcd68000000000000000000000000fac55fafd0b55bfb8dd41f735efcc195ada9891f"
    "00000000000000000000000000000000000000000000001b1ae4d6e2ef500000",
    "c2e73cca00000000000000000000000068a14629cb07c74259f481382fe8b6cfd8970121"
    "0000000000000000000000000000000000000000000000000000000000000000",
    "b9ddcd6800000000000000000000000068a14629cb07c74259f481382fe8b6cfd8970121"
    "00000000000000000000000000000000000000000000021e19e0c9bab2400000",
]
EXECUTE_BATCH = DecodedCall(
    "executeBatch",
    "executeBatch(address[],uint256[],bytes[],bytes32,bytes32)",
    [
        ("address[]", [RECOVERY, RECOVERY, WETH2, WETH2]),
        ("uint256[]", [0, 0, 0, 0]),
        ("bytes[]", [bytes.fromhex(p) for p in PAYLOADS]),
        ("bytes32", ZERO32),
        ("bytes32", ZERO32),
    ],
)
# hashOperationBatch(...) read from the Yearn TimelockController for the call above.
OPERATION_ID = "0x25864e54465a13f34e204660f5803e80ec9cd14c0d5b82bdbfffd2859b2b377d"


def _context(ready_at: int, now: int = READY_AT + 3 * 86400) -> TimelockExecutionContext:
    return TimelockExecutionContext(
        target=EXECUTOR,
        target_label="TimelockExecutor",
        timelock=TIMELOCK,
        timelock_label="Yearn TimelockController",
        signature=EXECUTE_BATCH.signature,
        operation_id=OPERATION_ID,
        call_count=4,
        min_delay=WEEK,
        ready_at=ready_at,
        now=now,
    )


class TestOperationId(unittest.TestCase):
    def test_matches_on_chain_hash_operation_batch(self) -> None:
        self.assertEqual(operation_id(EXECUTE_BATCH), OPERATION_ID)


class TestStatus(unittest.TestCase):
    def test_ready_operation_names_route_and_schedule_bound(self) -> None:
        lines = _context(READY_AT).lines()
        self.assertIn(f"executeBatch via {EXECUTOR} (TimelockExecutor) forwards to timelock {TIMELOCK}", lines[0])
        self.assertIn(f"releases operation {OPERATION_ID} (4 calls); timelock min delay 7d", lines[0])
        self.assertIn("ready since 2026-10-02 17:22 UTC (so scheduled no later than 2026-09-25 17:22 UTC)", lines[1])
        self.assertIn("not new proposals", lines[2])

    def test_unscheduled_done_and_early_make_no_queue_claim(self) -> None:
        unscheduled = "\n".join(_context(0).lines())
        early = "\n".join(_context(READY_AT, now=READY_AT - 60).lines())
        done = "\n".join(_context(1).lines())
        self.assertIn("NOT SCHEDULED", unscheduled)
        self.assertIn("have NOT gone through the delay", unscheduled)
        self.assertIn("NOT READY until 2026-10-02 17:22 UTC", early)
        self.assertIn("The delay has NOT elapsed", early)
        self.assertIn("ALREADY EXECUTED", done)
        for text in (unscheduled, early, done):
            self.assertNotIn("sat publicly in the timelock queue", text)

    def test_report(self) -> None:
        report = format_timelock_execution_report([_context(READY_AT)], 1, {})
        self.assertIn(f"**Operation ID:** `{OPERATION_ID}` (4 call(s))", report)
        self.assertIn("**Min delay:** 7d", report)


class TestResolve(unittest.TestCase):
    @patch.object(timelock_execution_context, "get_contract_label", return_value="")
    @patch.object(timelock_execution_context, "ChainManager")
    @patch.object(timelock_execution_context, "call_view")
    def test_follows_forwarding_executor_to_its_timelock(
        self, mock_call: MagicMock, mock_cm: MagicMock, _label: MagicMock
    ) -> None:
        answers = {
            (EXECUTOR, "getMinDelay()"): None,
            (EXECUTOR, "TIMELOCK()"): TIMELOCK.lower(),
            (TIMELOCK, "getMinDelay()"): WEEK,
            (TIMELOCK, "getTimestamp(bytes32)"): READY_AT,
        }
        mock_call.side_effect = lambda client, address, signature, output, args=(): answers.get((address, signature))
        mock_cm.get_client.return_value.eth.get_block.return_value = {"timestamp": READY_AT + 60}

        (context,) = resolve_timelock_execution_context("YEARN_MS", 1, [(EXECUTOR, EXECUTE_BATCH)])
        self.assertEqual((context.timelock, context.operation_id, context.ready_at), (TIMELOCK, OPERATION_ID, READY_AT))
        self.assertTrue(context.via_executor)

    @patch.object(timelock_execution_context, "ChainManager")
    def test_ignores_other_calls_without_rpc(self, mock_cm: MagicMock) -> None:
        call = DecodedCall("update_debt", "update_debt(address,uint256)", [("address", WETH2), ("uint256", 1)])
        self.assertEqual(resolve_timelock_execution_context("YEARN_MS", 1, [(RECOVERY, call)]), [])
        mock_cm.get_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()

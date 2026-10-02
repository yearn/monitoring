import unittest
from unittest.mock import MagicMock, patch

from protocols.timelock import stale_operations
from protocols.timelock.stale_operations import (
    DAY,
    Operation,
    format_operation,
    load_scheduled_operations,
)
from protocols.timelock.stale_operations import (
    stale_operations as find_stale,
)
from protocols.timelock.timelock_alerts import TIMELOCKS

SHORT = TIMELOCKS[("0x4b174afbed7b98ba01f50e36109eee5e6d327c32", 1)]
RATE_MANAGER = "0x11F6FAb3f4D8635880C3e80cbae8AEF8136D4189"
SET_RATE = "0x2bdb70970000000000000000000000007912eaff92b2f5bc64cdd21c76d79ffc12ea855e0000000000000000000000000000000000000000000000000ec5bd0d5ee40000"
NOW = 1_791_000_000


def _operation(op_id: str = "0x" + "11" * 32) -> Operation:
    return Operation(SHORT, op_id, NOW - 40 * DAY, "0x" + "ab" * 32, ((RATE_MANAGER, SET_RATE),))


def _event(event_id: str, op_id: str, index: int, ts: int = NOW - 40 * DAY, kind: str = "TimelockController") -> dict:
    return {
        "id": event_id,
        "timelockAddress": "0x4B174afbeD7b98BA01F50E36109EEE5e6d327c32",
        "timelockType": kind,
        "eventName": "CallScheduled",
        "chainId": 1,
        "blockTimestamp": ts,
        "transactionHash": "0x" + "ab" * 32,
        "operationId": op_id,
        "index": index,
        "target": RATE_MANAGER,
        "data": SET_RATE,
    }


class TestStaleSelection(unittest.TestCase):
    def test_classifies_by_get_timestamp(self) -> None:
        ops = [_operation("0x" + c * 64) for c in "abcde"]
        ready = {
            stale_operations._key(ops[0]): NOW - 39 * DAY,  # ready for 39 days -> stale
            stale_operations._key(ops[1]): 1,  # executed
            stale_operations._key(ops[2]): 0,  # cancelled
            stale_operations._key(ops[3]): NOW - 2 * DAY,  # ready, but recently
            stale_operations._key(ops[4]): NOW + DAY,  # still waiting
        }
        stale = find_stale(ops, ready, NOW)
        self.assertEqual([op.operation_id for op, _ in stale], [ops[0].operation_id])

    def test_unreadable_state_is_skipped(self) -> None:
        self.assertEqual(find_stale([_operation()], {}, NOW), [])


class TestLoadScheduledOperations(unittest.TestCase):
    @patch.object(stale_operations, "PAGE_SIZE", 2)
    @patch.object(stale_operations, "load_events")
    def test_groups_batches_across_pages(self, load: MagicMock) -> None:
        op = "0x" + "22" * 32
        page_one = [_event("a", op, 1), _event("b", op, 0)]
        page_two = [_event("b", op, 0), _event("c", "0x" + "33" * 32, 0, kind="Compound")]
        load.side_effect = [
            {"data": {"TimelockEvent": page_one}},
            {"data": {"TimelockEvent": page_two}},
            {"data": {"TimelockEvent": []}},
        ]

        (operation,) = load_scheduled_operations(0) or []

        self.assertEqual(operation.operation_id, op)
        self.assertEqual(len(operation.calls), 2)
        self.assertEqual(load.call_args_list[1].args[1], NOW - 40 * DAY - 1)

    @patch.object(stale_operations, "load_events", return_value=None)
    def test_envio_failure_returns_none(self, _load: MagicMock) -> None:
        self.assertIsNone(load_scheduled_operations(0))


class TestFormatOperation(unittest.TestCase):
    @patch.object(stale_operations, "decode_calldata")
    def test_links_full_addresses_and_counts_days(self, decode: MagicMock) -> None:
        decode.return_value.signature = "setRate(address,uint256)"
        text = format_operation(_operation(), NOW - 39 * DAY, NOW)
        self.assertIn("*Infinifi Shorttimelock* (chain 1)", text)
        self.assertIn(
            "[0x4B174afbeD7b98BA01F50E36109EEE5e6d327c32](https://etherscan.io/address/0x4B174afbeD7b98BA01F50E36109EEE5e6d327c32)",
            text,
        )
        self.assertIn("(39 days)", text)
        self.assertIn(f"[{RATE_MANAGER}](https://etherscan.io/address/{RATE_MANAGER}) `setRate(address,uint256)`", text)


class TestMain(unittest.TestCase):
    @patch.object(stale_operations, "write_last_value_to_file")
    @patch.object(stale_operations, "send_telegram_message")
    @patch.object(stale_operations, "get_last_value_for_key_from_file")
    @patch.object(stale_operations, "format_operation", return_value="op")
    @patch.object(stale_operations, "ready_times")
    @patch.object(stale_operations, "load_scheduled_operations")
    def test_alerts_each_operation_once(
        self,
        load: MagicMock,
        ready: MagicMock,
        _fmt: MagicMock,
        cached: MagicMock,
        send: MagicMock,
        write: MagicMock,
    ) -> None:
        old, already = _operation("0x" + "aa" * 32), _operation("0x" + "bb" * 32)
        load.return_value = [old, already]
        ready.return_value = {stale_operations._key(old): 1, stale_operations._key(already): 1}
        with patch.object(stale_operations, "stale_operations", return_value=[(old, 0), (already, 0)]):
            cached.side_effect = lambda _f, key: "1" if already.operation_id in key else 0
            stale_operations.main()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], "INFINIFI")
        self.assertTrue(send.call_args.kwargs["disable_notification"])
        write.assert_called_once_with(
            stale_operations.cache_filename, f"TIMELOCK_STALE_{stale_operations._key(old)}", 1
        )


class TestYearnMirror(unittest.TestCase):
    @patch.object(stale_operations, "write_last_value_to_file")
    @patch.object(stale_operations, "send_telegram_message")
    @patch.object(stale_operations, "get_last_value_for_key_from_file", return_value=0)
    @patch.object(stale_operations, "format_operation", return_value="op")
    @patch.object(stale_operations, "ready_times", return_value={})
    @patch.object(stale_operations, "load_scheduled_operations")
    def test_yearn_alert_is_mirrored_internally(
        self, load: MagicMock, _ready: MagicMock, _fmt: MagicMock, _cached: MagicMock, send: MagicMock, _w: MagicMock
    ) -> None:
        yearn = TIMELOCKS[("0x88ba032be87d5ef1fbe87336b7090767f367bf73", 1)]
        operation = Operation(yearn, "0x" + "cc" * 32, NOW, "0x" + "ab" * 32, ())
        load.return_value = [operation]
        with patch.object(stale_operations, "stale_operations", return_value=[(operation, 0)]):
            stale_operations.main()
        self.assertEqual([call.args[1] for call in send.call_args_list], ["YEARN_TIMELOCK", "YEARN_TIMELOCK_INTERNAL"])
        self.assertEqual(send.call_args_list[0].kwargs["origin_protocol"], "yearn")


if __name__ == "__main__":
    unittest.main()

"""Tests for timelock/timelock_alerts.py — build_alert_message truncation logic."""

import unittest
import unittest.mock
from unittest.mock import patch

from protocols.timelock.timelock_alerts import (
    TIMELOCKS,
    TimelockConfig,
    _truncate_call_lines,
    build_alert_message,
)
from utils.telegram import MAX_MESSAGE_LENGTH


def test_3jane_seven_day_timelock_is_monitored() -> None:
    timelock = TIMELOCKS[("0x3d3c41419ab401cd25055e8f9421d7d96d887885", 1)]

    assert timelock.protocol == "3JANE"
    assert timelock.label == "3Jane 7d TimelockController"


def _make_event(
    timelock_type: str = "TimelockController",
    chain_id: int = 1,
    target: str = "0x" + "ab" * 20,
    data: str = "0x",
    **overrides: object,
) -> dict:
    """Create a minimal TimelockEvent dict for testing."""
    event: dict = {
        "chainId": str(chain_id),
        "transactionHash": "0x" + "ff" * 32,
        "timelockAddress": "0x" + "aa" * 20,
        "timelockType": timelock_type,
        "operationId": "0x" + "00" * 32,
        "target": target,
        "data": data,
        "value": "0",
        "blockTimestamp": "1700000000",
    }
    event.update(overrides)
    return event


TIMELOCK_INFO = TimelockConfig(
    address="0x" + "aa" * 20,
    chain_id=1,
    protocol="TEST",
    label="Test Timelock",
)


class TestBuildAlertMessageTruncation(unittest.TestCase):
    """Test that build_alert_message respects MAX_MESSAGE_LENGTH and priority."""

    @patch("protocols.timelock.timelock_alerts._get_ai_explanation", return_value=None)
    def test_short_message_no_truncation(self, _mock_ai: object) -> None:
        """A simple message should not be truncated."""
        events = [_make_event()]
        msg = build_alert_message(events, TIMELOCK_INFO)
        self.assertLessEqual(len(msg), MAX_MESSAGE_LENGTH)
        self.assertIn("TIMELOCK: New Operation Scheduled", msg)
        self.assertIn("Test Timelock", msg)

    @patch("protocols.timelock.timelock_alerts._get_ai_explanation", return_value=None)
    def test_long_call_details_truncated(self, _mock_ai: object) -> None:
        """When call details are very long, they should be truncated to fit."""
        events = [
            _make_event(
                index=i,
                target=f"0x{i:040x}",
                data="0x" + "ab" * 200,
            )
            for i in range(30)
        ]
        msg = build_alert_message(events, TIMELOCK_INFO)
        self.assertLessEqual(len(msg), MAX_MESSAGE_LENGTH)
        self.assertIn("truncated", msg)

    @patch("protocols.timelock.timelock_alerts._get_ai_explanation", return_value=None)
    def test_truncation_keeps_markdown_entities_balanced(self, _mock_ai: object) -> None:
        """Truncating call details must not sever a `signature` mid-entity.

        Regression: slicing the joined text mid-line left an unclosed backtick,
        which Telegram rejected with 400 "can't parse entities", freezing the
        dedupe cursor and re-sending every event hourly.
        """
        events = [
            _make_event(
                index=i,
                target=f"0x{i:040x}",
                data="0x" + "ab" * 4,
                signature="update_max_debt_for_strategy(address,uint256)",
            )
            for i in range(60)
        ]
        msg = build_alert_message(events, TIMELOCK_INFO)

        self.assertLessEqual(len(msg), MAX_MESSAGE_LENGTH)
        # Backticks (code spans) and link brackets must come in balanced pairs.
        self.assertEqual(msg.count("`") % 2, 0, "unbalanced backticks would break Telegram Markdown")
        self.assertEqual(msg.count("["), msg.count("]"), "unbalanced link brackets")

    @patch("protocols.timelock.timelock_alerts.format_explanation_line")
    @patch("protocols.timelock.timelock_alerts._get_ai_explanation")
    def test_ai_summary_preserved_over_call_details(self, mock_ai: object, mock_format: object) -> None:
        """AI summary must be preserved even when call details are long."""
        from utils.llm.ai_explainer import Explanation

        ai_summary = "AI says this is a governance transfer with LOW risk."
        explanation = Explanation(summary=ai_summary, detail="")
        mock_ai.return_value = explanation  # type: ignore[union-attr]
        mock_format.return_value = f"\n🤖 *AI Summary:*\n{ai_summary}"  # type: ignore[union-attr]

        events = [
            _make_event(
                index=i,
                target=f"0x{i:040x}",
                data="0x" + "ab" * 200,
            )
            for i in range(30)
        ]
        msg = build_alert_message(events, TIMELOCK_INFO)

        self.assertLessEqual(len(msg), MAX_MESSAGE_LENGTH)
        # AI summary must be fully present
        self.assertIn(ai_summary, msg)
        # Footer (tx link) must be present
        self.assertIn("Tx:", msg)
        # Call details should be truncated
        self.assertIn("truncated", msg)

    @patch("protocols.timelock.timelock_alerts.format_explanation_line")
    @patch("protocols.timelock.timelock_alerts._get_ai_explanation")
    def test_message_under_limit_with_ai(self, mock_ai: object, mock_format: object) -> None:
        """When everything fits, nothing should be truncated."""
        from utils.llm.ai_explainer import Explanation

        explanation = Explanation(summary="Short summary.", detail="")
        mock_ai.return_value = explanation  # type: ignore[union-attr]
        mock_format.return_value = "\n🤖 *AI Summary:*\nShort summary."  # type: ignore[union-attr]

        events = [_make_event()]
        msg = build_alert_message(events, TIMELOCK_INFO)

        self.assertLessEqual(len(msg), MAX_MESSAGE_LENGTH)
        self.assertIn("Short summary.", msg)
        self.assertNotIn("...", msg)

    @patch("protocols.timelock.timelock_alerts._get_ai_explanation")
    def test_ai_skipped_for_governance_protocol(self, mock_ai: object) -> None:
        """Protocols with dedicated governance monitoring skip the AI summary entirely."""
        aave_info = TimelockConfig(
            address="0x" + "bb" * 20,
            chain_id=1,
            protocol="AAVE",
            label="Aave Governance V3",
        )
        msg = build_alert_message([_make_event()], aave_info)

        mock_ai.assert_not_called()  # type: ignore[attr-defined]
        self.assertNotIn("AI Summary", msg)
        self.assertIn("TIMELOCK: New Operation Scheduled", msg)


class TestTruncateCallLines(unittest.TestCase):
    """Direct tests for the _truncate_call_lines helper."""

    def test_fits_completely_without_marker(self) -> None:
        """When all lines fit, no truncation marker is added."""
        lines = ["line one", "line two"]
        result = _truncate_call_lines(lines, 100)
        self.assertEqual(result, "line one\nline two")
        self.assertNotIn("truncated", result)

    def test_truncates_whole_lines_and_keeps_marker(self) -> None:
        """When lines don't fit, drop whole trailing lines and add a marker."""
        lines = ["a" * 50, "b" * 50, "c" * 50]
        # Full text is 152 chars; keep the first two lines (101 chars) and the
        # marker (15 chars plus one newline) for a total of 117 chars.
        result = _truncate_call_lines(lines, 120)
        self.assertIn("a" * 50, result)
        self.assertIn("b" * 50, result)
        self.assertNotIn("c" * 50, result)
        self.assertIn("... (truncated)", result)
        self.assertLessEqual(len(result), 120)

    def test_small_budget_returns_empty_string(self) -> None:
        """When the budget cannot even fit the marker, return an empty string."""
        lines = ["a very long line that cannot possibly fit", "another line"]
        result = _truncate_call_lines(lines, 5)
        self.assertEqual(result, "")

    def test_marker_consistent_length_with_budget(self) -> None:
        """The returned string must never exceed the requested budget."""
        lines = [f"line {i}" for i in range(50)]
        for budget in (0, 1, 10, 50, 100, 1000):
            result = _truncate_call_lines(lines, budget)
            self.assertLessEqual(
                len(result),
                budget,
                f"result exceeded budget {budget}",
            )


class TestMapleProposalUnwrap(unittest.TestCase):
    """Maple ProposalScheduled has no target/data; recover them from the source tx."""

    @staticmethod
    def _make_schedule_calldata(targets: list[str], datas: list[bytes]) -> str:
        from eth_abi import encode
        from eth_utils import function_signature_to_4byte_selector

        selector = function_signature_to_4byte_selector("scheduleProposals(address[],bytes[])")
        body = encode(["address[]", "bytes[]"], [targets, datas])
        return "0x" + selector.hex() + body.hex()

    @staticmethod
    def _wrap_in_safe(inner_hex: str, safe_target: str) -> str:
        from eth_abi import encode
        from eth_utils import function_signature_to_4byte_selector

        selector = function_signature_to_4byte_selector(
            "execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)"
        )
        zero = "0x" + "00" * 20
        body = encode(
            ["address", "uint256", "bytes", "uint8", "uint256", "uint256", "uint256", "address", "address", "bytes"],
            [safe_target, 0, bytes.fromhex(inner_hex[2:]), 0, 0, 0, 0, zero, zero, b""],
        )
        return "0x" + selector.hex() + body.hex()

    @patch("protocols.timelock.timelock_alerts.ChainManager")
    def test_unwraps_safe_wrapped_schedule_proposals(self, mock_cm: object) -> None:
        from protocols.timelock.timelock_alerts import _maple_proposal_calls

        targets = ["0x" + "aa" * 20, "0x" + "bb" * 20]
        datas = [bytes.fromhex("8456cb59"), bytes.fromhex("3f4ba83a")]  # pause(), unpause()
        inner_hex = self._make_schedule_calldata(targets, datas)
        outer = self._wrap_in_safe(inner_hex, "0x2efff88747eb5a3ff00d4d8d0f0800e306c0426b")

        mock_client = unittest.mock.MagicMock()
        mock_client.eth.get_transaction.return_value = {"input": outer}
        mock_cm.get_client.return_value = mock_client  # type: ignore[attr-defined]

        event = _make_event(timelock_type="Maple", transactionHash="0x" + "ff" * 32)
        calls = _maple_proposal_calls(event, chain_id=1)

        assert calls is not None
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["target"], targets[0])
        self.assertEqual(calls[0]["data"], "0x8456cb59")
        self.assertEqual(calls[1]["target"], targets[1])
        self.assertEqual(calls[1]["data"], "0x3f4ba83a")

    @patch("protocols.timelock.timelock_alerts.ChainManager")
    def test_unwraps_direct_schedule_proposals(self, mock_cm: object) -> None:
        from protocols.timelock.timelock_alerts import _maple_proposal_calls

        targets = ["0x" + "cc" * 20]
        datas = [bytes.fromhex("8456cb59")]
        inner_hex = self._make_schedule_calldata(targets, datas)

        mock_client = unittest.mock.MagicMock()
        mock_client.eth.get_transaction.return_value = {"input": inner_hex}
        mock_cm.get_client.return_value = mock_client  # type: ignore[attr-defined]

        event = _make_event(timelock_type="Maple", transactionHash="0x" + "ff" * 32)
        calls = _maple_proposal_calls(event, chain_id=1)
        assert calls is not None
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["data"], "0x8456cb59")

    @patch("protocols.timelock.timelock_alerts.ChainManager")
    def test_returns_none_for_unknown_selector(self, mock_cm: object) -> None:
        from protocols.timelock.timelock_alerts import _maple_proposal_calls

        # proposeRoleUpdates path — we can't synthesize (target, data) pairs from it.
        mock_client = unittest.mock.MagicMock()
        mock_client.eth.get_transaction.return_value = {"input": "0x2d6e853c" + "00" * 100}
        mock_cm.get_client.return_value = mock_client  # type: ignore[attr-defined]

        event = _make_event(timelock_type="Maple", transactionHash="0x" + "ff" * 32)
        self.assertIsNone(_maple_proposal_calls(event, chain_id=1))


if __name__ == "__main__":
    unittest.main()

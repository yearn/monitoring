"""Tests for timelock/timelock_alerts.py — build_alert_message truncation logic."""

import unittest
import unittest.mock
from unittest.mock import MagicMock, patch

from protocols.timelock.timelock_alerts import (
    TimelockConfig,
    build_alert_message,
)
from utils.telegram import MAX_MESSAGE_LENGTH


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
    def test_batch_calls_are_numbered_from_one(self, _mock_ai: object) -> None:
        """Telegram numbering must match the AI report's 1-based call flow."""
        events = [_make_event(index=i, target=f"0x{i + 1:040x}", data="0x8456cb59") for i in range(2)]
        msg = build_alert_message(events, TIMELOCK_INFO)
        self.assertIn("--- Call 1 ---", msg)
        self.assertIn("--- Call 2 ---", msg)
        self.assertNotIn("--- Call 0 ---", msg)

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
    def test_ai_summary_preserved_over_call_details(self, mock_ai: MagicMock, mock_format: MagicMock) -> None:
        """AI summary must be preserved even when call details are long."""
        from utils.llm.ai_explainer import Explanation

        ai_summary = "AI says this is a governance transfer with LOW risk."
        explanation = Explanation(summary=ai_summary, detail="")
        mock_ai.return_value = explanation
        mock_format.return_value = f"\n🤖 *AI Summary:*\n{ai_summary}"

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
    def test_message_under_limit_with_ai(self, mock_ai: MagicMock, mock_format: MagicMock) -> None:
        """When everything fits, nothing should be truncated."""
        from utils.llm.ai_explainer import Explanation

        explanation = Explanation(summary="Short summary.", detail="")
        mock_ai.return_value = explanation
        mock_format.return_value = "\n🤖 *AI Summary:*\nShort summary."

        events = [_make_event()]
        msg = build_alert_message(events, TIMELOCK_INFO)

        self.assertLessEqual(len(msg), MAX_MESSAGE_LENGTH)
        self.assertIn("Short summary.", msg)
        self.assertNotIn("...", msg)

    @patch("protocols.timelock.timelock_alerts._get_ai_explanation")
    def test_ai_skipped_for_governance_protocol(self, mock_ai: MagicMock) -> None:
        """Protocols with dedicated governance monitoring skip the AI summary entirely."""
        aave_info = TimelockConfig(
            address="0x" + "bb" * 20,
            chain_id=1,
            protocol="AAVE",
            label="Aave Governance V3",
        )
        msg = build_alert_message([_make_event()], aave_info)

        mock_ai.assert_not_called()
        self.assertNotIn("AI Summary", msg)
        self.assertIn("TIMELOCK: New Operation Scheduled", msg)


@patch("protocols.timelock.timelock_alerts._get_ai_explanation", return_value=None)
class TestOperationIdLine(unittest.TestCase):
    """The scheduled alert shows the same copyable Operation ID as the stale-operation alert."""

    OPERATION_ID = "0x" + "5f" * 32

    def test_timelock_controller_shows_id_before_calls(self, _mock_ai: object) -> None:
        msg = build_alert_message([_make_event(operationId=self.OPERATION_ID)], TIMELOCK_INFO)
        self.assertIn(f"🆔 Operation ID: `{self.OPERATION_ID}`\n", msg)
        self.assertLess(msg.index("Operation ID"), msg.index("🎯 Target"))

    def test_batch_shows_id_once(self, _mock_ai: object) -> None:
        events = [_make_event(operationId=self.OPERATION_ID, index=i) for i in range(3)]
        msg = build_alert_message(events, TIMELOCK_INFO)
        self.assertEqual(msg.count(self.OPERATION_ID), 1)

    def test_id_survives_call_truncation(self, _mock_ai: object) -> None:
        events = [
            _make_event(operationId=self.OPERATION_ID, index=i, target=f"0x{i:040x}", data="0x" + "ab" * 200)
            for i in range(30)
        ]
        msg = build_alert_message(events, TIMELOCK_INFO)
        self.assertIn("truncated", msg)
        self.assertIn(f"🆔 Operation ID: `{self.OPERATION_ID}`", msg)

    def test_compound_and_unknown_types_show_id(self, _mock_ai: object) -> None:
        for timelock_type in ("Compound", "SomethingNew"):
            with self.subTest(timelock_type=timelock_type):
                event = _make_event(timelock_type, operationId=self.OPERATION_ID)
                msg = build_alert_message([event], TIMELOCK_INFO)
                self.assertIn(f"🆔 Operation ID: `{self.OPERATION_ID}`", msg)

    def test_governance_types_keep_their_own_id_label(self, _mock_ai: object) -> None:
        for timelock_type, label in (("Aave", "Proposal"), ("Lido", "Vote"), ("Maple", "Proposal")):
            with self.subTest(timelock_type=timelock_type):
                msg = build_alert_message([_make_event(timelock_type, operationId="42")], TIMELOCK_INFO)
                self.assertIn(f"🆔 {label}: 42", msg)
                self.assertNotIn("Operation ID", msg)

    def test_missing_id_adds_no_empty_code_span(self, _mock_ai: object) -> None:
        msg = build_alert_message([_make_event(operationId=None)], TIMELOCK_INFO)
        self.assertNotIn("Operation ID", msg)
        self.assertNotIn("``", msg)

    def test_aave_governance_link_uses_proposal_id(self, _mock_ai: object) -> None:
        """Aave alerts link directly to the queued proposal, including proposal zero."""
        for proposal_id in ("525", "42", "0", 0):
            with self.subTest(proposal_id=proposal_id):
                event = _make_event("Aave", operationId=proposal_id)
                msg = build_alert_message([event], TIMELOCK_INFO)
                self.assertIn(f"🆔 Proposal: {proposal_id}\n", msg)
                self.assertIn(
                    "🔗 Governance: [Aave Governance]"
                    f"(https://app.aave.com/governance/v3/proposal/?proposalId={proposal_id})",
                    msg,
                )

    def test_aave_missing_id_omits_governance_link(self, _mock_ai: object) -> None:
        """An absent proposal ID must not produce a broken governance link."""
        for proposal_id in (None, ""):
            with self.subTest(proposal_id=proposal_id):
                msg = build_alert_message([_make_event("Aave", operationId=proposal_id)], TIMELOCK_INFO)
                self.assertNotIn("app.aave.com", msg)


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
    def test_unwraps_safe_wrapped_schedule_proposals(self, mock_cm: MagicMock) -> None:
        from protocols.timelock.timelock_alerts import _maple_proposal_calls

        targets = ["0x" + "aa" * 20, "0x" + "bb" * 20]
        datas = [bytes.fromhex("8456cb59"), bytes.fromhex("3f4ba83a")]  # pause(), unpause()
        inner_hex = self._make_schedule_calldata(targets, datas)
        outer = self._wrap_in_safe(inner_hex, "0x2efff88747eb5a3ff00d4d8d0f0800e306c0426b")

        mock_client = unittest.mock.MagicMock()
        mock_client.eth.get_transaction.return_value = {"input": outer}
        mock_cm.get_client.return_value = mock_client

        event = _make_event(timelock_type="Maple", transactionHash="0x" + "ff" * 32)
        calls = _maple_proposal_calls(event, chain_id=1)

        assert calls is not None
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["target"], targets[0])
        self.assertEqual(calls[0]["data"], "0x8456cb59")
        self.assertEqual(calls[1]["target"], targets[1])
        self.assertEqual(calls[1]["data"], "0x3f4ba83a")

    @patch("protocols.timelock.timelock_alerts.ChainManager")
    def test_unwraps_direct_schedule_proposals(self, mock_cm: MagicMock) -> None:
        from protocols.timelock.timelock_alerts import _maple_proposal_calls

        targets = ["0x" + "cc" * 20]
        datas = [bytes.fromhex("8456cb59")]
        inner_hex = self._make_schedule_calldata(targets, datas)

        mock_client = unittest.mock.MagicMock()
        mock_client.eth.get_transaction.return_value = {"input": inner_hex}
        mock_cm.get_client.return_value = mock_client

        event = _make_event(timelock_type="Maple", transactionHash="0x" + "ff" * 32)
        calls = _maple_proposal_calls(event, chain_id=1)
        assert calls is not None
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["data"], "0x8456cb59")

    @patch("protocols.timelock.timelock_alerts.ChainManager")
    def test_returns_none_for_unknown_selector(self, mock_cm: MagicMock) -> None:
        from protocols.timelock.timelock_alerts import _maple_proposal_calls

        # proposeRoleUpdates path — we can't synthesize (target, data) pairs from it.
        mock_client = unittest.mock.MagicMock()
        mock_client.eth.get_transaction.return_value = {"input": "0x2d6e853c" + "00" * 100}
        mock_cm.get_client.return_value = mock_client

        event = _make_event(timelock_type="Maple", transactionHash="0x" + "ff" * 32)
        self.assertIsNone(_maple_proposal_calls(event, chain_id=1))


class TestAiExplanationCallGates(unittest.TestCase):
    """Empty and short payloads must reach the explainer, including mixed batches."""

    @patch("protocols.timelock.timelock_alerts.explain_transaction")
    def test_empty_calldata_single_call_reaches_explainer(self, mock_explain: unittest.mock.MagicMock) -> None:
        from protocols.timelock.timelock_alerts import _get_ai_explanation

        mock_explain.return_value = None
        event = _make_event(data="0x", value="1000")
        _get_ai_explanation([event], TIMELOCK_INFO, 1)
        mock_explain.assert_called_once()
        self.assertEqual(mock_explain.call_args.kwargs["calldata"], "0x")
        self.assertEqual(mock_explain.call_args.kwargs["value"], 1000)

    @patch("protocols.timelock.timelock_alerts.explain_transaction")
    def test_null_value_does_not_crash_explainer(self, mock_explain: unittest.mock.MagicMock) -> None:
        from protocols.timelock.timelock_alerts import _get_ai_explanation

        mock_explain.return_value = None
        event = _make_event(data="0x8456cb59")
        event["value"] = None
        _get_ai_explanation([event], TIMELOCK_INFO, 1)
        mock_explain.assert_called_once()
        self.assertEqual(mock_explain.call_args.kwargs["value"], 0)

    @patch("protocols.timelock.timelock_alerts.explain_transaction")
    def test_short_calldata_single_call_reaches_explainer(self, mock_explain: unittest.mock.MagicMock) -> None:
        from protocols.timelock.timelock_alerts import _get_ai_explanation

        mock_explain.return_value = None
        event = _make_event(data="0x12")
        _get_ai_explanation([event], TIMELOCK_INFO, 1)
        mock_explain.assert_called_once()
        self.assertEqual(mock_explain.call_args.kwargs["calldata"], "0x12")

    @patch("protocols.timelock.timelock_alerts.explain_batch_transaction")
    def test_mixed_batch_keeps_empty_and_short_entries(self, mock_batch: unittest.mock.MagicMock) -> None:
        from protocols.timelock.timelock_alerts import _get_ai_explanation

        mock_batch.return_value = None
        events = [
            _make_event(data="0x8456cb59", target="0x" + "11" * 20),
            _make_event(data="0x", target="0x" + "22" * 20, value="1"),
            _make_event(data="0x12", target="0x" + "33" * 20),
        ]
        _get_ai_explanation(events, TIMELOCK_INFO, 1)
        mock_batch.assert_called_once()
        calls = mock_batch.call_args.kwargs["calls"]
        self.assertEqual([c["data"] for c in calls], ["0x8456cb59", "0x", "0x12"])
        self.assertEqual(calls[1]["value"], "1")

    @patch("protocols.timelock.timelock_alerts.explain_transaction")
    @patch("protocols.timelock.timelock_alerts.explain_batch_transaction")
    def test_events_without_target_are_skipped(
        self, mock_batch: unittest.mock.MagicMock, mock_single: unittest.mock.MagicMock
    ) -> None:
        from protocols.timelock.timelock_alerts import _get_ai_explanation

        result = _get_ai_explanation([_make_event(target="", data="0x8456cb59")], TIMELOCK_INFO, 1)
        self.assertIsNone(result)
        mock_batch.assert_not_called()
        mock_single.assert_not_called()


if __name__ == "__main__":
    unittest.main()

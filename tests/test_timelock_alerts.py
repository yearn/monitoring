"""Tests for timelock/timelock_alerts.py — build_alert_message truncation logic."""

import unittest
from unittest.mock import patch

from timelock.timelock_alerts import TimelockConfig, build_alert_message
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

    @patch("timelock.timelock_alerts._get_ai_explanation", return_value=None)
    def test_short_message_no_truncation(self, _mock_ai: object) -> None:
        """A simple message should not be truncated."""
        events = [_make_event()]
        msg = build_alert_message(events, TIMELOCK_INFO)
        self.assertLessEqual(len(msg), MAX_MESSAGE_LENGTH)
        self.assertIn("TIMELOCK: New Operation Scheduled", msg)
        self.assertIn("Test Timelock", msg)

    @patch("timelock.timelock_alerts._get_ai_explanation", return_value=None)
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
        self.assertIn("...", msg)

    @patch("timelock.timelock_alerts.format_explanation_line")
    @patch("timelock.timelock_alerts._get_ai_explanation")
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
        self.assertIn("...", msg)

    @patch("timelock.timelock_alerts.format_explanation_line")
    @patch("timelock.timelock_alerts._get_ai_explanation")
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


if __name__ == "__main__":
    unittest.main()

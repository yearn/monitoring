"""Tests for alert."""

import unittest
from unittest.mock import MagicMock, patch

from utils.alert import Alert, AlertSeverity, register_alert_hook, send_alert


class TestAlert(unittest.TestCase):
    """Tests for the Alert system."""

    def test_severity_enum_values(self):
        self.assertEqual(AlertSeverity.LOW.value, "LOW")
        self.assertEqual(AlertSeverity.MEDIUM.value, "MEDIUM")
        self.assertEqual(AlertSeverity.HIGH.value, "HIGH")
        self.assertEqual(AlertSeverity.CRITICAL.value, "CRITICAL")

    def test_alert_dataclass_immutability(self):
        alert = Alert(severity=AlertSeverity.HIGH, message="test", protocol="proto")
        with self.assertRaises(AttributeError):
            # Deliberately attempt mutation to verify the frozen dataclass.
            alert.message = "changed"  # ty: ignore[invalid-assignment]

    @patch("utils.alert.send_telegram_message")
    def test_emoji_prefix_low(self, mock_send):
        alert = Alert(severity=AlertSeverity.LOW, message="info msg", protocol="test")
        send_alert(alert)
        mock_send.assert_called_once_with(
            "ℹ️ info msg",
            "test",
            True,
            False,
            severity="LOW",
            source="protocol",
            origin_protocol="test",
            channel="test",
        )

    @patch("utils.alert.send_telegram_message")
    def test_emoji_prefix_medium(self, mock_send):
        alert = Alert(severity=AlertSeverity.MEDIUM, message="warn msg", protocol="test")
        send_alert(alert)
        mock_send.assert_called_once_with(
            "⚠️ warn msg",
            "test",
            False,
            False,
            severity="MEDIUM",
            source="protocol",
            origin_protocol="test",
            channel="test",
        )

    @patch("utils.alert.send_telegram_message")
    def test_emoji_prefix_high(self, mock_send):
        alert = Alert(severity=AlertSeverity.HIGH, message="high msg", protocol="test")
        send_alert(alert)
        mock_send.assert_called_once_with(
            "🚨 high msg",
            "test",
            False,
            False,
            severity="HIGH",
            source="protocol",
            origin_protocol="test",
            channel="test",
        )

    @patch("utils.alert.send_telegram_message")
    def test_emoji_prefix_critical(self, mock_send):
        alert = Alert(severity=AlertSeverity.CRITICAL, message="crit msg", protocol="test")
        send_alert(alert)
        mock_send.assert_called_once_with(
            "🔴 crit msg",
            "test",
            False,
            False,
            severity="CRITICAL",
            source="protocol",
            origin_protocol="test",
            channel="test",
        )

    @patch("utils.alert.send_telegram_message")
    def test_silent_default_low(self, mock_send):
        # LOW defaults to silent=True
        send_alert(Alert(severity=AlertSeverity.LOW, message="m", protocol="p"))
        _, args, _ = mock_send.mock_calls[0]
        self.assertTrue(args[2], "LOW should default to silent")

    @patch("utils.alert.send_telegram_message")
    def test_silent_default_medium_high_critical(self, mock_send):
        # MEDIUM, HIGH and CRITICAL default to silent=False (loud)
        for sev in (AlertSeverity.MEDIUM, AlertSeverity.HIGH, AlertSeverity.CRITICAL):
            mock_send.reset_mock()
            send_alert(Alert(severity=sev, message="m", protocol="p"))
            _, args, _ = mock_send.mock_calls[0]
            self.assertFalse(args[2], f"{sev.value} should default to loud")

    @patch("utils.alert.send_telegram_message")
    def test_silent_explicit_override(self, mock_send):
        # Override silent for a HIGH alert to True
        alert = Alert(severity=AlertSeverity.HIGH, message="m", protocol="p")
        send_alert(alert, silent=True)
        _, args, _ = mock_send.mock_calls[0]
        self.assertTrue(args[2])

        # Override silent for a LOW alert to False
        mock_send.reset_mock()
        alert = Alert(severity=AlertSeverity.LOW, message="m", protocol="p")
        send_alert(alert, silent=False)
        _, args, _ = mock_send.mock_calls[0]
        self.assertFalse(args[2])

    @patch("utils.alert.send_telegram_message")
    def test_plain_text_passthrough(self, mock_send):
        alert = Alert(severity=AlertSeverity.MEDIUM, message="m", protocol="p")
        send_alert(alert, plain_text=True)
        _, args, _ = mock_send.mock_calls[0]
        self.assertTrue(args[3])

    @patch("utils.alert.send_telegram_message")
    def test_channel_routes_telegram(self, mock_send):
        """When channel is set, Telegram message goes to channel, not protocol."""
        alert = Alert(severity=AlertSeverity.HIGH, message="peg alert", protocol="origin", channel="pegs")
        send_alert(alert)
        mock_send.assert_called_once_with(
            "🚨 peg alert",
            "pegs",
            False,
            False,
            severity="HIGH",
            source="protocol",
            origin_protocol="origin",
            channel="pegs",
        )

    @patch("utils.alert.send_telegram_message")
    def test_channel_fallback_to_protocol(self, mock_send):
        """When channel is empty, Telegram message goes to protocol."""
        alert = Alert(severity=AlertSeverity.HIGH, message="reserves low", protocol="infinifi")
        send_alert(alert)
        mock_send.assert_called_once_with(
            "🚨 reserves low",
            "infinifi",
            False,
            False,
            severity="HIGH",
            source="protocol",
            origin_protocol="infinifi",
            channel="infinifi",
        )

    @patch("utils.alert.send_telegram_message")
    def test_hook_invoked_for_high(self, mock_send):
        hook = MagicMock()
        register_alert_hook(hook)
        try:
            alert = Alert(severity=AlertSeverity.HIGH, message="m", protocol="p")
            send_alert(alert)
            hook.assert_called_once_with(alert)
        finally:
            register_alert_hook(None)

    @patch("utils.alert.send_telegram_message")
    def test_hook_invoked_for_critical(self, mock_send):
        hook = MagicMock()
        register_alert_hook(hook)
        try:
            alert = Alert(severity=AlertSeverity.CRITICAL, message="m", protocol="p")
            send_alert(alert)
            hook.assert_called_once_with(alert)
        finally:
            register_alert_hook(None)

    @patch("utils.alert.send_telegram_message")
    def test_hook_not_called_for_low_medium(self, mock_send):
        hook = MagicMock()
        register_alert_hook(hook)
        try:
            for sev in (AlertSeverity.LOW, AlertSeverity.MEDIUM):
                hook.reset_mock()
                send_alert(Alert(severity=sev, message="m", protocol="p"))
                hook.assert_not_called()
        finally:
            register_alert_hook(None)

    @patch("utils.alert.send_telegram_message")
    def test_hook_exception_swallowed(self, mock_send):
        hook = MagicMock(side_effect=RuntimeError("hook broke"))
        register_alert_hook(hook)
        try:
            alert = Alert(severity=AlertSeverity.HIGH, message="m", protocol="p")
            # Should NOT raise
            send_alert(alert)
            hook.assert_called_once_with(alert)
            # Telegram message should still have been sent
            mock_send.assert_called_once()
        finally:
            register_alert_hook(None)

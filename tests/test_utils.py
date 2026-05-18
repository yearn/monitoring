"""Tests for utility functions."""

import importlib
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

import requests

from utils.alert import Alert, AlertSeverity, register_alert_hook, send_alert
from utils.config import Config, ProtocolConfig
from utils.telegram import TelegramError, send_telegram_message


class TestConfig(unittest.TestCase):
    """Tests for the Config class."""

    def test_get_env(self):
        with patch.dict(os.environ, {"TEST_VAR": "test_value"}):
            self.assertEqual(Config.get_env("TEST_VAR"), "test_value")
            self.assertEqual(Config.get_env("NONEXISTENT_VAR", "default"), "default")

    def test_get_env_int(self):
        with patch.dict(os.environ, {"TEST_INT": "42", "TEST_INVALID": "not_an_int"}):
            self.assertEqual(Config.get_env_int("TEST_INT", 0), 42)
            self.assertEqual(Config.get_env_int("NONEXISTENT_VAR", 10), 10)
            self.assertEqual(Config.get_env_int("TEST_INVALID", 10), 10)

    def test_get_env_float(self):
        with patch.dict(os.environ, {"TEST_FLOAT": "3.14", "TEST_INVALID": "not_a_float"}):
            self.assertAlmostEqual(Config.get_env_float("TEST_FLOAT", 0.0), 3.14)
            self.assertAlmostEqual(Config.get_env_float("NONEXISTENT_VAR", 2.71), 2.71)
            self.assertAlmostEqual(Config.get_env_float("TEST_INVALID", 2.71), 2.71)

    def test_get_env_bool(self):
        with patch.dict(
            os.environ,
            {
                "TEST_TRUE1": "true",
                "TEST_TRUE2": "yes",
                "TEST_TRUE3": "1",
                "TEST_FALSE": "false",
            },
        ):
            self.assertTrue(Config.get_env_bool("TEST_TRUE1", False))
            self.assertTrue(Config.get_env_bool("TEST_TRUE2", False))
            self.assertTrue(Config.get_env_bool("TEST_TRUE3", False))
            self.assertFalse(Config.get_env_bool("TEST_FALSE", True))
            self.assertTrue(Config.get_env_bool("NONEXISTENT_VAR", True))

    def test_get_protocol_config(self):
        with patch.dict(
            os.environ,
            {
                "AAVE_ALERT_THRESHOLD": "0.96",
                "AAVE_CRITICAL_THRESHOLD": "0.99",
                "AAVE_ENABLE_NOTIFICATIONS": "false",
            },
        ):
            config = Config.get_protocol_config("aave")
            self.assertIsInstance(config, ProtocolConfig)
            self.assertEqual(config.name, "aave")
            self.assertAlmostEqual(config.alert_threshold, 0.96)
            self.assertAlmostEqual(config.critical_threshold, 0.99)
            self.assertFalse(config.enable_notifications)


class TestTelegram(unittest.TestCase):
    """Tests for Telegram utility functions."""

    @patch("utils.telegram.requests.post")
    def test_send_telegram_message_success(self, mock_post):
        # Setup mock response
        mock_response = unittest.mock.Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = unittest.mock.Mock()
        mock_post.return_value = mock_response

        # Test with environment variables
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN_TEST": "test_token",
                "TELEGRAM_CHAT_ID_TEST": "test_chat_id",
                "LOG_LEVEL": "INFO",
            },
        ):
            # Should not raise any exceptions
            send_telegram_message("Test message", "test")

            # Verify the request was made with the correct parameters
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            self.assertEqual(kwargs["json"]["text"], "Test message")
            self.assertEqual(kwargs["json"]["parse_mode"], "Markdown")

    @patch("utils.telegram.requests.get")
    def test_send_telegram_message_missing_credentials(self, mock_get):
        # Test with missing environment variables
        with patch.dict(os.environ, {}, clear=True):
            # Should not raise exceptions but log a warning
            with patch("utils.telegram.logger") as mock_logger:
                send_telegram_message("Test message", "test")
                mock_logger.warning.assert_any_call("Missing Telegram credentials for %s", "test")

            # Verify no request was made
            mock_get.assert_not_called()

    @patch("utils.telegram.requests.post")
    def test_send_telegram_message_failure(self, mock_post):
        # Setup mock response for failure
        mock_post.side_effect = requests.RequestException("Connection error")

        # Test with environment variables
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN_TEST": "test_token",
                "TELEGRAM_CHAT_ID_TEST": "test_chat_id",
                "LOG_LEVEL": "INFO",
            },
        ):
            # Should raise TelegramError
            with self.assertRaises(TelegramError):
                send_telegram_message("Test message", "test")

    @patch("utils.telegram.requests.post")
    def test_send_telegram_message_with_topic(self, mock_post):
        """When TELEGRAM_TOPIC_ID is set, message goes to topics chat with message_thread_id."""
        mock_response = unittest.mock.Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = unittest.mock.Mock()
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN_DEFAULT": "default_token",
                "TELEGRAM_CHAT_ID_TOPICS": "topics_chat_id",
                "TELEGRAM_TOPIC_ID_AAVE": "42",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_telegram_message("Test message", "aave")

            mock_post.assert_called_once()
            url = mock_post.call_args[0][0]
            kwargs = mock_post.call_args[1]
            self.assertEqual(kwargs["json"]["chat_id"], "topics_chat_id")
            self.assertEqual(kwargs["json"]["message_thread_id"], 42)
            self.assertIn("default_token", url)

    @patch("utils.telegram.requests.post")
    def test_send_telegram_message_topic_uses_default_bot(self, mock_post):
        """Topic routing always uses the default bot, even if protocol-specific bot exists."""
        mock_response = unittest.mock.Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = unittest.mock.Mock()
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN_DEFAULT": "default_token",
                "TELEGRAM_BOT_TOKEN_AAVE": "aave_specific_token",
                "TELEGRAM_CHAT_ID_TOPICS": "topics_chat_id",
                "TELEGRAM_TOPIC_ID_AAVE": "7",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_telegram_message("Test", "aave")
            url = mock_post.call_args[0][0]
            self.assertIn("default_token", url)
            self.assertNotIn("aave_specific_token", url)

    @patch("utils.telegram.requests.post")
    def test_send_telegram_message_no_topic_falls_back(self, mock_post):
        """Without topic ID, uses legacy per-protocol chat routing."""
        mock_response = unittest.mock.Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = unittest.mock.Mock()
        mock_post.return_value = mock_response

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN_AAVE": "aave_token",
                "TELEGRAM_CHAT_ID_AAVE": "aave_chat_id",
                "TELEGRAM_CHAT_ID_TOPICS": "topics_chat_id",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_telegram_message("Test", "aave")
            kwargs = mock_post.call_args[1]
            self.assertEqual(kwargs["json"]["chat_id"], "aave_chat_id")
            self.assertNotIn("message_thread_id", kwargs["json"])


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
            alert.message = "changed"

    @patch("utils.alert.send_telegram_message")
    def test_emoji_prefix_low(self, mock_send):
        alert = Alert(severity=AlertSeverity.LOW, message="info msg", protocol="test")
        send_alert(alert)
        mock_send.assert_called_once_with("ℹ️ info msg", "test", True, False)

    @patch("utils.alert.send_telegram_message")
    def test_emoji_prefix_medium(self, mock_send):
        alert = Alert(severity=AlertSeverity.MEDIUM, message="warn msg", protocol="test")
        send_alert(alert)
        mock_send.assert_called_once_with("⚠️ warn msg", "test", False, False)

    @patch("utils.alert.send_telegram_message")
    def test_emoji_prefix_high(self, mock_send):
        alert = Alert(severity=AlertSeverity.HIGH, message="high msg", protocol="test")
        send_alert(alert)
        mock_send.assert_called_once_with("🚨 high msg", "test", False, False)

    @patch("utils.alert.send_telegram_message")
    def test_emoji_prefix_critical(self, mock_send):
        alert = Alert(severity=AlertSeverity.CRITICAL, message="crit msg", protocol="test")
        send_alert(alert)
        mock_send.assert_called_once_with("🔴 crit msg", "test", False, False)

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
        mock_send.assert_called_once_with("🚨 peg alert", "pegs", False, False)

    @patch("utils.alert.send_telegram_message")
    def test_channel_fallback_to_protocol(self, mock_send):
        """When channel is empty, Telegram message goes to protocol."""
        alert = Alert(severity=AlertSeverity.HIGH, message="reserves low", protocol="infinifi")
        send_alert(alert)
        mock_send.assert_called_once_with("🚨 reserves low", "infinifi", False, False)

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


class TestDispatch(unittest.TestCase):
    """Tests for the emergency dispatch utility."""

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._record_dispatch")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_sends_correct_payload(self, mock_cooldown, mock_record, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        alert = Alert(severity=AlertSeverity.HIGH, message="Reserves low", protocol="infinifi")

        with patch.dict(os.environ, {"PAT_DISPATCH": "ghp_test_token", "LOG_LEVEL": "INFO"}):
            dispatch_emergency_withdrawal(alert)

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        payload = call_kwargs["json"]
        self.assertEqual(payload["event_type"], "emergency_withdrawal")
        self.assertEqual(payload["client_payload"]["protocol"], "infinifi")
        self.assertEqual(payload["client_payload"]["severity"], "HIGH")
        self.assertEqual(payload["client_payload"]["message"], "Reserves low")
        # Payload should only contain protocol, severity, and message (no markets/vault/chain)
        self.assertEqual(set(payload["client_payload"].keys()), {"protocol", "severity", "message"})

        # Verify auth header
        headers = call_kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer ghp_test_token")

        mock_record.assert_called_once_with("infinifi")

    @patch("utils.dispatch.requests.post")
    def test_dispatch_skips_low_severity(self, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        alert = Alert(severity=AlertSeverity.LOW, message="info", protocol="infinifi")
        dispatch_emergency_withdrawal(alert)
        mock_post.assert_not_called()

    @patch("utils.dispatch.requests.post")
    def test_dispatch_skips_medium_severity(self, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        alert = Alert(severity=AlertSeverity.MEDIUM, message="warn", protocol="infinifi")
        dispatch_emergency_withdrawal(alert)
        mock_post.assert_not_called()

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_skips_unknown_protocol(self, mock_cooldown, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        alert = Alert(severity=AlertSeverity.HIGH, message="alert", protocol="unknown_protocol")

        with patch.dict(os.environ, {"PAT_DISPATCH": "ghp_test_token"}):
            dispatch_emergency_withdrawal(alert)

        mock_post.assert_not_called()

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._is_on_cooldown", return_value=True)
    def test_dispatch_skips_on_cooldown(self, mock_cooldown, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        alert = Alert(severity=AlertSeverity.HIGH, message="alert", protocol="infinifi")

        with patch.dict(os.environ, {"PAT_DISPATCH": "ghp_test_token"}):
            dispatch_emergency_withdrawal(alert)

        mock_post.assert_not_called()

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_skips_missing_pat(self, mock_cooldown, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        alert = Alert(severity=AlertSeverity.HIGH, message="alert", protocol="infinifi")

        with patch.dict(os.environ, {}, clear=True):
            dispatch_emergency_withdrawal(alert)

        mock_post.assert_not_called()

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._record_dispatch")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_critical_sends_critical_severity(self, mock_cooldown, mock_record, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        alert = Alert(severity=AlertSeverity.CRITICAL, message="total failure", protocol="infinifi")

        with patch.dict(os.environ, {"PAT_DISPATCH": "ghp_test_token", "LOG_LEVEL": "INFO"}):
            dispatch_emergency_withdrawal(alert)

        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["client_payload"]["severity"], "CRITICAL")

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._record_dispatch")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_handles_request_exception(self, mock_cooldown, mock_record, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        mock_post.side_effect = requests.RequestException("Connection error")

        alert = Alert(severity=AlertSeverity.HIGH, message="alert", protocol="infinifi")

        with patch.dict(os.environ, {"PAT_DISPATCH": "ghp_test_token", "LOG_LEVEL": "INFO"}):
            # Should not raise
            dispatch_emergency_withdrawal(alert)

        mock_record.assert_not_called()

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._record_dispatch")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_uses_protocol_not_channel(self, mock_cooldown, mock_record, mock_post):
        """Dispatch uses alert.protocol (not channel) for payload and cooldown."""
        from utils.dispatch import dispatch_emergency_withdrawal

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        alert = Alert(severity=AlertSeverity.HIGH, message="redeem value dropped", protocol="origin", channel="pegs")

        with patch.dict(os.environ, {"PAT_DISPATCH": "ghp_test_token", "LOG_LEVEL": "INFO"}):
            dispatch_emergency_withdrawal(alert)

        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["client_payload"]["protocol"], "origin")
        mock_record.assert_called_once_with("origin")

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_skips_non_dispatchable_channel_protocol(self, mock_cooldown, mock_post):
        """Protocol not in DISPATCHABLE_PROTOCOLS is skipped even with a valid channel."""
        from utils.dispatch import dispatch_emergency_withdrawal

        alert = Alert(severity=AlertSeverity.HIGH, message="peg alert", protocol="puffer", channel="pegs")

        with patch.dict(os.environ, {"PAT_DISPATCH": "ghp_test_token"}):
            dispatch_emergency_withdrawal(alert)

        mock_post.assert_not_called()

    @patch("utils.dispatch.requests.post")
    def test_dispatch_skips_in_debug_mode(self, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        alert = Alert(severity=AlertSeverity.HIGH, message="alert", protocol="infinifi")

        with patch.dict(os.environ, {"PAT_DISPATCH": "ghp_test_token", "LOG_LEVEL": "DEBUG"}):
            dispatch_emergency_withdrawal(alert)

        mock_post.assert_not_called()

    def test_cooldown_logic(self):
        import time

        from utils.dispatch import _is_on_cooldown

        with patch("utils.dispatch.get_last_value_for_key_from_file") as mock_get:
            # No previous dispatch
            mock_get.return_value = 0
            self.assertFalse(_is_on_cooldown("infinifi"))

            # Recent dispatch (within cooldown)
            mock_get.return_value = str(time.time() - 10)
            self.assertTrue(_is_on_cooldown("infinifi", cooldown_seconds=60))

            # Old dispatch (past cooldown)
            mock_get.return_value = str(time.time() - 7200)
            self.assertFalse(_is_on_cooldown("infinifi", cooldown_seconds=3600))


class TestDefiLlama(unittest.TestCase):
    """Tests for the DeFiLlama stablecoin price helper."""

    def test_fetch_prices_raises_on_api_error(self):
        fake_client = MagicMock()
        fake_client.prices.getCurrentPrices.side_effect = RuntimeError("upstream timeout")
        fake_sdk = types.ModuleType("defillama_sdk")
        fake_sdk.DefiLlama = MagicMock(return_value=fake_client)

        with patch.dict(sys.modules, {"defillama_sdk": fake_sdk}):
            sys.modules.pop("utils.defillama", None)
            defillama = importlib.import_module("utils.defillama")
            try:
                with self.assertRaises(RuntimeError):
                    defillama.fetch_prices(["ethereum:0xtoken"])
            finally:
                sys.modules.pop("utils.defillama", None)


if __name__ == "__main__":
    unittest.main()

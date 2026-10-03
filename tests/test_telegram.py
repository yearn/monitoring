"""Tests for telegram."""

import os
import unittest
from unittest import mock
from unittest.mock import patch

import requests

from utils.telegram import TelegramError, send_envio_error_message, send_error_message, send_telegram_message


class TestTelegram(unittest.TestCase):
    """Tests for Telegram utility functions."""

    @patch("utils.telegram.requests.post")
    def test_send_telegram_message_success(self, mock_post):
        # Setup mock response
        mock_post.return_value = mock.Mock(status_code=200, raise_for_status=mock.Mock(return_value=None))

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

    @patch("utils.telegram.requests.post")
    def test_send_telegram_message_plain_text_omits_parse_mode(self, mock_post):
        mock_post.return_value = mock.Mock(status_code=200, raise_for_status=mock.Mock(return_value=None))

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN_TEST": "test_token",
                "TELEGRAM_CHAT_ID_TEST": "test_chat_id",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_telegram_message("Test message", "test", plain_text=True)

            kwargs = mock_post.call_args[1]
            self.assertEqual(kwargs["json"]["text"], "Test message")
            self.assertNotIn("parse_mode", kwargs["json"])

    @patch("utils.telegram.requests.post")
    def test_send_telegram_message_missing_credentials(self, mock_post):
        # Test with missing environment variables
        with patch.dict(os.environ, {}, clear=True):
            # Should not raise exceptions but log a warning
            with patch("utils.telegram.logger") as mock_logger:
                send_telegram_message("Test message", "test")
                mock_logger.warning.assert_any_call("Missing Telegram credentials for %s", "test")

            # Verify no request was made
            mock_post.assert_not_called()

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
        mock_post.return_value = mock.Mock(status_code=200, raise_for_status=mock.Mock(return_value=None))

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
        mock_post.return_value = mock.Mock(status_code=200, raise_for_status=mock.Mock(return_value=None))

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
        mock_post.return_value = mock.Mock(status_code=200, raise_for_status=mock.Mock(return_value=None))

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN_AAVE": "aave_token",
                "TELEGRAM_CHAT_ID_AAVE": "aave_chat_id",
                "TELEGRAM_CHAT_ID_TOPICS": "topics_chat_id",
                "TELEGRAM_TOPIC_ID_AAVE": "",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_telegram_message("Test", "aave")
            kwargs = mock_post.call_args[1]
            self.assertEqual(kwargs["json"]["chat_id"], "aave_chat_id")
            self.assertNotIn("message_thread_id", kwargs["json"])

    @patch("utils.telegram.requests.post")
    def test_send_telegram_message_test_override(self, mock_post):
        """TELEGRAM_TEST_CHAT_ID forces every message to one chat via the default bot.

        It overrides both topic and legacy routing, prepends a [protocol] label
        (Markdown-escaped so protocol names with `_` and the brackets don't trip a
        400 parse error), and never applies topic threading.
        """
        mock_post.return_value = mock.Mock(status_code=200, raise_for_status=mock.Mock(return_value=None))

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TEST_CHAT_ID": "dummy_group",
                "TELEGRAM_BOT_TOKEN_DEFAULT": "default_token",
                # Production routing that must be ignored while the override is set:
                "TELEGRAM_BOT_TOKEN_AAVE": "aave_token",
                "TELEGRAM_CHAT_ID_TOPICS": "topics_chat_id",
                "TELEGRAM_TOPIC_ID_AAVE": "42",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_telegram_message("Test message", "aave")

            url = mock_post.call_args[0][0]
            kwargs = mock_post.call_args[1]
            self.assertIn("default_token", url)
            self.assertNotIn("aave_token", url)
            self.assertEqual(kwargs["json"]["chat_id"], "dummy_group")
            self.assertEqual(kwargs["json"]["text"], "\\[aave] Test message")
            self.assertNotIn("message_thread_id", kwargs["json"])

        # A protocol name with an underscore is the case the escaping protects:
        # unescaped, `[yearn_timelock]` is parsed as Markdown and 400s.
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TEST_CHAT_ID": "dummy_group",
                "TELEGRAM_BOT_TOKEN_DEFAULT": "default_token",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_telegram_message("Test message", "yearn_timelock")
            self.assertEqual(
                mock_post.call_args[1]["json"]["text"],
                "\\[yearn\\_timelock] Test message",
            )

        # plain_text sends keep the label literal (no parse_mode → nothing to escape).
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TEST_CHAT_ID": "dummy_group",
                "TELEGRAM_BOT_TOKEN_DEFAULT": "default_token",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_telegram_message("Test message", "yearn_timelock", plain_text=True)
            self.assertEqual(
                mock_post.call_args[1]["json"]["text"],
                "[yearn_timelock] Test message",
            )


class TestSendErrorMessage(unittest.TestCase):
    """Tests for utils.telegram.send_error_message (dedicated errors channel)."""

    @patch("utils.telegram.requests.post")
    def test_routes_to_errors_chat_with_label_silent_plain(self, mock_post):
        """With an errors chat configured, the message goes there labelled, silent, plain."""
        mock_post.return_value = mock.Mock(status_code=200, raise_for_status=mock.Mock(return_value=None))

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TEST_CHAT_ID": "",
                "TELEGRAM_TOPIC_ID_ERRORS": "",
                "TELEGRAM_CHAT_ID_ERRORS": "errors_chat_id",
                "TELEGRAM_BOT_TOKEN_DEFAULT": "default_token",
                # Aave's own routing must be ignored — the error goes to the errors chat.
                "TELEGRAM_BOT_TOKEN_AAVE": "aave_token",
                "TELEGRAM_CHAT_ID_AAVE": "aave_chat_id",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_error_message("GraphQL boom", "aave")

        url = mock_post.call_args[0][0]
        json_body = mock_post.call_args[1]["json"]
        self.assertIn("default_token", url)
        self.assertEqual(json_body["chat_id"], "errors_chat_id")
        self.assertEqual(json_body["text"], "[aave] GraphQL boom")
        self.assertTrue(json_body["disable_notification"])
        self.assertNotIn("parse_mode", json_body)  # plain text

    @patch("utils.telegram.requests.post")
    def test_errors_topic_takes_precedence(self, mock_post):
        """TELEGRAM_TOPIC_ID_ERRORS routes to the topics group on the errors thread."""
        mock_post.return_value = mock.Mock(status_code=200, raise_for_status=mock.Mock(return_value=None))

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TEST_CHAT_ID": "",
                "TELEGRAM_TOPIC_ID_ERRORS": "99",
                "TELEGRAM_CHAT_ID_ERRORS": "errors_chat_id",
                "TELEGRAM_CHAT_ID_TOPICS": "topics_chat_id",
                "TELEGRAM_BOT_TOKEN_DEFAULT": "default_token",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_error_message("boom", "morpho")

        json_body = mock_post.call_args[1]["json"]
        self.assertEqual(json_body["chat_id"], "topics_chat_id")
        self.assertEqual(json_body["message_thread_id"], 99)
        self.assertEqual(json_body["text"], "[morpho] boom")

    @patch("utils.telegram.requests.post")
    def test_falls_back_to_protocol_channel_when_unconfigured(self, mock_post):
        """With no errors destination set, the error routes to the protocol's own chat (no label)."""
        mock_post.return_value = mock.Mock(status_code=200, raise_for_status=mock.Mock(return_value=None))

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TEST_CHAT_ID": "",
                "TELEGRAM_TOPIC_ID_ERRORS": "",
                "TELEGRAM_CHAT_ID_ERRORS": "",
                "TELEGRAM_TOPIC_ID_AAVE": "",
                "TELEGRAM_BOT_TOKEN_AAVE": "aave_token",
                "TELEGRAM_CHAT_ID_AAVE": "aave_chat_id",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_error_message("GraphQL boom", "aave")

        url = mock_post.call_args[0][0]
        json_body = mock_post.call_args[1]["json"]
        self.assertIn("aave_token", url)
        self.assertEqual(json_body["chat_id"], "aave_chat_id")
        self.assertEqual(json_body["text"], "GraphQL boom")  # no [label] prefix on fallback
        self.assertTrue(json_body["disable_notification"])
        self.assertNotIn("parse_mode", json_body)  # plain text


class TestSendEnvioErrorMessage(unittest.TestCase):
    """Tests for utils.telegram.send_envio_error_message (dedicated envio channel)."""

    @staticmethod
    def _ok_response(mock_post):
        mock_post.return_value = mock.Mock(status_code=200, raise_for_status=mock.Mock(return_value=None))

    @patch("utils.telegram.requests.post")
    def test_routes_to_envio_chat_with_label_silent_plain(self, mock_post):
        """With an envio chat configured, the message goes there labelled, silent, plain."""
        self._ok_response(mock_post)

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TEST_CHAT_ID": "",
                "TELEGRAM_CHAT_ID_ENVIO": "envio_chat_id",
                # The errors channel must not win — envio problems have their own chat.
                "TELEGRAM_CHAT_ID_ERRORS": "errors_chat_id",
                "TELEGRAM_BOT_TOKEN_DEFAULT": "default_token",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_envio_error_message("Indexer stale on Mainnet", "yearn")

        url = mock_post.call_args[0][0]
        json_body = mock_post.call_args[1]["json"]
        self.assertIn("default_token", url)
        self.assertEqual(json_body["chat_id"], "envio_chat_id")
        self.assertNotIn("message_thread_id", json_body)  # standalone chat, no topic
        self.assertEqual(json_body["text"], "[yearn] Indexer stale on Mainnet")
        self.assertTrue(json_body["disable_notification"])
        self.assertNotIn("parse_mode", json_body)  # plain text

    def test_alert_protocol_changes_stored_key_only(self):
        """alert_protocol overrides the stored key; label and chat stay the same."""
        for env in (
            {"TELEGRAM_CHAT_ID_ENVIO": "envio_chat_id", "TELEGRAM_CHAT_ID_ERRORS": ""},
            {"TELEGRAM_CHAT_ID_ENVIO": "", "TELEGRAM_CHAT_ID_ERRORS": "errors_chat_id"},
            {"TELEGRAM_CHAT_ID_ENVIO": "", "TELEGRAM_CHAT_ID_ERRORS": ""},
        ):
            with (
                self.subTest(env=env),
                patch.dict(os.environ, env),
                patch("utils.telegram.send_telegram_message") as mock_send,
            ):
                send_envio_error_message("GraphQL boom", "yearn", alert_protocol="yearn-internal")

                kwargs = mock_send.call_args.kwargs
                self.assertEqual(kwargs["origin_protocol"], "yearn-internal")
                if env["TELEGRAM_CHAT_ID_ENVIO"] or env["TELEGRAM_CHAT_ID_ERRORS"]:
                    self.assertEqual(mock_send.call_args.args[0], "[yearn] GraphQL boom")
                else:
                    self.assertEqual(kwargs["channel"], "yearn")

    @patch("utils.telegram.requests.post")
    def test_labels_originating_protocol(self, mock_post):
        """Every monitor's envio problems land in one chat, labelled by origin."""
        self._ok_response(mock_post)

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TEST_CHAT_ID": "",
                "TELEGRAM_CHAT_ID_ENVIO": "envio_chat_id",
                "TELEGRAM_BOT_TOKEN_DEFAULT": "default_token",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_envio_error_message("GraphQL boom", "timelock")

        json_body = mock_post.call_args[1]["json"]
        self.assertEqual(json_body["chat_id"], "envio_chat_id")
        self.assertEqual(json_body["text"], "[timelock] GraphQL boom")

    @patch("utils.telegram.requests.post")
    def test_falls_back_to_errors_channel_when_unconfigured(self, mock_post):
        """With TELEGRAM_CHAT_ID_ENVIO unset, the alert routes to the errors channel."""
        self._ok_response(mock_post)

        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TEST_CHAT_ID": "",
                "TELEGRAM_CHAT_ID_ENVIO": "",
                "TELEGRAM_TOPIC_ID_ERRORS": "",
                "TELEGRAM_CHAT_ID_ERRORS": "errors_chat_id",
                "TELEGRAM_BOT_TOKEN_DEFAULT": "default_token",
                "LOG_LEVEL": "INFO",
            },
        ):
            send_envio_error_message("GraphQL boom", "yearn")

        json_body = mock_post.call_args[1]["json"]
        self.assertEqual(json_body["chat_id"], "errors_chat_id")
        self.assertEqual(json_body["text"], "[yearn] GraphQL boom")

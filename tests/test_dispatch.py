"""Tests for dispatch."""

import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import requests

from utils.alert import Alert, AlertSeverity


class TestDispatch(unittest.TestCase):
    """Tests for the emergency dispatch utility."""

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._record_dispatch")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_sends_for_3jane(self, mock_cooldown, mock_record, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response
        alert = Alert(severity=AlertSeverity.HIGH, message="Junior buffer low", protocol="3jane")

        with patch.dict(os.environ, {"LIQUIDITY_WEBHOOK_SECRET": "test_secret", "LOG_LEVEL": "INFO"}):
            dispatch_emergency_withdrawal(alert)

        payload = json.loads(mock_post.call_args[1]["data"].decode("utf-8"))
        self.assertEqual(payload["client_payload"]["protocol"], "3jane")
        self.assertEqual(payload["client_payload"]["severity"], "HIGH")
        mock_record.assert_called_once_with("3jane")

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._record_dispatch")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_sends_correct_payload(self, mock_cooldown, mock_record, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        alert = Alert(severity=AlertSeverity.HIGH, message="Reserves low", protocol="infinifi")

        with patch.dict(os.environ, {"LIQUIDITY_WEBHOOK_SECRET": "test_secret", "LOG_LEVEL": "INFO"}):
            dispatch_emergency_withdrawal(alert)

        mock_post.assert_called_once()
        call_args = mock_post.call_args[0]
        call_kwargs = mock_post.call_args[1]
        self.assertEqual(call_args[0], "http://127.0.0.1:8080/webhook/emergency")
        body = call_kwargs["data"]
        self.assertIsInstance(body, bytes)
        self.assertNotIn("json", call_kwargs)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["event_type"], "emergency_withdrawal")
        self.assertEqual(payload["client_payload"]["protocol"], "infinifi")
        self.assertEqual(payload["client_payload"]["severity"], "HIGH")
        self.assertEqual(payload["client_payload"]["message"], "Reserves low")
        # Payload should only contain protocol, severity, and message (no markets/vault/chain)
        self.assertEqual(set(payload["client_payload"].keys()), {"protocol", "severity", "message"})

        headers = call_kwargs["headers"]
        expected_hmac = hmac.new(b"test_secret", body, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Hub-Signature-256"], f"sha256={expected_hmac}")
        self.assertEqual(headers["Content-Type"], "application/json")

        mock_record.assert_called_once_with("infinifi")

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._record_dispatch")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_uses_configured_webhook_url(self, mock_cooldown, mock_record, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        alert = Alert(severity=AlertSeverity.HIGH, message="Reserves low", protocol="infinifi")

        with patch.dict(
            os.environ,
            {
                "LIQUIDITY_WEBHOOK_SECRET": "test_secret",
                "LIQUIDITY_WEBHOOK_URL": "http://localhost:9000/webhook/emergency",
                "LOG_LEVEL": "INFO",
            },
        ):
            dispatch_emergency_withdrawal(alert)

        self.assertEqual(mock_post.call_args[0][0], "http://localhost:9000/webhook/emergency")

    @patch("utils.dispatch.requests.post")
    def test_dispatch_skips_low_and_medium_severity(self, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        for severity in (AlertSeverity.LOW, AlertSeverity.MEDIUM):
            dispatch_emergency_withdrawal(Alert(severity=severity, message="m", protocol="infinifi"))
        mock_post.assert_not_called()

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_skips_unknown_protocol(self, mock_cooldown, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        alert = Alert(severity=AlertSeverity.HIGH, message="alert", protocol="unknown_protocol")

        with patch.dict(os.environ, {"LIQUIDITY_WEBHOOK_SECRET": "test_secret"}):
            dispatch_emergency_withdrawal(alert)

        mock_post.assert_not_called()

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._is_on_cooldown", return_value=True)
    def test_dispatch_skips_on_cooldown(self, mock_cooldown, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        alert = Alert(severity=AlertSeverity.HIGH, message="alert", protocol="infinifi")

        with patch.dict(os.environ, {"LIQUIDITY_WEBHOOK_SECRET": "test_secret"}):
            dispatch_emergency_withdrawal(alert)

        mock_post.assert_not_called()

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_skips_missing_webhook_secret(self, mock_cooldown, mock_post):
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

        with patch.dict(os.environ, {"LIQUIDITY_WEBHOOK_SECRET": "test_secret", "LOG_LEVEL": "INFO"}):
            dispatch_emergency_withdrawal(alert)

        payload = json.loads(mock_post.call_args[1]["data"].decode("utf-8"))
        self.assertEqual(payload["client_payload"]["severity"], "CRITICAL")

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._record_dispatch")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_handles_request_exception(self, mock_cooldown, mock_record, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        mock_post.side_effect = requests.RequestException("Connection error")

        alert = Alert(severity=AlertSeverity.HIGH, message="alert", protocol="infinifi")

        with patch.dict(os.environ, {"LIQUIDITY_WEBHOOK_SECRET": "test_secret", "LOG_LEVEL": "INFO"}):
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

        with patch.dict(os.environ, {"LIQUIDITY_WEBHOOK_SECRET": "test_secret", "LOG_LEVEL": "INFO"}):
            dispatch_emergency_withdrawal(alert)

        payload = json.loads(mock_post.call_args[1]["data"].decode("utf-8"))
        self.assertEqual(payload["client_payload"]["protocol"], "origin")
        mock_record.assert_called_once_with("origin")

    @patch("utils.dispatch.requests.post")
    @patch("utils.dispatch._is_on_cooldown", return_value=False)
    def test_dispatch_skips_non_dispatchable_channel_protocol(self, mock_cooldown, mock_post):
        """Protocol not in DISPATCHABLE_PROTOCOLS is skipped even with a valid channel."""
        from utils.dispatch import dispatch_emergency_withdrawal

        alert = Alert(severity=AlertSeverity.HIGH, message="peg alert", protocol="puffer", channel="pegs")

        with patch.dict(os.environ, {"LIQUIDITY_WEBHOOK_SECRET": "test_secret"}):
            dispatch_emergency_withdrawal(alert)

        mock_post.assert_not_called()

    @patch("utils.dispatch.requests.post")
    def test_dispatch_skips_in_debug_mode(self, mock_post):
        from utils.dispatch import dispatch_emergency_withdrawal

        alert = Alert(severity=AlertSeverity.HIGH, message="alert", protocol="infinifi")

        with patch.dict(os.environ, {"LIQUIDITY_WEBHOOK_SECRET": "test_secret", "LOG_LEVEL": "DEBUG"}):
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

from __future__ import annotations

from unittest.mock import patch

import pytest

from utils import paths, store
from utils.alert import Alert, AlertSeverity, send_alert
from utils.telegram import TelegramError, send_error_message, send_telegram_message


def _use_cache_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(paths, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(store, "_initialized", False)
    monkeypatch.setattr(store, "_initialized_path", None)


def test_raw_send_records_protocol(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_DEFAULT", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_AAVE", "chat")
    with patch("utils.telegram._post_message") as post:
        send_telegram_message("hello", "aave")
    post.assert_called_once()
    row = store.query_alerts()[0]
    assert row["source"] == "protocol"
    assert row["protocol"] == "aave"
    assert row["severity"] is None
    assert row["delivery_status"] == "delivered"


def test_send_alert_records_severity_and_origin(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_DEFAULT", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_ROUTED", "chat")
    with patch("utils.telegram._post_message"):
        send_alert(Alert(AlertSeverity.HIGH, "boom", "aave", channel="routed"))
    row = store.query_alerts()[0]
    assert row["protocol"] == "aave"
    assert row["channel"] == "routed"
    assert row["severity"] == "HIGH"


def test_error_message_records_ops_source(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_DEFAULT", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_ERRORS", "chat")
    with patch("utils.telegram._post_message"):
        send_error_message("boom", "aave")
    row = store.query_alerts()[0]
    assert row["source"] == "ops_error"
    assert row["protocol"] == "aave"
    assert row["channel"] == "errors"


def test_capture_failures_are_swallowed(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_DEFAULT", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_AAVE", "chat")
    with patch("utils.telegram.store.record_alert", side_effect=RuntimeError("db down")):
        with patch("utils.telegram._post_message") as post:
            send_telegram_message("hello", "aave")
    post.assert_called_once()


def test_delivery_update_failure_is_swallowed(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_DEFAULT", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_AAVE", "chat")
    with patch("utils.telegram.store.update_alert_delivery", side_effect=RuntimeError("db down")):
        with patch("utils.telegram._post_message"):
            send_telegram_message("hello", "aave")


def test_telegram_failure_marks_failed_and_reraises(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_DEFAULT", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_AAVE", "chat")
    with patch("utils.telegram._post_message", side_effect=TelegramError("bad")):
        with pytest.raises(TelegramError):
            send_telegram_message("hello", "aave")
    row = store.query_alerts()[0]
    assert row["delivery_status"] == "failed"
    assert row["delivery_error"] == "bad"


def test_missing_credentials_and_debug_are_recorded(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    send_telegram_message("missing", "aave")
    assert store.query_alerts()[0]["delivery_status"] == "skipped_missing_credentials"

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    send_telegram_message("debug", "aave")
    assert store.query_alerts()[0]["delivery_status"] == "skipped_debug"

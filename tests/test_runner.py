"""Tests for utils.runner.run_with_alert."""

import unittest
from unittest.mock import patch

from utils.runner import run_with_alert


class TestRunWithAlert(unittest.TestCase):
    def test_success_runs_entrypoint_no_alert(self) -> None:
        called = {"n": 0}

        def ok() -> None:
            called["n"] += 1

        with patch("utils.runner.send_telegram_message") as mock_send:
            run_with_alert(ok, "yearn")
        self.assertEqual(called["n"], 1)
        mock_send.assert_not_called()

    def test_exception_sends_alert_and_returns(self) -> None:
        def boom() -> None:
            raise RuntimeError("kaboom")

        with patch("utils.runner.send_telegram_message") as mock_send:
            # Must NOT raise — the wrapper swallows and alerts
            run_with_alert(boom, "yearn", name="test.script")

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        message = call_args.args[0]
        self.assertIn("test.script", message)
        self.assertIn("RuntimeError", message)
        self.assertIn("kaboom", message)
        # plain_text=True so Telegram doesn't parse Markdown on the exception string
        self.assertTrue(call_args.kwargs.get("plain_text"))
        self.assertTrue(call_args.kwargs.get("disable_notification"))
        self.assertEqual(call_args.args[1], "yearn")

    def test_alert_includes_github_run_url_when_present(self) -> None:
        def boom() -> None:
            raise ValueError("x")

        with (
            patch("utils.runner.send_telegram_message") as mock_send,
            patch("utils.runner.get_github_run_url", return_value="https://example.com/run/1"),
        ):
            run_with_alert(boom, "yearn", name="s")

        message = mock_send.call_args.args[0]
        self.assertIn("https://example.com/run/1", message)

    def test_systemexit_propagates(self) -> None:
        def quit_() -> None:
            raise SystemExit(2)

        with patch("utils.runner.send_telegram_message") as mock_send:
            with self.assertRaises(SystemExit):
                run_with_alert(quit_, "yearn")
        mock_send.assert_not_called()

    def test_keyboardinterrupt_propagates(self) -> None:
        def interrupted() -> None:
            raise KeyboardInterrupt()

        with patch("utils.runner.send_telegram_message") as mock_send:
            with self.assertRaises(KeyboardInterrupt):
                run_with_alert(interrupted, "yearn")
        mock_send.assert_not_called()

    def test_alert_send_failure_does_not_propagate(self) -> None:
        def boom() -> None:
            raise RuntimeError("primary")

        def telegram_dies(*args: object, **kwargs: object) -> None:
            raise ConnectionError("telegram unreachable")

        with patch("utils.runner.send_telegram_message", side_effect=telegram_dies):
            # Must still return cleanly even when alerting itself fails
            run_with_alert(boom, "yearn", name="s")

    def test_name_defaults_to_entrypoint_module(self) -> None:
        def boom() -> None:
            raise RuntimeError("x")

        with patch("utils.runner.send_telegram_message") as mock_send:
            run_with_alert(boom, "yearn")

        message = mock_send.call_args.args[0]
        self.assertIn(boom.__module__, message)


if __name__ == "__main__":
    unittest.main()

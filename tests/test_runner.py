"""Tests for utils.runner.run_with_alert."""

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.runner import run_with_alert


class TestRunWithAlert(unittest.TestCase):
    def test_success_runs_entrypoint_no_alert(self) -> None:
        called = {"n": 0}

        def ok() -> None:
            called["n"] += 1

        with patch("utils.runner.send_error_message") as mock_send:
            run_with_alert(ok, "yearn")
        self.assertEqual(called["n"], 1)
        mock_send.assert_not_called()

    def test_exception_sends_alert_and_returns(self) -> None:
        def boom() -> None:
            raise RuntimeError("kaboom")

        with patch("utils.runner.send_error_message") as mock_send:
            # Must NOT raise — the wrapper swallows and alerts
            run_with_alert(boom, "yearn", name="test.script")

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        message = call_args.args[0]
        self.assertIn("test.script", message)
        self.assertIn("RuntimeError", message)
        self.assertIn("kaboom", message)
        # Routed via send_error_message (plain-text + silent handling lives there);
        # the protocol is forwarded as the label / fallback channel.
        self.assertEqual(call_args.args[1], "yearn")

    def test_alert_protocol_is_forwarded_for_internal_crashes(self) -> None:
        def boom() -> None:
            raise RuntimeError("kaboom")

        with patch("utils.runner.send_error_message") as mock_send:
            run_with_alert(boom, "yearn", alert_protocol="yearn-internal")

        self.assertEqual(mock_send.call_args.args[1], "yearn")
        self.assertEqual(mock_send.call_args.kwargs["alert_protocol"], "yearn-internal")

    def test_alert_includes_github_run_url_when_present(self) -> None:
        def boom() -> None:
            raise ValueError("x")

        with (
            patch("utils.runner.send_error_message") as mock_send,
            patch("utils.runner.get_github_run_url", return_value="https://example.com/run/1"),
        ):
            run_with_alert(boom, "yearn", name="s")

        message = mock_send.call_args.args[0]
        self.assertIn("https://example.com/run/1", message)

    def test_systemexit_propagates(self) -> None:
        def quit_() -> None:
            raise SystemExit(2)

        with patch("utils.runner.send_error_message") as mock_send:
            with self.assertRaises(SystemExit):
                run_with_alert(quit_, "yearn")
        mock_send.assert_not_called()

    def test_keyboardinterrupt_propagates(self) -> None:
        def interrupted() -> None:
            raise KeyboardInterrupt()

        with patch("utils.runner.send_error_message") as mock_send:
            with self.assertRaises(KeyboardInterrupt):
                run_with_alert(interrupted, "yearn")
        mock_send.assert_not_called()

    def test_alert_send_failure_does_not_propagate(self) -> None:
        def boom() -> None:
            raise RuntimeError("primary")

        def telegram_dies(*args: object, **kwargs: object) -> None:
            raise ConnectionError("telegram unreachable")

        with patch("utils.runner.send_error_message", side_effect=telegram_dies):
            # Must still return cleanly even when alerting itself fails
            run_with_alert(boom, "yearn", name="s")

    def test_name_defaults_to_entrypoint_module(self) -> None:
        def boom() -> None:
            raise RuntimeError("x")

        with patch("utils.runner.send_error_message") as mock_send:
            run_with_alert(boom, "yearn")

        message = mock_send.call_args.args[0]
        self.assertIn(boom.__module__, message)


class TestRunWithAlertRedaction(unittest.TestCase):
    def test_crash_alert_redacts_provider_url(self) -> None:
        import os

        url = "https://eth-mainnet.g.alchemy.com/v2/crashkey1234567890ab"

        def boom() -> None:
            raise RuntimeError(f"All providers failed. Errors:\n{url}: 429")

        # Redaction happens in send_telegram_message, so follow the alert down to
        # the point where it is stored (every delivery path records it first).
        with (
            patch.dict(os.environ, {"PROVIDER_URL_MAINNET": url}, clear=True),
            patch("utils.telegram._record_alert_safe") as record,
        ):
            run_with_alert(boom, "aave", name="aave.main")

        message = record.call_args.kwargs["message"]
        self.assertNotIn("crashkey1234567890ab", message)
        self.assertIn("eth-mainnet.g.alchemy.com/REDACTED", message)


def _wait_for(predicate, timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _process_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class TestSigtermCleanup(unittest.TestCase):
    """A task stopped by the automation runner must take its detached CLI agent with it."""

    # The agent writes its pid, then outlives any reasonable test unless it is killed.
    SCRIPT = """
        import pathlib
        import sys
        from concurrent.futures import ThreadPoolExecutor

        from utils.llm.cli_provider import _run_cli
        from utils.runner import run_with_alert

        marker = sys.argv[1]
        use_thread = sys.argv[2] == "thread"
        agent = ["sh", "-c", f"echo $$ > {marker}; exec sleep 60"]


        def call_agent():
            _run_cli("agent", agent, "", pathlib.Path("."), 55)


        def main():
            if use_thread:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(call_agent).result()
            else:
                call_agent()


        run_with_alert(main, "test")
    """

    def _assert_agent_stops_on_sigterm(self, mode: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "agent.pid"
            script = Path(directory) / "task.py"
            script.write_text(textwrap.dedent(self.SCRIPT))
            repo_root = Path(__file__).resolve().parent.parent
            env = {**os.environ, "PYTHONPATH": str(repo_root), "LOG_LEVEL": "DEBUG"}
            task = subprocess.Popen([sys.executable, str(script), str(marker), mode], cwd=repo_root, env=env)
            try:
                self.assertTrue(
                    _wait_for(lambda: marker.exists() and marker.read_text().strip() != ""), "agent never started"
                )
                agent_pid = int(marker.read_text())

                task.send_signal(signal.SIGTERM)

                self.assertEqual(task.wait(timeout=20), 128 + signal.SIGTERM)
                self.assertTrue(_wait_for(lambda: _process_gone(agent_pid), timeout=5), "agent survived SIGTERM")
            finally:
                task.kill()
                task.wait()

    def test_agent_started_on_the_main_thread_is_stopped(self) -> None:
        self._assert_agent_stops_on_sigterm("main")

    def test_agent_started_on_a_worker_thread_is_stopped(self) -> None:
        self._assert_agent_stops_on_sigterm("thread")

    def test_previous_handler_is_restored(self) -> None:
        before = signal.getsignal(signal.SIGTERM)
        run_with_alert(lambda: None, "test")
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_runs_on_a_non_main_thread(self) -> None:
        called = threading.Event()
        thread = threading.Thread(target=run_with_alert, args=(called.set, "test"))
        thread.start()
        thread.join()
        self.assertTrue(called.is_set())


if __name__ == "__main__":
    unittest.main()

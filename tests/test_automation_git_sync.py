"""Tests for automation/git_sync.py."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from automation.git_sync import sync_to_remote_main


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestSyncToRemoteMain(unittest.TestCase):
    def test_requires_git_checkout(self):
        with TemporaryDirectory() as d:
            result = sync_to_remote_main(Path(d))

        self.assertFalse(result.ok)
        self.assertIn("not a git checkout", result.output)

    def test_fetches_then_hard_resets_origin_main(self):
        with TemporaryDirectory() as d:
            repo = Path(d)
            (repo / ".git").mkdir()

            with patch("automation.git_sync.subprocess.run", side_effect=[_Result(), _Result()]) as mock_run:
                result = sync_to_remote_main(repo)

        self.assertTrue(result.ok)
        self.assertEqual(
            [call.args[0] for call in mock_run.call_args_list],
            [
                ["git", "-C", str(repo), "fetch", "--quiet", "origin", "main"],
                ["git", "-C", str(repo), "reset", "--hard", "--quiet", "origin/main"],
            ],
        )

    def test_fetch_failure_stops_before_reset(self):
        with TemporaryDirectory() as d:
            repo = Path(d)
            (repo / ".git").mkdir()

            with patch(
                "automation.git_sync.subprocess.run", return_value=_Result(1, stderr="network down")
            ) as mock_run:
                result = sync_to_remote_main(repo)

        self.assertFalse(result.ok)
        self.assertEqual(mock_run.call_count, 1)
        self.assertIn("network down", result.output)

    def test_reset_failure_is_reported(self):
        with TemporaryDirectory() as d:
            repo = Path(d)
            (repo / ".git").mkdir()

            with patch(
                "automation.git_sync.subprocess.run",
                side_effect=[_Result(), _Result(128, stderr="cannot lock ref")],
            ):
                result = sync_to_remote_main(repo)

        self.assertFalse(result.ok)
        self.assertIn("cannot lock ref", result.output)


if __name__ == "__main__":
    unittest.main()

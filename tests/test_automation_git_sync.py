"""Tests for automation/git_sync.py."""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from automation.git_sync import UV_SYNC_ARGV, sync_dependencies, sync_to_remote_main


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


class TestSyncDependencies(unittest.TestCase):
    def test_runs_frozen_uv_sync_in_repo(self):
        with (
            patch.dict(os.environ, {"CACHE_DIR": "/srv/cache", "VIRTUAL_ENV": "/elsewhere"}, clear=True),
            patch("automation.git_sync.subprocess.run", return_value=_Result(stderr="Checked 64 packages")) as mock_run,
        ):
            result = sync_dependencies(Path("/srv/repo"))

        self.assertTrue(result.ok)
        self.assertEqual(result.output, "Checked 64 packages")
        self.assertEqual(mock_run.call_args.args[0], UV_SYNC_ARGV)
        self.assertIn("--frozen", UV_SYNC_ARGV)
        self.assertEqual(mock_run.call_args.kwargs["cwd"], Path("/srv/repo"))
        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(env["UV_CACHE_DIR"], "/srv/cache/uv/cache")
        self.assertEqual(env["UV_PYTHON_INSTALL_DIR"], "/srv/cache/uv/python")
        self.assertNotIn("VIRTUAL_ENV", env)

    def test_respects_operator_uv_cache_dir(self):
        with (
            patch.dict(os.environ, {"CACHE_DIR": "/srv/cache", "UV_CACHE_DIR": "/custom"}, clear=True),
            patch("automation.git_sync.subprocess.run", return_value=_Result()) as mock_run,
        ):
            sync_dependencies(Path("/srv/repo"))

        self.assertEqual(mock_run.call_args.kwargs["env"]["UV_CACHE_DIR"], "/custom")

    def test_failure_is_reported(self):
        with patch("automation.git_sync.subprocess.run", return_value=_Result(2, stderr="lockfile out of date")):
            result = sync_dependencies(Path("/srv/repo"))

        self.assertFalse(result.ok)
        self.assertIn("lockfile out of date", result.output)

    def test_missing_uv_is_reported(self):
        with patch("automation.git_sync.subprocess.run", side_effect=FileNotFoundError("uv")):
            result = sync_dependencies(Path("/srv/repo"))

        self.assertFalse(result.ok)
        self.assertIn("failed to spawn", result.output)


if __name__ == "__main__":
    unittest.main()

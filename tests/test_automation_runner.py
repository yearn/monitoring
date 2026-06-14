"""Tests for automation/runner.py."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.config import Profile, Task
from automation.runner import ProfileResult, TaskResult, build_argv, run_profile


def _profile(tasks: list[Task], env: dict[str, str] | None = None, sync_before_run: bool = False) -> Profile:
    return Profile(
        name="hourly",
        cron="26 * * * *",
        tasks=tasks,
        env=env or {},
        sync_before_run=sync_before_run,
    )


class TestBuildArgv(unittest.TestCase):
    def test_no_args(self):
        task = Task(name="aave", script="aave/main.py")
        self.assertEqual(build_argv(task), [sys.executable, "aave/main.py"])

    def test_args_preserve_order_and_kebab(self):
        task = Task(name="x", script="x.py", args={"cache-file": "/srv/cache/x.json", "verbose": "true"})
        argv = build_argv(task)
        self.assertEqual(
            argv,
            [sys.executable, "x.py", "--cache-file=/srv/cache/x.json", "--verbose=true"],
        )

    def test_explicit_python(self):
        task = Task(name="x", script="x.py")
        argv = build_argv(task, python="/opt/py")
        self.assertEqual(argv, ["/opt/py", "x.py"])


class TestRunProfileDryRun(unittest.TestCase):
    def test_dry_run_does_not_invoke_subprocess(self):
        profile = _profile([Task(name="x", script="x.py"), Task(name="y", script="y.py")])
        with patch("automation.runner.subprocess.run") as mock_run:
            result = run_profile(profile, repo_root=Path("/tmp"), dry_run=True)
        mock_run.assert_not_called()
        self.assertEqual(len(result.tasks), 2)
        self.assertTrue(all(t.skipped for t in result.tasks))
        self.assertTrue(result.ok)


class TestRunProfileSuccess(unittest.TestCase):
    def test_passes_env_and_cwd(self):
        profile = _profile(
            [Task(name="x", script="x.py")],
            env={"CACHE_FILENAME": "/srv/cache/foo.txt"},
        )

        class _Result:
            returncode = 0

        with patch("automation.runner.subprocess.run", return_value=_Result()) as mock_run:
            run_profile(profile, repo_root=Path("/srv/repo"), dry_run=False, send_digest=False)

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], [sys.executable, "x.py"])
        self.assertEqual(kwargs["cwd"], Path("/srv/repo"))
        self.assertEqual(kwargs["env"]["CACHE_FILENAME"], "/srv/cache/foo.txt")
        self.assertFalse(kwargs["check"])

    def test_sync_before_run_forces_remote_main_sync(self):
        profile = _profile([Task(name="x", script="x.py")], sync_before_run=True)

        class _Result:
            returncode = 0

        with (
            patch("automation.runner.git_sync.sync_to_remote_main") as mock_sync,
            patch("automation.runner.subprocess.run", return_value=_Result()),
        ):
            run_profile(profile, repo_root=Path("/srv/repo"), dry_run=False, send_digest=False)

        mock_sync.assert_called_once_with(Path("/srv/repo"))


class TestRunProfileContinuesOnFailure(unittest.TestCase):
    def test_continues_after_non_zero_exit(self):
        profile = _profile([Task(name="a", script="a.py"), Task(name="b", script="b.py")])

        class _Ok:
            returncode = 0

        class _Bad:
            returncode = 2

        with patch("automation.runner.subprocess.run", side_effect=[_Bad(), _Ok()]) as mock_run:
            with patch("automation.runner._send_failure_digest") as mock_digest:
                result = run_profile(profile, repo_root=Path("/"), dry_run=False)

        self.assertEqual(mock_run.call_count, 2)
        self.assertFalse(result.ok)
        self.assertEqual([f.name for f in result.failures], ["a"])
        mock_digest.assert_called_once()

    def test_spawn_failure_recorded(self):
        profile = _profile([Task(name="x", script="x.py")])
        with patch("automation.runner.subprocess.run", side_effect=OSError("no such file")):
            with patch("automation.runner._send_failure_digest"):
                result = run_profile(profile, repo_root=Path("/"), dry_run=False)
        self.assertFalse(result.ok)
        self.assertIn("spawn failed", result.failures[0].error or "")


class TestTelegramSummary(unittest.TestCase):
    def test_lists_failures(self):
        result = ProfileResult(
            profile="hourly",
            started_at=0.0,
            finished_at=12.5,
            tasks=[
                TaskResult(name="ok", script="ok.py", returncode=0, duration_s=1.0),
                TaskResult(name="bad", script="bad.py", returncode=3, duration_s=2.0),
            ],
        )
        body = result.telegram_summary()
        self.assertIn("automation: hourly", body)
        self.assertIn("12.5s", body)
        self.assertIn("bad", body)
        self.assertIn("exit 3", body)

    def test_error_tail_rendered_in_fenced_block(self):
        result = ProfileResult(
            profile="hourly",
            started_at=0.0,
            finished_at=3.0,
            tasks=[
                TaskResult(
                    name="yearn-check-stuck-triggers",
                    script="yearn/check_stuck_triggers.py",
                    returncode=1,
                    duration_s=2.0,
                    error="RuntimeError: RPC timeout on mainnet",
                ),
            ],
        )
        body = result.telegram_summary()
        # The actionable tail is carried, not just the exit code.
        self.assertIn("RuntimeError: RPC timeout on mainnet", body)
        # It lives inside a code fence so Markdown metacharacters can't break parsing.
        self.assertIn("```", body)

    def test_summary_is_markdown_parse_safe(self):
        """A traceback full of Markdown metacharacters must not produce unbalanced entities.

        The tail goes inside a ``` fence (literal content), so the only fence-relevant
        character is the backtick — which must be neutralized, leaving exactly the two
        fence markers we emit.
        """
        nasty = "File `check_stuck_triggers.py`, line 42: a*b _c_ [d]"
        result = ProfileResult(
            profile="hourly",
            started_at=0.0,
            finished_at=1.0,
            tasks=[
                TaskResult(name="bad", script="bad.py", returncode=1, duration_s=1.0, error=nasty),
            ],
        )
        body = result.telegram_summary()
        # Backticks in the tail are neutralized; only our two fence markers remain.
        self.assertEqual(body.count("```"), 2)
        # Bold/italic markers from the captured text survive verbatim inside the fence.
        self.assertIn("a*b _c_ [d]", body)


if __name__ == "__main__":
    unittest.main()

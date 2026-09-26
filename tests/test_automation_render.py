"""Tests for `python -m automation render-crontab`."""

import io
import os
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from automation.__main__ import cmd_render_crontab, cmd_run
from automation.config import load_jobs_config
from automation.crontab import CRONTAB_PATH_ENV


def _write_yaml(tmp: Path, body: str) -> Path:
    path = tmp / "jobs.yaml"
    path.write_text(textwrap.dedent(body))
    return path


class TestRenderCrontab(unittest.TestCase):
    def test_one_line_per_enabled_profile(self):
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  hourly:
                    cron: "26 * * * *"
                    tasks: [{ name: "a", script: a/main.py }]
                  daily:
                    cron: "19 8 * * *"
                    tasks: [{ name: "b", script: b/main.py }]
                  weekly:
                    cron: "19 8 * * 0"
                    enabled: false
                    tasks: [{ name: "c", script: c/main.py }]
                """,
            )
            cfg = load_jobs_config(path)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_render_crontab(cfg)
            self.assertEqual(rc, 0)
            lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 2)  # weekly is disabled

    def test_lines_wrap_with_flock(self):
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  hourly:
                    cron: "26 * * * *"
                    tasks: [{ name: "a", script: a/main.py }]
                """,
            )
            cfg = load_jobs_config(path)
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_render_crontab(cfg)
            line = buf.getvalue().strip()
            # Cron expression preserved verbatim, then flock, then the invocation.
            self.assertTrue(line.startswith("26 * * * *"))
            self.assertIn("flock -n", line)
            self.assertIn("/tmp/automation.hourly.lock", line)
            self.assertIn("python -m automation run hourly", line)

    def test_lock_paths_distinct_per_profile(self):
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  hourly:
                    cron: "26 * * * *"
                    tasks: [{ name: "a", script: a/main.py }]
                  yearn-stuck-triggers:
                    cron: "26 * * * *"
                    tasks: [{ name: "b", script: b/main.py }]
                """,
            )
            cfg = load_jobs_config(path)
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_render_crontab(cfg)
            text = buf.getvalue()
            self.assertIn("/tmp/automation.hourly.lock", text)
            self.assertIn("/tmp/automation.yearn-stuck-triggers.lock", text)


class TestRunUnknownProfile(unittest.TestCase):
    """A stale crontab calling a renamed profile must still sync the checkout."""

    def _config(self, d: str):
        return load_jobs_config(
            _write_yaml(
                Path(d),
                """
                profiles:
                  ten_minute:
                    cron: "0/10 * * * *"
                    tasks: [{ name: "a", script: a/main.py }]
                """,
            )
        )

    def _run(self, *, scheduler: bool, dry_run: bool = False):
        env = {CRONTAB_PATH_ENV: "/tmp/crontab"} if scheduler else {}
        with (
            TemporaryDirectory() as d,
            patch.dict(os.environ, env),
            patch("automation.__main__.sync_repo") as mock_sync,
        ):
            if not scheduler:
                os.environ.pop(CRONTAB_PATH_ENV, None)
            rc = cmd_run(self._config(d), "multisig", dry_run=dry_run)
        self.assertEqual(rc, 2)
        return mock_sync

    def test_unknown_profile_under_scheduler_still_syncs(self):
        self._run(scheduler=True).assert_called_once()

    def test_unknown_profile_outside_scheduler_does_not_sync(self):
        self._run(scheduler=False).assert_not_called()

    def test_unknown_profile_dry_run_does_not_sync(self):
        self._run(scheduler=True, dry_run=True).assert_not_called()


if __name__ == "__main__":
    unittest.main()

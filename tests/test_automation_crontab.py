"""Tests for automation/crontab.py live-crontab refresh."""

import os
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from automation.config import load_jobs_config
from automation.crontab import CRONTAB_PATH_ENV, refresh_live_crontab, render_crontab

_JOBS = """
profiles:
  ten_minute:
    cron: "0/10 * * * *"
    tasks: [{ name: "a", script: a/main.py }]
"""


def _write_yaml(tmp: Path, body: str) -> Path:
    path = tmp / "jobs.yaml"
    path.write_text(textwrap.dedent(body))
    return path


class TestRefreshLiveCrontab(unittest.TestCase):
    def test_noop_without_env(self):
        with TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CRONTAB_PATH_ENV, None)
            self.assertFalse(refresh_live_crontab(_write_yaml(Path(d), _JOBS)))

    def test_rewrites_stale_crontab(self):
        with TemporaryDirectory() as d:
            jobs = _write_yaml(Path(d), _JOBS)
            live = Path(d) / "crontab"
            live.write_text("0/10 * * * *\tflock -n /tmp/automation.multisig.lock python -m automation run multisig\n")
            with patch.dict(os.environ, {CRONTAB_PATH_ENV: str(live)}):
                self.assertTrue(refresh_live_crontab(jobs))
            self.assertEqual(live.read_text(), render_crontab(load_jobs_config(jobs)))
            self.assertIn("run ten_minute", live.read_text())

    def test_unchanged_crontab_not_rewritten(self):
        with TemporaryDirectory() as d:
            jobs = _write_yaml(Path(d), _JOBS)
            live = Path(d) / "crontab"
            live.write_text(render_crontab(load_jobs_config(jobs)))
            with patch.dict(os.environ, {CRONTAB_PATH_ENV: str(live)}):
                self.assertFalse(refresh_live_crontab(jobs))

    def test_invalid_jobs_yaml_keeps_current_crontab(self):
        with TemporaryDirectory() as d:
            jobs = _write_yaml(Path(d), "profiles: {}\n")
            live = Path(d) / "crontab"
            live.write_text("keep me\n")
            with patch.dict(os.environ, {CRONTAB_PATH_ENV: str(live)}):
                self.assertFalse(refresh_live_crontab(jobs))
            self.assertEqual(live.read_text(), "keep me\n")


if __name__ == "__main__":
    unittest.main()

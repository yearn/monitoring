"""Tests for `python -m automation render-crontab`."""

import io
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from automation.__main__ import cmd_render_crontab
from automation.config import load_jobs_config


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


if __name__ == "__main__":
    unittest.main()

"""Tests for automation/config.py."""

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from automation.config import JobsConfigError, load_jobs_config


def _write_yaml(tmp: Path, body: str) -> Path:
    path = tmp / "jobs.yaml"
    path.write_text(textwrap.dedent(body))
    return path


class TestLoadJobsConfig(unittest.TestCase):
    def test_minimal(self):
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  hourly:
                    cron: "26 * * * *"
                    tasks:
                      - { name: "aave", script: aave/main.py }
                """,
            )
            cfg = load_jobs_config(path)
            self.assertEqual(list(cfg.profiles), ["hourly"])
            self.assertEqual(cfg.profiles["hourly"].cron, "26 * * * *")
            self.assertEqual(len(cfg.profiles["hourly"].tasks), 1)
            self.assertEqual(cfg.profiles["hourly"].tasks[0].script, "aave/main.py")

    def test_env_and_args(self):
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  hourly:
                    cron: "26 * * * *"
                    env:
                      CACHE_FILENAME: /srv/cache/cache-id.txt
                    tasks:
                      - name: "stuck"
                        script: yearn/check_stuck_triggers.py
                        args:
                          cache-file: /srv/cache/tks.json
                """,
            )
            cfg = load_jobs_config(path)
            profile = cfg.profiles["hourly"]
            self.assertEqual(profile.env, {"CACHE_FILENAME": "/srv/cache/cache-id.txt"})
            self.assertEqual(profile.tasks[0].args, {"cache-file": "/srv/cache/tks.json"})

    def test_disabled_profile_and_task(self):
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  hourly:
                    cron: "26 * * * *"
                    tasks:
                      - { name: "ok",  script: a/main.py }
                      - { name: "off", script: b/main.py, enabled: false }
                  weekly:
                    cron: "19 8 * * 0"
                    enabled: false
                    tasks:
                      - { name: "endorsed", script: yearn/check_endorsed.py }
                """,
            )
            cfg = load_jobs_config(path)
            self.assertEqual([p.name for p in cfg.enabled_profiles], ["hourly"])
            hourly = cfg.profiles["hourly"]
            self.assertEqual([t.name for t in hourly.enabled_tasks], ["ok"])

    def test_missing_top_level_profiles(self):
        with TemporaryDirectory() as d:
            path = _write_yaml(Path(d), "other: 1\n")
            with self.assertRaises(JobsConfigError):
                load_jobs_config(path)

    def test_empty_profiles(self):
        with TemporaryDirectory() as d:
            path = _write_yaml(Path(d), "profiles: {}\n")
            with self.assertRaises(JobsConfigError):
                load_jobs_config(path)

    def test_task_missing_script(self):
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  hourly:
                    cron: "26 * * * *"
                    tasks:
                      - { name: "broken" }
                """,
            )
            with self.assertRaises(JobsConfigError) as ctx:
                load_jobs_config(path)
            self.assertIn("script", str(ctx.exception))

    def test_unknown_task_key(self):
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  hourly:
                    cron: "26 * * * *"
                    tasks:
                      - { name: "x", script: a/main.py, typo: oops }
                """,
            )
            with self.assertRaises(JobsConfigError) as ctx:
                load_jobs_config(path)
            self.assertIn("typo", str(ctx.exception))

    def test_unknown_profile_key(self):
        with TemporaryDirectory() as d:
            path = _write_yaml(
                Path(d),
                """
                profiles:
                  hourly:
                    cron: "26 * * * *"
                    unknown_field: 1
                    tasks: []
                """,
            )
            with self.assertRaises(JobsConfigError) as ctx:
                load_jobs_config(path)
            self.assertIn("unknown_field", str(ctx.exception))

    def test_missing_file(self):
        with TemporaryDirectory() as d:
            with self.assertRaises(JobsConfigError):
                load_jobs_config(Path(d) / "does-not-exist.yaml")


class TestRepoJobsYaml(unittest.TestCase):
    """Smoke test against the actual jobs.yaml shipped in the repo."""

    def test_repo_jobs_yaml_parses(self):
        repo_yaml = Path(__file__).resolve().parent.parent / "automation" / "jobs.yaml"
        cfg = load_jobs_config(repo_yaml)
        self.assertGreaterEqual(len(cfg.profiles), 5)
        for expected in ("hourly", "daily", "weekly", "multisig", "yearn-stuck-triggers"):
            self.assertIn(expected, cfg.profiles, f"missing profile {expected}")


if __name__ == "__main__":
    unittest.main()

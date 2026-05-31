"""CLI entry point: `python -m automation <list|render-crontab|run>`.

Used by supercronic (rendered crontab invokes `run <profile>`) and by operators for
local dry-runs.
"""

from __future__ import annotations

import argparse
import logging
import shlex
import sys
from pathlib import Path

from automation.config import REPO_ROOT, JobsConfig, JobsConfigError, load_jobs_config
from automation.runner import run_profile

# Lock dir for `flock -n` wrappers emitted by render-crontab. /tmp is fine — the container
# tmpfs survives across cron ticks but not container restarts, which is the correct scope.
LOCK_DIR = "/tmp"

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    import os

    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_list(config: JobsConfig) -> int:
    for profile in config.profiles.values():
        marker = "" if profile.enabled else " (disabled)"
        print(f"{profile.name}  cron={profile.cron!r}  tasks={len(profile.tasks)}{marker}")
    return 0


def cmd_render_crontab(config: JobsConfig) -> int:
    """Emit a supercronic-compatible crontab.

    Each enabled profile becomes one line, wrapped in `flock -n` to prevent overlapping runs
    of the same profile (mirrors `concurrency: cancel-in-progress: true` on the existing GH
    Actions workflows). `flock -n` returns non-zero immediately if the lock is held —
    supercronic logs the skip, and the next tick tries again.

    The crontab calls `python -m automation run <profile>`; `python` resolves to the venv's
    interpreter because the image's PATH starts with /app/.venv/bin.
    """
    lines: list[str] = []
    for profile in config.enabled_profiles:
        lock_path = f"{LOCK_DIR}/automation.{profile.name}.lock"
        command = shlex.join(["python", "-m", "automation", "run", profile.name])
        lines.append(f"{profile.cron}\tflock -n {lock_path} {command}")
    print("\n".join(lines))
    return 0


def cmd_run(config: JobsConfig, profile_name: str, *, dry_run: bool) -> int:
    profile = config.profiles.get(profile_name)
    if profile is None:
        print(f"error: unknown profile {profile_name!r}", file=sys.stderr)
        print(f"known profiles: {', '.join(config.profiles)}", file=sys.stderr)
        return 2
    if not profile.enabled:
        print(f"profile {profile_name!r} is disabled, skipping", file=sys.stderr)
        return 0

    result = run_profile(profile, repo_root=REPO_ROOT, dry_run=dry_run)
    if result.failures:
        for failure in result.failures:
            print(
                f"FAIL {failure.name}: {failure.error or f'exit {failure.returncode}'}",
                file=sys.stderr,
            )
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m automation", description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to jobs.yaml (default: automation/jobs.yaml in REPO_ROOT)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List configured profiles")
    sub.add_parser("render-crontab", help="Emit a supercronic-compatible crontab")

    run_p = sub.add_parser("run", help="Run one profile")
    run_p.add_argument("profile")
    run_p.add_argument("--dry-run", action="store_true", help="Print the argv that would run; do not execute")

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = build_parser().parse_args(argv)

    try:
        config = load_jobs_config(args.config)
    except JobsConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "list":
        return cmd_list(config)
    if args.command == "render-crontab":
        return cmd_render_crontab(config)
    if args.command == "run":
        return cmd_run(config, args.profile, dry_run=args.dry_run)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())

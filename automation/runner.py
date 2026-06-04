"""Execute a profile's tasks as subprocesses and post a single Telegram digest on failure.

The runner is intentionally dumb: every task is a Python script invocation plus an optional
dict of CLI flags. Each task runs in its own subprocess, returncode/duration is captured, and
a single Markdown digest is sent through `utils.telegram.send_telegram_message` after all tasks
finish — so a profile with N failing tasks produces one alert, not N.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from automation import git_sync
from automation.config import Profile, Task
from utils.telegram import TelegramError, send_telegram_message

logger = logging.getLogger(__name__)

# Protocol slug used to look up Telegram credentials for the failure digest. Falls back to
# TELEGRAM_BOT_TOKEN_DEFAULT / TELEGRAM_CHAT_ID_DEFAULT, mirroring how scripts without a
# dedicated channel are routed today.
TELEGRAM_PROTOCOL: str = "automation"


@dataclass
class TaskResult:
    name: str
    script: str
    returncode: int
    duration_s: float
    skipped: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.skipped or self.returncode == 0


@dataclass
class ProfileResult:
    profile: str
    started_at: float
    finished_at: float
    tasks: list[TaskResult] = field(default_factory=list)
    dry_run: bool = False

    @property
    def failures(self) -> list[TaskResult]:
        return [t for t in self.tasks if not t.ok]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def duration_s(self) -> float:
        return self.finished_at - self.started_at

    def telegram_summary(self) -> str:
        lines = [
            f"*automation: {self.profile}*",
            f"duration: {self.duration_s:.1f}s",
            f"tasks: {len(self.tasks)} ({len(self.failures)} failed)",
        ]
        for failure in self.failures:
            detail = failure.error or f"exit {failure.returncode}"
            lines.append(f"- {failure.name}: {detail}")
        return "\n".join(lines)


def build_argv(task: Task, *, python: str | None = None) -> list[str]:
    """Build the subprocess argv for a task.

    Args are emitted as `--<key>=<value>` in the order they appear in jobs.yaml. Keys keep
    their declared casing (kebab-case in YAML stays kebab-case on the command line) so
    matching the existing script CLIs (e.g. `--cache-file`) is one-to-one.
    """
    interpreter = python or sys.executable
    argv = [interpreter, task.script]
    for key, value in task.args.items():
        argv.append(f"--{key}={value}")
    return argv


def run_profile(
    profile: Profile,
    *,
    repo_root: Path,
    dry_run: bool = False,
    send_digest: bool = True,
) -> ProfileResult:
    """Run every enabled task in `profile`, continuing on per-task failure.

    Returns a ProfileResult; the caller (CLI / supercronic) decides whether to surface
    failures via exit code.
    """
    started = time.monotonic()
    started_wall = time.time()
    result = ProfileResult(profile=profile.name, started_at=started_wall, finished_at=started_wall, dry_run=dry_run)

    if profile.sync_before_run and not dry_run:
        _sync_repo(repo_root)

    for task in profile.enabled_tasks:
        result.tasks.append(_run_task(task, profile=profile, repo_root=repo_root, dry_run=dry_run))

    result.finished_at = result.started_at + (time.monotonic() - started)

    if send_digest and result.failures and not dry_run:
        _send_failure_digest(result)
    return result


def _sync_repo(repo_root: Path) -> None:
    """Fast-forward the checkout to origin before running the profile's tasks.

    Best-effort: a failed pull is logged but never blocks the run — these are
    read-only checks, so running slightly older code is harmless, and we never
    want a transient git hiccup to silence an alert. See `automation.git_sync`.
    """
    result = git_sync.pull_ff_only(repo_root)
    if result.ok:
        logger.info("pre-run git sync: %s", result.output or "already up to date")
    else:
        logger.warning("pre-run git sync failed (running existing checkout): %s", result.output)


def _run_task(task: Task, *, profile: Profile, repo_root: Path, dry_run: bool) -> TaskResult:
    argv = build_argv(task)
    env = {**os.environ, **profile.env}

    if dry_run:
        logger.info("[would run] %s (env: %s)", " ".join(argv), ", ".join(profile.env) or "-")
        return TaskResult(name=task.name, script=task.script, returncode=0, duration_s=0.0, skipped=True)

    logger.info("running task %s: %s", task.name, " ".join(argv))
    start = time.monotonic()
    try:
        completed = subprocess.run(argv, cwd=repo_root, env=env, check=False)
    except OSError as exc:
        duration = time.monotonic() - start
        logger.exception("task %s failed to spawn", task.name)
        return TaskResult(
            name=task.name,
            script=task.script,
            returncode=-1,
            duration_s=duration,
            error=f"spawn failed: {exc}",
        )
    duration = time.monotonic() - start

    if completed.returncode != 0:
        logger.warning("task %s exited %d after %.1fs", task.name, completed.returncode, duration)
    else:
        logger.info("task %s ok in %.1fs", task.name, duration)
    return TaskResult(
        name=task.name,
        script=task.script,
        returncode=completed.returncode,
        duration_s=duration,
    )


def _send_failure_digest(result: ProfileResult) -> None:
    message = result.telegram_summary()
    try:
        send_telegram_message(message, protocol=TELEGRAM_PROTOCOL, plain_text=False)
    except TelegramError as exc:
        logger.error("failed to send automation digest for %s: %s", result.profile, exc)

"""Render the supercronic crontab from jobs.yaml and keep the live copy current.

supercronic runs with `-inotify`, so rewriting the crontab file it was started
with makes it reload the schedule without a service restart. After every pre-run
git sync the runner re-renders the crontab from the freshly pulled jobs.yaml and
rewrites the live file when it differs. Adding, removing or renaming a profile,
or changing its `cron:`, then takes effect on the next sync instead of waiting
for a manual `systemctl restart monitoring`.

The restart-only behavior caused a two-day outage on 2026-09-24: the sync-anchor
profile `multisig` was renamed, supercronic kept calling `run multisig`, that
failed before its sync ran, and the box stopped pulling code.
"""

from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path

from automation.config import JobsConfig, JobsConfigError, load_jobs_config

# Lock dir for `flock -n` wrappers. /tmp is fine — under the monitoring systemd unit it's a
# per-service PrivateTmp that survives across cron ticks but not service restarts, which is
# the correct scope.
LOCK_DIR = "/tmp"

# Path of the crontab supercronic is running, set by the systemd unit. Unset (local runs,
# tests) means there is no live crontab to refresh.
CRONTAB_PATH_ENV = "CRONTAB_PATH"

logger = logging.getLogger(__name__)


def is_scheduler_run() -> bool:
    """Return True when running under the systemd/supercronic scheduler.

    Only the monitoring unit sets `CRONTAB_PATH`, so local and operator runs return False.
    """
    return bool(os.environ.get(CRONTAB_PATH_ENV))


def render_crontab(config: JobsConfig) -> str:
    """Render a supercronic-compatible crontab, one line per enabled profile.

    Each line is wrapped in `flock -n` to prevent overlapping runs of the same profile.
    `flock -n` returns non-zero immediately if the lock is held — supercronic logs the skip,
    and the next tick tries again. `python` resolves to the venv's interpreter because the
    unit's PATH starts with the venv's bin dir.

    Args:
        config: Parsed jobs.yaml.

    Returns:
        The crontab text, newline-terminated.
    """
    lines: list[str] = []
    for profile in config.enabled_profiles:
        lock_path = f"{LOCK_DIR}/automation.{profile.name}.lock"
        command = shlex.join(["python", "-m", "automation", "run", profile.name])
        lines.append(f"{profile.cron}\tflock -n {lock_path} {command}")
    return "\n".join(lines) + "\n"


def refresh_live_crontab(config_path: Path | None = None) -> bool:
    """Rewrite the live crontab if jobs.yaml now renders differently.

    Best-effort and never raises: a malformed jobs.yaml or an unwritable file is logged,
    and supercronic keeps the schedule it already has.

    Args:
        config_path: jobs.yaml to render from (defaults to `automation/jobs.yaml`).

    Returns:
        True when the live crontab was rewritten.
    """
    live = os.environ.get(CRONTAB_PATH_ENV)
    if not live:
        return False
    live_path = Path(live)

    try:
        rendered = render_crontab(load_jobs_config(config_path))
    except JobsConfigError as exc:
        logger.error("crontab refresh skipped, jobs.yaml is invalid (keeping current schedule): %s", exc)
        return False

    try:
        current = live_path.read_text() if live_path.exists() else ""
        if current == rendered:
            return False
        # Write in place rather than rename-over: supercronic's inotify watch is on the file.
        live_path.write_text(rendered)
    except OSError as exc:
        logger.error("crontab refresh failed to write %s: %s", live_path, exc)
        return False

    logger.warning("jobs.yaml schedule changed; rewrote %s for supercronic to reload:\n%s", live_path, rendered)
    return True

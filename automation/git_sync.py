"""Force the live VPS checkout to match origin/main before a profile runs.

Deliberately minimal: fetch ``origin/main`` and hard-reset the checkout to it.
The monitoring app is pure read-only — it never commits or writes anything back
into the checkout — so local tracked-file drift is disposable. If an operator
hot-edits a tracked file or the local branch diverges, the next sync discards it
in favor of the reviewed remote ``main`` branch. The worst case of a failed sync
is that we run slightly older read-only code, which is harmless. Callers
therefore log and carry on rather than skipping the run.

Anchored on the most frequent profile (`ten_minute`, every 10 min via
`sync_before_run` in jobs.yaml): one sync there keeps the whole tree current for
every other profile, since supercronic re-spawns each profile fresh against
whatever is on disk.

After a successful sync the runner also runs ``uv sync --frozen`` so a
``pyproject.toml`` / ``uv.lock`` change (including a Python version bump) lands
in the venv together with the code that needs it. It is a ~40ms no-op when the
venv already matches the lockfile, so it runs unconditionally: a failed install
is retried on the next tick instead of being skipped because HEAD did not move.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Mirrors deploy/install.sh: the production venv carries the `ai` extra (LLM clients).
UV_SYNC_ARGV: list[str] = ["uv", "sync", "--frozen", "--extra", "ai"]

# Bounds on each step. A stalled `git fetch` (half-open TCP to GitHub) would
# otherwise block the sync profile forever: its flock stays held, so every later
# tick — and with it code sync — is skipped.
GIT_FETCH_TIMEOUT_SECONDS = 120
GIT_RESET_TIMEOUT_SECONDS = 60
UV_SYNC_TIMEOUT_SECONDS = 600
# On timeout a step gets SIGTERM and this long to exit before SIGKILL. git
# removes its lock files on SIGTERM; a SIGKILL leaves them behind.
TERMINATE_GRACE_SECONDS = 10
# A git lock file older than this cannot belong to a live sync step (each one is
# bounded well below it), so it was left by a killed process and is removed.
# Otherwise every later sync fails with "index.lock: File exists".
STALE_LOCK_SECONDS = 30 * 60


@dataclass
class SyncResult:
    """Outcome of a forced sync to origin/main."""

    ok: bool
    output: str


def _run(
    argv: list[str],
    timeout: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` capturing text output, terminating it gracefully on timeout.

    ``subprocess.run(timeout=...)`` sends SIGKILL, which leaves git lock files in
    place. This sends SIGTERM first and only kills after ``TERMINATE_GRACE_SECONDS``.
    The process runs in its own process group and the signals go to the whole
    group, so helpers it spawned (git-remote-https, uv workers) are stopped too.
    Draining their output is bounded as well: a descendant that escaped the group
    and still holds the pipes cannot keep the sync profile locked.

    Args:
        argv: Command and arguments.
        timeout: Seconds before the process is terminated.
        cwd: Working directory for the process.
        env: Environment for the process; inherits the current one when None.

    Returns:
        The completed process.

    Raises:
        subprocess.TimeoutExpired: If the process outlived ``timeout``.
        OSError: If the process could not be spawned.
    """
    with subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                _signal_group(proc, sig)
                try:
                    proc.communicate(timeout=TERMINATE_GRACE_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    continue
            raise subprocess.TimeoutExpired(argv, timeout) from None
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _signal_group(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Send ``sig`` to the process group led by ``proc``, ignoring an already-empty group."""
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def _remove_stale_git_locks(repo_root: Path) -> None:
    """Delete git lock files left behind by a killed git process.

    Args:
        repo_root: Path to the git checkout.
    """
    git_dir = repo_root / ".git"
    cutoff = time.time() - STALE_LOCK_SECONDS
    for lock in (*git_dir.glob("*.lock"), *git_dir.glob("refs/**/*.lock")):
        try:
            if lock.stat().st_mtime < cutoff:
                lock.unlink()
                logger.warning("Removed stale git lock %s", lock)
        except OSError as exc:
            logger.warning("Could not remove stale git lock %s: %s", lock, exc)


def sync_to_remote_main(repo_root: Path) -> SyncResult:
    """Force `repo_root` to match origin/main.

    Args:
        repo_root: Path to the git checkout to update.

    Returns:
        SyncResult with `ok` False when git is missing, the path is not a
        checkout, or fetch/reset fails. Never raises.
    """
    if not (repo_root / ".git").exists():
        return SyncResult(ok=False, output=f"{repo_root} is not a git checkout")

    _remove_stale_git_locks(repo_root)
    try:
        fetch = _run(
            ["git", "-C", str(repo_root), "fetch", "--quiet", "origin", "main"],
            timeout=GIT_FETCH_TIMEOUT_SECONDS,
        )
        if fetch.returncode != 0:
            output = (fetch.stdout + fetch.stderr).strip()
            return SyncResult(ok=False, output=output or f"fetch exited {fetch.returncode}")

        reset = _run(
            ["git", "-C", str(repo_root), "reset", "--hard", "--quiet", "origin/main"],
            timeout=GIT_RESET_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return SyncResult(ok=False, output=f"git sync timed out after {exc.timeout}s: {' '.join(exc.cmd)}")
    except OSError as exc:
        return SyncResult(ok=False, output=f"git sync failed to spawn: {exc}")

    output = (reset.stdout + reset.stderr).strip()
    if reset.returncode != 0:
        return SyncResult(ok=False, output=output or f"reset exited {reset.returncode}")
    return SyncResult(ok=True, output=output)


def _uv_env() -> dict[str, str]:
    """Build the environment for `uv sync` under the systemd unit.

    The unit runs with ``ProtectHome=read-only``, so uv's default cache and
    managed-Python dirs under ``~`` are not writable. Point them at
    ``$CACHE_DIR/uv`` (already in ``ReadWritePaths``) unless the operator set
    them explicitly. ``VIRTUAL_ENV`` is dropped so uv always targets the
    project's ``.venv`` rather than warning about an active environment.
    """
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    cache_dir = env.get("CACHE_DIR")
    if cache_dir:
        env.setdefault("UV_CACHE_DIR", os.path.join(cache_dir, "uv", "cache"))
        env.setdefault("UV_PYTHON_INSTALL_DIR", os.path.join(cache_dir, "uv", "python"))
    return env


def sync_dependencies(repo_root: Path) -> SyncResult:
    """Bring the project venv in line with `uv.lock` via `uv sync --frozen`.

    Args:
        repo_root: Path to the project checkout (contains pyproject.toml).

    Returns:
        SyncResult with `ok` False when uv is missing or exits non-zero; `output`
        is uv's combined output (it reports installs on stderr). Never raises.
    """
    try:
        completed = _run(UV_SYNC_ARGV, timeout=UV_SYNC_TIMEOUT_SECONDS, cwd=repo_root, env=_uv_env())
    except subprocess.TimeoutExpired as exc:
        # An interrupted install can leave the venv partly updated; the next tick
        # re-runs `uv sync` unconditionally and repairs it.
        return SyncResult(ok=False, output=f"uv sync timed out after {exc.timeout}s")
    except OSError as exc:
        return SyncResult(ok=False, output=f"uv sync failed to spawn: {exc}")

    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0:
        return SyncResult(ok=False, output=output or f"uv sync exited {completed.returncode}")
    return SyncResult(ok=True, output=output)

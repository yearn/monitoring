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
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Mirrors deploy/install.sh: the production venv carries the `ai` extra (LLM clients).
UV_SYNC_ARGV: list[str] = ["uv", "sync", "--frozen", "--extra", "ai"]


@dataclass
class SyncResult:
    """Outcome of a forced sync to origin/main."""

    ok: bool
    output: str


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

    try:
        fetch = subprocess.run(
            ["git", "-C", str(repo_root), "fetch", "--quiet", "origin", "main"],
            capture_output=True,
            text=True,
            check=False,
        )
        if fetch.returncode != 0:
            output = (fetch.stdout + fetch.stderr).strip()
            return SyncResult(ok=False, output=output or f"fetch exited {fetch.returncode}")

        reset = subprocess.run(
            ["git", "-C", str(repo_root), "reset", "--hard", "--quiet", "origin/main"],
            capture_output=True,
            text=True,
            check=False,
        )
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
        completed = subprocess.run(
            UV_SYNC_ARGV,
            cwd=repo_root,
            env=_uv_env(),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return SyncResult(ok=False, output=f"uv sync failed to spawn: {exc}")

    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0:
        return SyncResult(ok=False, output=output or f"uv sync exited {completed.returncode}")
    return SyncResult(ok=True, output=output)

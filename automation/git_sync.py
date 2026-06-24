"""Force the live VPS checkout to match origin/main before a profile runs.

Deliberately minimal: fetch ``origin/main`` and hard-reset the checkout to it.
The monitoring app is pure read-only — it never commits or writes anything back
into the checkout — so local tracked-file drift is disposable. If an operator
hot-edits a tracked file or the local branch diverges, the next sync discards it
in favor of the reviewed remote ``main`` branch. The worst case of a failed sync
is that we run slightly older read-only code, which is harmless. Callers
therefore log and carry on rather than skipping the run.

Anchored on the most frequent profile (`multisig`, every 10 min via
`sync_before_run` in jobs.yaml): one sync there keeps the whole tree current for
every other profile, since supercronic re-spawns each profile fresh against
whatever is on disk.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


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

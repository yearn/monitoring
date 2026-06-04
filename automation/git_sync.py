"""Fast-forward the live VPS checkout to origin before a profile runs.

Deliberately minimal: a single `git pull --ff-only` wrapper, no locking and no
divergence handling. The monitoring app is pure read-only — it never commits or
writes anything back into the checkout — so there is nothing for a pull to race
against. `--ff-only` refuses to merge or clobber, and the worst case of a failed
pull is that we run slightly older read-only code, which is harmless. Callers
therefore log and carry on rather than skipping the run.

Anchored on the most frequent profile (`multisig`, every 10 min via
`sync_before_run` in jobs.yaml): one pull there keeps the whole tree current for
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
    """Outcome of a `git pull --ff-only`."""

    ok: bool
    output: str


def pull_ff_only(repo_root: Path) -> SyncResult:
    """Fast-forward `repo_root` to its upstream branch.

    Args:
        repo_root: Path to the git checkout to update.

    Returns:
        SyncResult with `ok` False when git is missing, the path is not a
        checkout, or the pull is non-fast-forward / fails. Never raises.
    """
    if not (repo_root / ".git").exists():
        return SyncResult(ok=False, output=f"{repo_root} is not a git checkout")

    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "pull", "--ff-only", "--quiet"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return SyncResult(ok=False, output=f"git pull failed to spawn: {exc}")

    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0:
        return SyncResult(ok=False, output=output or f"exit {completed.returncode}")
    return SyncResult(ok=True, output=output)

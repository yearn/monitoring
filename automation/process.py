"""Run a subprocess under a timeout that stops its whole process group gracefully."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

# On timeout a process gets SIGTERM and this long to exit before SIGKILL. git
# removes its lock files on SIGTERM, and Python tasks stop the agents they started.
TERMINATE_GRACE_SECONDS = 10


def run_with_graceful_timeout(
    argv: list[str],
    timeout: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` capturing text output, terminating it gracefully on timeout.

    ``subprocess.run(timeout=...)`` sends SIGKILL to the direct child only, which
    leaves git lock files in place and gives a task no chance to clean up. This
    sends SIGTERM first and only kills after ``TERMINATE_GRACE_SECONDS``. The
    process runs in its own process group and the signals go to the whole group,
    so helpers it spawned (git-remote-https, uv workers) are stopped too.
    Draining their output is bounded as well: a descendant that escaped the group
    and still holds the pipes cannot keep the caller blocked.

    Args:
        argv: Command and arguments.
        timeout: Seconds before the process is terminated.
        cwd: Working directory for the process.
        env: Environment for the process; inherits the current one when None.

    Returns:
        The completed process.

    Raises:
        subprocess.TimeoutExpired: If the process outlived ``timeout``. Carries
            whatever output was captured.
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
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = exc.stdout, exc.stderr
            for sig in (signal.SIGTERM, signal.SIGKILL):
                _signal_group(proc, sig)
                try:
                    stdout, stderr = proc.communicate(timeout=TERMINATE_GRACE_SECONDS)
                    break
                except subprocess.TimeoutExpired as drain_exc:
                    stdout, stderr = drain_exc.stdout, drain_exc.stderr
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
        except BaseException:
            # Ctrl-C only reaches the foreground group, which no longer includes the
            # child, and Popen.__exit__ would otherwise wait for it to finish.
            _signal_group(proc, signal.SIGKILL)
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _signal_group(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Send ``sig`` to the process group led by ``proc``, ignoring an already-empty group."""
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass

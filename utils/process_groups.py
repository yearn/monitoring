"""Track detached child process groups so a terminating script can stop them.

A CLI agent runs as the leader of its own process group, so signals sent to the
script's group don't reach it, and the ``finally`` that normally kills it can't
run while the script is stuck waiting on a worker thread. ``utils.runner`` kills
everything registered here when the script receives SIGTERM.
"""

import os
import signal

_registered_groups: set[int] = set()


def kill_process_group(pgid: int) -> None:
    """SIGKILL every process in ``pgid``; a group that's already gone is fine."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def register_process_group(pgid: int) -> None:
    """Mark ``pgid`` to be killed if this script is terminated."""
    _registered_groups.add(pgid)


def unregister_process_group(pgid: int) -> None:
    """Forget ``pgid`` once it has been cleaned up."""
    _registered_groups.discard(pgid)


def kill_registered_process_groups() -> None:
    """SIGKILL every process group that is still registered."""
    for pgid in list(_registered_groups):
        kill_process_group(pgid)

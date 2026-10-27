"""Cross-process exclusive locks on files in ``CACHE_DIR``.

Two processes that both check-then-write the same cache keys (a scheduled job
and a continuous worker) take the same named lock around the whole
read → alert → write sequence, so they cannot both see a change as new and
both alert it. ``flock`` locks are released by the kernel when the holder
exits, so a crash never leaves a stale lock.
"""

import fcntl
import time
from collections.abc import Iterator
from contextlib import contextmanager

from utils.paths import cache_path


class LockTimeout(Exception):
    """The lock was still held by another process when the timeout ran out."""


@contextmanager
def file_lock(name: str, timeout: float = 120.0, poll_interval: float = 0.1) -> Iterator[None]:
    """Hold an exclusive lock on ``$CACHE_DIR/<name>.lock`` for the ``with`` block.

    Args:
        name: Lock name; callers that must exclude each other use the same name.
        timeout: Seconds to wait for the lock.
        poll_interval: Seconds between attempts while waiting.

    Raises:
        LockTimeout: Another holder kept the lock for longer than ``timeout``.
    """
    path = cache_path(f"{name}.lock")
    with open(path, "a+") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"{path} still locked after {timeout:.0f}s") from None
                time.sleep(poll_interval)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

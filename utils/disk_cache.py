"""File-backed JSON cache with per-entry TTL and LRU eviction.

Stores one small JSON file per key under ``<CACHE_DIR>/<namespace>/``. Built to
lift the previously process-lifetime in-memory caches in
:mod:`utils.source_context` and :mod:`utils.swiss_knife` onto disk now that
monitoring runs on a persistent VPS rather than ephemeral CI runners — the same
verified contract source / address labels were otherwise re-fetched from
Etherscan / Swiss Knife on every cron run.

Positive (found) entries are written with ``ttl=None`` and live until evicted,
since verified source and curated labels are effectively immutable for a given
address. Negative (miss) entries are written with a short TTL (see
:data:`DEFAULT_NEGATIVE_TTL_SECONDS`) so a contract that gets verified — or an
address that later gains a label — is not cached as missing forever.

Concurrency: writes go through a temp file + :func:`os.replace` (atomic on
POSIX), so a reader never observes a half-written entry even when the hourly and
multisig cron profiles overlap. Eviction and reads are best-effort: any
filesystem error degrades to a cache miss rather than raising.

Sizing: each cache is bounded by ``max_entries`` and/or ``max_bytes``. When a
write pushes a namespace over either cap, least-recently-used entries are evicted
until both caps are satisfied. Recency is tracked by file mtime: a write sets it
and a successful :meth:`DiskCache.get` touches it (:func:`os.utime`), so an entry
re-read every cron run is kept even if it was written long ago. TTL is computed
from the stored write time, not mtime, so touching on read never extends a
negative entry's lifetime.
"""

import json
import os
import tempfile
import time
from typing import Any

from utils.cache import cache_path
from utils.logging import get_logger

logger = get_logger("utils.disk_cache")

# Default time-to-live for negative (miss) entries: 1 day. Overridable via env so
# the cadence can be tuned without a code change.
DEFAULT_NEGATIVE_TTL_SECONDS: float = float(os.getenv("CACHE_NEGATIVE_TTL_SECONDS", "86400"))

# Sentinel returned by ``DiskCache.get`` when a key is absent or expired.
# Distinct from a stored value of ``None`` (a cached negative entry).
MISS: Any = object()

_SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _safe_filename(key: str) -> str:
    """Map a cache key to a filesystem-safe filename stem.

    Replaces any character outside ``[A-Za-z0-9._-]`` with ``_``. Callers pass
    already-safe keys (e.g. ``"1-0xabc…"``), so this is a guard rather than a
    collision-resistant hash.
    """
    cleaned = "".join(c if c in _SAFE_CHARS else "_" for c in key)
    return cleaned or "_"


class DiskCache:
    """A namespaced, file-backed JSON cache with TTL and size-bounded eviction."""

    def __init__(
        self,
        namespace: str,
        *,
        max_entries: int | None = None,
        max_bytes: int | None = None,
        negative_ttl: float = DEFAULT_NEGATIVE_TTL_SECONDS,
    ) -> None:
        """Initialise the cache.

        Args:
            namespace: Subdirectory under ``CACHE_DIR`` that holds this cache's files.
            max_entries: Evict oldest entries once the file count exceeds this. None disables.
            max_bytes: Evict oldest entries once total bytes exceed this. None disables.
            negative_ttl: Default TTL (seconds) used by :meth:`set_negative`.
        """
        self.namespace = namespace
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.negative_ttl = negative_ttl

    def _dir(self) -> str:
        # Resolved lazily (not at import) so an env/`CACHE_DIR` change — e.g. the
        # tests' temp-dir redirect — is always honoured.
        return cache_path(self.namespace)

    def _path(self, key: str) -> str:
        return os.path.join(self._dir(), _safe_filename(key) + ".json")

    def get(self, key: str) -> Any:
        """Return the cached value for ``key``, or :data:`MISS` if absent/expired.

        A return value of ``None`` is a genuine cached negative, distinct from
        ``MISS``. Expired negative entries are removed best-effort on read.
        """
        path = self._path(key)
        try:
            with open(path) as f:
                entry = json.load(f)
        except (OSError, json.JSONDecodeError):
            return MISS
        if not isinstance(entry, dict) or "v" not in entry:
            return MISS

        ttl = entry.get("ttl")
        if ttl is not None and (time.time() - float(entry.get("t", 0))) > float(ttl):
            try:
                os.remove(path)
            except OSError:
                pass
            return MISS

        # LRU: bump the file mtime so eviction (which sorts by mtime) treats this
        # as recently used — an entry re-read every cron run survives even if it
        # was written long ago. TTL is unaffected: expiry keys off the JSON "t"
        # field, not mtime, so a read never extends a negative entry's lifetime.
        # Best-effort; a failed touch only means slightly staler eviction order.
        try:
            os.utime(path, None)
        except OSError:
            pass
        return entry["v"]

    def set(self, key: str, value: Any, *, ttl: float | None) -> None:
        """Write ``value`` under ``key`` with an optional ``ttl`` (seconds).

        ``ttl=None`` never expires. Failures are swallowed (best-effort cache).
        """
        directory = self._dir()
        path = self._path(key)
        tmp = ""
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=f".{os.path.basename(path)}.",
                suffix=".tmp",
                dir=directory,
                text=True,
            )
            with os.fdopen(fd, "w") as f:
                json.dump({"v": value, "t": time.time(), "ttl": ttl}, f)
            os.replace(tmp, path)
        except OSError as e:
            logger.debug("disk cache write failed for %s/%s: %s", self.namespace, key, e)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return
        self._evict_if_needed(directory)

    def set_positive(self, key: str, value: Any) -> None:
        """Cache a found value that never expires (immutable per key)."""
        self.set(key, value, ttl=None)

    def set_negative(self, key: str, value: Any = None) -> None:
        """Cache a miss for ``negative_ttl`` seconds so it's retried later."""
        self.set(key, value, ttl=self.negative_ttl)

    def clear(self) -> None:
        """Remove every entry in this namespace (best-effort)."""
        try:
            for entry in os.scandir(self._dir()):
                if entry.name.endswith(".json"):
                    try:
                        os.remove(entry.path)
                    except OSError:
                        pass
        except OSError:
            pass

    def _evict_if_needed(self, directory: str) -> None:
        """Drop least-recently-used (oldest mtime) entries until size caps hold.

        ``get`` refreshes mtime on read, so oldest-mtime is least-recently-used
        rather than merely oldest-written.
        """
        if self.max_entries is None and self.max_bytes is None:
            return
        try:
            items = [(e.path, e.stat()) for e in os.scandir(directory) if e.name.endswith(".json")]
        except OSError:
            return

        count = len(items)
        total_bytes = sum(st.st_size for _, st in items)

        def over_cap() -> bool:
            return (self.max_entries is not None and count > self.max_entries) or (
                self.max_bytes is not None and total_bytes > self.max_bytes
            )

        if not over_cap():
            return

        items.sort(key=lambda item: item[1].st_mtime)  # oldest first
        for path, st in items:
            if not over_cap():
                break
            try:
                os.remove(path)
                count -= 1
                total_bytes -= st.st_size
            except OSError:
                pass

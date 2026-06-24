"""Tests for utils/disk_cache.py.

These rely on the autouse conftest fixture that redirects CACHE_DIR to a
per-test temp dir, so every DiskCache here writes under an isolated location.
"""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from utils.disk_cache import MISS, DiskCache


class TestDiskCacheRoundtrip(unittest.TestCase):
    def test_positive_roundtrip(self) -> None:
        cache = DiskCache(namespace="rt")
        cache.set_positive("k", {"a": 1, "b": ["x", "y"]})
        self.assertEqual(cache.get("k"), {"a": 1, "b": ["x", "y"]})

    def test_absent_key_returns_miss(self) -> None:
        cache = DiskCache(namespace="rt")
        self.assertIs(cache.get("nope"), MISS)

    def test_negative_value_none_is_distinct_from_miss(self) -> None:
        cache = DiskCache(namespace="neg")
        cache.set_negative("k")  # stores value None
        self.assertIsNone(cache.get("k"))  # a cached negative, not MISS
        self.assertIs(cache.get("other"), MISS)

    def test_empty_list_negative_roundtrips(self) -> None:
        cache = DiskCache(namespace="neg")
        cache.set_negative("k", [])
        self.assertEqual(cache.get("k"), [])

    def test_clear_removes_entries(self) -> None:
        cache = DiskCache(namespace="clr")
        cache.set_positive("a", 1)
        cache.set_positive("b", 2)
        cache.clear()
        self.assertIs(cache.get("a"), MISS)
        self.assertIs(cache.get("b"), MISS)


class TestDiskCacheTTL(unittest.TestCase):
    def test_negative_entry_expires(self) -> None:
        cache = DiskCache(namespace="ttl", negative_ttl=10)
        with patch("utils.disk_cache.time.time") as mock_time:
            mock_time.return_value = 1000.0
            cache.set_negative("k")
            mock_time.return_value = 1005.0  # within TTL
            self.assertIsNone(cache.get("k"))
            mock_time.return_value = 1011.0  # past TTL
            self.assertIs(cache.get("k"), MISS)

    def test_positive_entry_never_expires(self) -> None:
        cache = DiskCache(namespace="ttl")
        with patch("utils.disk_cache.time.time") as mock_time:
            mock_time.return_value = 1000.0
            cache.set_positive("k", "v")
            mock_time.return_value = 1000.0 + 10**9  # far future
            self.assertEqual(cache.get("k"), "v")


class TestDiskCacheEviction(unittest.TestCase):
    def test_evicts_to_max_entries_keeping_newest(self) -> None:
        cache = DiskCache(namespace="evict", max_entries=2)
        cache.set_positive("a", 1)
        cache.set_positive("b", 2)
        # Force deterministic mtime ordering (a oldest) regardless of FS resolution.
        os.utime(cache._path("a"), (100, 100))
        os.utime(cache._path("b"), (200, 200))
        cache.set_positive("c", 3)  # fresh mtime; eviction drops the oldest ("a")

        self.assertIs(cache.get("a"), MISS)  # evicted
        self.assertEqual(cache.get("b"), 2)
        self.assertEqual(cache.get("c"), 3)

    def test_read_refreshes_lru_recency(self) -> None:
        # Reading "a" before inserting "c" must keep "a" and evict the unread "b",
        # even though "a" was written first. (Guards against FIFO-by-write-time.)
        cache = DiskCache(namespace="lru", max_entries=2)
        cache.set_positive("a", 1)
        cache.set_positive("b", 2)
        os.utime(cache._path("a"), (100, 100))  # a oldest-written
        os.utime(cache._path("b"), (200, 200))  # b newer
        cache.get("a")  # LRU touch bumps "a" above the unread "b"
        cache.set_positive("c", 3)  # over cap → evict least-recently-used ("b")

        self.assertEqual(cache.get("a"), 1)  # kept: recently read
        self.assertIs(cache.get("b"), MISS)  # evicted: never read, oldest use
        self.assertEqual(cache.get("c"), 3)

    def test_evicts_to_max_bytes(self) -> None:
        big = "x" * 2000
        cache = DiskCache(namespace="bytes", max_bytes=3000)
        cache.set_positive("a", big)
        os.utime(cache._path("a"), (100, 100))  # mark "a" oldest before "b" triggers eviction
        cache.set_positive("b", big)  # two ~2KB entries exceed the 3KB cap → "a" dropped
        self.assertIs(cache.get("a"), MISS)
        self.assertEqual(cache.get("b"), big)


class TestDiskCacheResilience(unittest.TestCase):
    def test_corrupt_file_is_a_miss(self) -> None:
        cache = DiskCache(namespace="corrupt")
        cache.set_positive("k", "v")
        # Overwrite with garbage.
        with open(cache._path("k"), "w") as f:
            f.write("{not json")
        self.assertIs(cache.get("k"), MISS)

    def test_concurrent_same_key_writes_use_independent_temp_files(self) -> None:
        cache = DiskCache(namespace="concurrent")

        def write_value(i: int) -> None:
            cache.set_positive("shared", {"writer": i, "payload": "x" * 10000})

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write_value, range(40)))

        value = cache.get("shared")
        self.assertIsInstance(value, dict)
        self.assertIn(value["writer"], range(40))
        self.assertEqual(value["payload"], "x" * 10000)

        leftovers = [name for name in os.listdir(cache._dir()) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()

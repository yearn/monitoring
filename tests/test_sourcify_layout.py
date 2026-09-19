"""Tests for utils/sourcify_layout.py."""

import unittest
from unittest.mock import patch

from utils.sourcify_layout import _layout_cache, fetch_storage_layout

LAYOUT_PAYLOAD = {
    "match": "match",
    "creationMatch": "match",
    "runtimeMatch": "match",
    "chainId": "1",
    "address": "0xabc",
    "storageLayout": {
        "storage": [{"slot": "0", "offset": 0, "label": "cap", "type": "t_uint256", "astId": 1}],
        "types": {"t_uint256": {"label": "uint256", "encoding": "inplace", "numberOfBytes": "32"}},
    },
}

# What Sourcify returns for an address it knows but has not verified: HTTP 200.
NO_MATCH_PAYLOAD = {"match": None, "creationMatch": None, "runtimeMatch": None, "chainId": "1", "address": "0xabc"}


class TestFetchStorageLayout(unittest.TestCase):
    def setUp(self) -> None:
        _layout_cache.clear()

    def test_returns_layout_on_match(self) -> None:
        with patch("utils.sourcify_layout.fetch_json", return_value=LAYOUT_PAYLOAD):
            layout = fetch_storage_layout(1, "0xABC")
        assert layout is not None
        self.assertEqual(layout.match, "match")
        self.assertEqual(len(layout.storage), 1)

    def test_unverified_match_is_none(self) -> None:
        """`match: null` arrives with HTTP 200 — status alone proves nothing."""
        with patch("utils.sourcify_layout.fetch_json", return_value=NO_MATCH_PAYLOAD):
            self.assertIsNone(fetch_storage_layout(1, "0xABC"))

    def test_malformed_layout_is_none(self) -> None:
        payload = {"match": "match", "storageLayout": {"storage": "not-a-list", "types": {}}}
        with patch("utils.sourcify_layout.fetch_json", return_value=payload):
            self.assertIsNone(fetch_storage_layout(1, "0xABC"))

    def test_layout_without_entries_is_none(self) -> None:
        payload = {"match": "match", "storageLayout": {"storage": [], "types": {}}}
        with patch("utils.sourcify_layout.fetch_json", return_value=payload):
            self.assertIsNone(fetch_storage_layout(1, "0xABC"))

    def test_network_failure_is_none_and_not_cached(self) -> None:
        with patch("utils.sourcify_layout.fetch_json", return_value=None) as mock_fetch:
            self.assertIsNone(fetch_storage_layout(1, "0xABC"))
            self.assertIsNone(fetch_storage_layout(1, "0xABC"))
            self.assertEqual(mock_fetch.call_count, 2, "a transient failure must be retried, not cached")

    def test_positive_result_is_cached(self) -> None:
        with patch("utils.sourcify_layout.fetch_json", return_value=LAYOUT_PAYLOAD) as mock_fetch:
            fetch_storage_layout(1, "0xABC")
            layout = fetch_storage_layout(1, "0xabc")  # case-insensitive key
        self.assertEqual(mock_fetch.call_count, 1)
        assert layout is not None
        self.assertEqual(layout.storage[0]["label"], "cap")

    def test_no_coverage_is_cached_negatively(self) -> None:
        with patch("utils.sourcify_layout.fetch_json", return_value=NO_MATCH_PAYLOAD) as mock_fetch:
            self.assertIsNone(fetch_storage_layout(1, "0xABC"))
            self.assertIsNone(fetch_storage_layout(1, "0xABC"))
        self.assertEqual(mock_fetch.call_count, 1)

    def test_requests_only_the_storage_layout_field(self) -> None:
        with patch("utils.sourcify_layout.fetch_json", return_value=LAYOUT_PAYLOAD) as mock_fetch:
            fetch_storage_layout(8453, "0xABC")
        url, kwargs = mock_fetch.call_args[0][0], mock_fetch.call_args[1]
        self.assertIn("/8453/0xABC", url)
        self.assertEqual(kwargs["params"], {"fields": "storageLayout"})


if __name__ == "__main__":
    unittest.main()

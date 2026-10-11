"""Tests for utils/swiss_knife.py."""

import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import requests

from utils.swiss_knife import fetch_swiss_knife_labels, pick_display_name, reset_cache


class TestFetchSwissKnifeLabels(unittest.TestCase):
    def setUp(self) -> None:
        reset_cache()

    @patch("utils.swiss_knife.fetch_json")
    def test_returns_label_array(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = ["Circle: USDC Token", "circle", "stablecoin"]
        labels = fetch_swiss_knife_labels("0x" + "a0" * 20, 1)
        self.assertEqual(labels, ["Circle: USDC Token", "circle", "stablecoin"])

    @patch("utils.swiss_knife.fetch_json")
    def test_returns_empty_on_dict_error_response(self, mock_fetch: MagicMock) -> None:
        # Swiss Knife returns {"error": "..."} (dict, not list) for unknown addresses.
        mock_fetch.return_value = {"error": "Error fetching data"}
        self.assertEqual(fetch_swiss_knife_labels("0x" + "b0" * 20, 1), [])

    @patch("utils.swiss_knife.fetch_json")
    def test_returns_empty_on_none(self, mock_fetch: MagicMock) -> None:
        # fetch_json returns None on HTTP error or network failure.
        mock_fetch.return_value = None
        self.assertEqual(fetch_swiss_knife_labels("0x" + "c0" * 20, 1), [])

    def test_invalid_address_skips_network(self) -> None:
        # Should short-circuit without any HTTP call.
        with patch("utils.swiss_knife.fetch_json") as mock_fetch:
            self.assertEqual(fetch_swiss_knife_labels("", 1), [])
            self.assertEqual(fetch_swiss_knife_labels("not-hex", 1), [])
            self.assertEqual(fetch_swiss_knife_labels("0xshort", 1), [])
            mock_fetch.assert_not_called()

    @patch("utils.swiss_knife.fetch_json")
    def test_caches_repeat_lookups(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = ["Curve.fi: 3pool"]
        addr = "0x" + "d0" * 20
        fetch_swiss_knife_labels(addr, 1)
        fetch_swiss_knife_labels(addr, 1)
        fetch_swiss_knife_labels(addr, 1)
        self.assertEqual(mock_fetch.call_count, 1)

    @patch("utils.swiss_knife.fetch_json")
    def test_concurrent_same_address_lookup_single_flights(self, mock_fetch: MagicMock) -> None:
        def slow_response(*args: object, **kwargs: object) -> list[str]:
            time.sleep(0.02)
            return ["Curve.fi: 3pool"]

        mock_fetch.side_effect = slow_response
        addr = "0x" + "d0" * 20

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: fetch_swiss_knife_labels(addr, 1), range(8)))

        self.assertTrue(all(result == ["Curve.fi: 3pool"] for result in results))
        self.assertEqual(mock_fetch.call_count, 1)

    @patch("utils.swiss_knife.fetch_json")
    def test_labels_persist_across_process_restart(self, mock_fetch: MagicMock) -> None:
        # Disk cache should serve labels after the in-memory layer is dropped.
        mock_fetch.return_value = ["Curve.fi: 3pool"]
        addr = "0x" + "d0" * 20
        fetch_swiss_knife_labels(addr, 1)
        reset_cache()  # clears in-memory only
        self.assertEqual(fetch_swiss_knife_labels(addr, 1), ["Curve.fi: 3pool"])
        self.assertEqual(mock_fetch.call_count, 1)  # served from disk

    @patch("utils.swiss_knife.fetch_json")
    def test_empty_negative_persists_across_process_restart(self, mock_fetch: MagicMock) -> None:
        # An unknown address (dict error body = a real 200 response) is cached as
        # an empty negative so we don't re-query it every run.
        mock_fetch.return_value = {"error": "Error fetching data"}
        addr = "0x" + "e0" * 20
        self.assertEqual(fetch_swiss_knife_labels(addr, 1), [])
        reset_cache()
        self.assertEqual(fetch_swiss_knife_labels(addr, 1), [])
        self.assertEqual(mock_fetch.call_count, 1)  # negative cached on disk

    @patch("utils.swiss_knife.fetch_json")
    def test_transient_error_is_not_persisted(self, mock_fetch: MagicMock) -> None:
        # Only service backoff persists; the address can gain a label immediately
        # after it expires, including in the same process.
        with patch("utils.swiss_knife.time.time", return_value=1000) as mock_time:
            mock_fetch.return_value = None
            addr = "0x" + "f0" * 20
            self.assertEqual(fetch_swiss_knife_labels(addr, 1), [])
            mock_time.return_value = 1301
            mock_fetch.return_value = ["Aave: Pool"]
            self.assertEqual(fetch_swiss_knife_labels(addr, 1), ["Aave: Pool"])
            self.assertEqual(mock_fetch.call_count, 2)

    @patch("utils.http_client.requests.request")
    def test_http_400_backs_off_other_addresses_and_survives_restart(self, mock_request: MagicMock) -> None:
        response = requests.Response()
        response.status_code = 400
        response.url = "https://swiss-knife.xyz/api/labels/unknown"
        response._content = (
            b'{"error":"Error fetching data","walletLabelsError":{"message":"Request failed with status code 404"}}'
        )
        mock_request.return_value = response

        with (
            patch("utils.swiss_knife.time.time", return_value=1000) as mock_time,
            self.assertNoLogs("utils.http_client", level="ERROR"),
            self.assertLogs("utils.swiss_knife", level="WARNING") as logs,
        ):
            self.assertEqual(fetch_swiss_knife_labels("0x" + "a1" * 20, 1), [])
            self.assertEqual(fetch_swiss_knife_labels("0x" + "b1" * 20, 1), [])
            reset_cache()
            self.assertEqual(fetch_swiss_knife_labels("0x" + "c1" * 20, 1), [])
            self.assertEqual(mock_request.call_count, 1)
            self.assertEqual(len(logs.output), 1)

            mock_time.return_value = 1301
            response.status_code = 200
            response._content = b'["Aave: Pool"]'
            self.assertEqual(fetch_swiss_knife_labels("0x" + "a1" * 20, 1), ["Aave: Pool"])
            self.assertEqual(mock_request.call_count, 2)

    @patch("utils.swiss_knife.fetch_json")
    def test_backoff_keeps_cached_labels_and_other_chains_available(self, mock_fetch: MagicMock) -> None:
        cached_address = "0x" + "a2" * 20
        mock_fetch.return_value = ["Aave: Pool"]
        fetch_swiss_knife_labels(cached_address, 1)
        mock_fetch.return_value = None
        fetch_swiss_knife_labels("0x" + "b2" * 20, 1)
        reset_cache()
        self.assertEqual(fetch_swiss_knife_labels(cached_address, 1), ["Aave: Pool"])
        mock_fetch.return_value = ["Arbitrum: Pool"]
        self.assertEqual(fetch_swiss_knife_labels("0x" + "c2" * 20, 42161), ["Arbitrum: Pool"])
        self.assertEqual(mock_fetch.call_count, 3)


class TestPickDisplayName(unittest.TestCase):
    """Sanity-check that we only use Swiss Knife's first label when it looks like a name."""

    def test_accepts_name_colon_description(self) -> None:
        self.assertEqual(pick_display_name(["Circle: USDC Token", "circle", "stablecoin"]), "Circle: USDC Token")

    def test_accepts_name_with_space(self) -> None:
        self.assertEqual(pick_display_name(["Uniswap V3 Router", "uniswap", "dex"]), "Uniswap V3 Router")

    def test_accepts_ens_style_name(self) -> None:
        self.assertEqual(pick_display_name(["vitalik.eth"]), "vitalik.eth")

    def test_accepts_capitalized_single_word(self) -> None:
        self.assertEqual(pick_display_name(["WETH"]), "WETH")

    def test_rejects_bare_lowercase_tag(self) -> None:
        # API sometimes returns just ["stablecoin"] — that's a tag, not a name.
        self.assertEqual(pick_display_name(["stablecoin"]), "")

    def test_empty_input(self) -> None:
        self.assertEqual(pick_display_name([]), "")


if __name__ == "__main__":
    unittest.main()

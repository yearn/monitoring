"""Tests for utils/swiss_knife.py."""

import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import requests

from utils.swiss_knife import fetch_swiss_knife_labels, pick_display_name, reset_cache


def _response(data: object, status: int = 200) -> requests.Response:
    """Build a real HTTP response for label client tests."""
    response = requests.Response()
    response.status_code = status
    response.url = "https://swiss-knife.xyz/api/labels/test"
    response._content = json.dumps(data).encode()
    return response


class TestFetchSwissKnifeLabels(unittest.TestCase):
    def setUp(self) -> None:
        reset_cache()

    @patch("utils.swiss_knife.request_with_retry")
    def test_returns_label_array(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = _response(["Circle: USDC Token", "circle", "stablecoin"])
        labels = fetch_swiss_knife_labels("0x" + "a0" * 20, 1)
        self.assertEqual(labels, ["Circle: USDC Token", "circle", "stablecoin"])

    def test_invalid_address_skips_network(self) -> None:
        # Should short-circuit without any HTTP call.
        with patch("utils.swiss_knife.request_with_retry") as mock_fetch:
            self.assertEqual(fetch_swiss_knife_labels("", 1), [])
            self.assertEqual(fetch_swiss_knife_labels("not-hex", 1), [])
            self.assertEqual(fetch_swiss_knife_labels("0xshort", 1), [])
            mock_fetch.assert_not_called()

    @patch("utils.swiss_knife.request_with_retry")
    def test_concurrent_same_address_lookup_single_flights(self, mock_fetch: MagicMock) -> None:
        def slow_response(*args: object, **kwargs: object) -> requests.Response:
            time.sleep(0.02)
            return _response(["Curve.fi: 3pool"])

        mock_fetch.side_effect = slow_response
        addr = "0x" + "d0" * 20

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: fetch_swiss_knife_labels(addr, 1), range(8)))

        self.assertTrue(all(result == ["Curve.fi: 3pool"] for result in results))
        self.assertEqual(mock_fetch.call_count, 1)

    @patch("utils.swiss_knife.request_with_retry")
    def test_labels_persist_across_process_restart(self, mock_fetch: MagicMock) -> None:
        # Disk cache should serve labels after the in-memory layer is dropped.
        mock_fetch.return_value = _response(["Curve.fi: 3pool"])
        addr = "0x" + "d0" * 20
        fetch_swiss_knife_labels(addr, 1)
        reset_cache()  # clears in-memory only
        self.assertEqual(fetch_swiss_knife_labels(addr, 1), ["Curve.fi: 3pool"])
        self.assertEqual(mock_fetch.call_count, 1)  # served from disk

    @patch("utils.swiss_knife.request_with_retry")
    def test_empty_negative_persists_across_process_restart(self, mock_fetch: MagicMock) -> None:
        # An unknown address (dict error body = a real 200 response) is cached as
        # an empty negative so we don't re-query it every run.
        mock_fetch.return_value = _response({"error": "Error fetching data"})
        addr = "0x" + "e0" * 20
        self.assertEqual(fetch_swiss_knife_labels(addr, 1), [])
        reset_cache()
        self.assertEqual(fetch_swiss_knife_labels(addr, 1), [])
        self.assertEqual(mock_fetch.call_count, 1)  # negative cached on disk

    @patch("utils.http_client.requests.request")
    def test_http_503_backs_off_other_addresses_and_survives_restart(self, mock_request: MagicMock) -> None:
        response = _response({"error": "service unavailable"}, 503)
        mock_request.return_value = response

        with (
            patch("utils.swiss_knife.time.time", return_value=1000) as mock_time,
            patch("utils.http_client.Config.get_retry_count", return_value=0),
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

    @patch("utils.swiss_knife.request_with_retry")
    def test_backoff_keeps_cached_labels_and_other_chains_available(self, mock_fetch: MagicMock) -> None:
        cached_address = "0x" + "a2" * 20
        mock_fetch.return_value = _response(["Aave: Pool"])
        fetch_swiss_knife_labels(cached_address, 1)
        mock_fetch.side_effect = requests.ConnectionError("connection reset")
        fetch_swiss_knife_labels("0x" + "b2" * 20, 1)
        reset_cache()
        self.assertEqual(fetch_swiss_knife_labels(cached_address, 1), ["Aave: Pool"])
        mock_fetch.side_effect = None
        mock_fetch.return_value = _response(["Arbitrum: Pool"])
        self.assertEqual(fetch_swiss_knife_labels("0x" + "c2" * 20, 42161), ["Arbitrum: Pool"])
        self.assertEqual(mock_fetch.call_count, 3)

    @patch("utils.http_client.requests.request")
    def test_http_400_does_not_suppress_another_address_or_persist_a_miss(self, mock_request: MagicMock) -> None:
        missing = "0x" + "a3" * 20
        known = "0x" + "b3" * 20
        mock_request.side_effect = [
            _response(
                {
                    "error": "Error fetching data",
                    "walletLabelsError": {"message": "Request failed with status code 404"},
                },
                400,
            ),
            _response(["Circle: USDC Token"]),
            _response(["New Label"]),
        ]

        with self.assertNoLogs("utils.http_client", level="ERROR"):
            self.assertEqual(fetch_swiss_knife_labels(missing, 1), [])
            self.assertEqual(fetch_swiss_knife_labels(known, 1), ["Circle: USDC Token"])
            reset_cache()
            self.assertEqual(fetch_swiss_knife_labels(missing, 1), ["New Label"])
        self.assertEqual(mock_request.call_count, 3)

    @patch("utils.http_client.requests.request")
    def test_rate_limit_and_network_failures_skip_later_lookups(self, mock_request: MagicMock) -> None:
        # HTTP 400 and 503 are covered by the lifecycle tests above.
        failures = [429, requests.Timeout("timeout"), requests.ConnectionError("reset")]
        for chain_id, failure in enumerate(failures, start=1):
            with self.subTest(failure=failure):
                mock_request.reset_mock()
                if isinstance(failure, int):
                    mock_request.side_effect = None
                    mock_request.return_value = _response({"error": "failed"}, failure)
                else:
                    mock_request.side_effect = failure
                with patch("utils.http_client.Config.get_retry_count", return_value=0):
                    self.assertEqual(fetch_swiss_knife_labels("0x" + "d3" * 20, chain_id), [])
                    self.assertEqual(fetch_swiss_knife_labels("0x" + "e3" * 20, chain_id), [])
                self.assertEqual(mock_request.call_count, 1)

    @patch("utils.http_client.requests.request")
    def test_null_or_malformed_json_does_not_suppress_other_addresses(self, mock_request: MagicMock) -> None:
        for chain_id, body in enumerate([b"null", b"not JSON"], start=1):
            with self.subTest(body=body):
                invalid = _response(None)
                invalid._content = body
                mock_request.reset_mock()
                mock_request.side_effect = [invalid, _response(["Aave: Pool"])]
                self.assertEqual(fetch_swiss_knife_labels("0x" + "e3" * 20, chain_id), [])
                self.assertEqual(fetch_swiss_knife_labels("0x" + "f3" * 20, chain_id), ["Aave: Pool"])
                self.assertEqual(mock_request.call_count, 2)


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

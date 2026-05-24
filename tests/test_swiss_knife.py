"""Tests for utils/swiss_knife.py."""

import unittest
from unittest.mock import patch

from utils.swiss_knife import fetch_swiss_knife_labels, pick_display_name, reset_cache


class TestFetchSwissKnifeLabels(unittest.TestCase):
    def setUp(self) -> None:
        reset_cache()

    @patch("utils.swiss_knife.fetch_json")
    def test_returns_label_array(self, mock_fetch: object) -> None:
        mock_fetch.return_value = ["Circle: USDC Token", "circle", "stablecoin"]  # type: ignore[attr-defined]
        labels = fetch_swiss_knife_labels("0x" + "a0" * 20, 1)
        self.assertEqual(labels, ["Circle: USDC Token", "circle", "stablecoin"])

    @patch("utils.swiss_knife.fetch_json")
    def test_returns_empty_on_dict_error_response(self, mock_fetch: object) -> None:
        # Swiss Knife returns {"error": "..."} (dict, not list) for unknown addresses.
        mock_fetch.return_value = {"error": "Error fetching data"}  # type: ignore[attr-defined]
        self.assertEqual(fetch_swiss_knife_labels("0x" + "b0" * 20, 1), [])

    @patch("utils.swiss_knife.fetch_json")
    def test_returns_empty_on_none(self, mock_fetch: object) -> None:
        # fetch_json returns None on HTTP error or network failure.
        mock_fetch.return_value = None  # type: ignore[attr-defined]
        self.assertEqual(fetch_swiss_knife_labels("0x" + "c0" * 20, 1), [])

    def test_invalid_address_skips_network(self) -> None:
        # Should short-circuit without any HTTP call.
        with patch("utils.swiss_knife.fetch_json") as mock_fetch:
            self.assertEqual(fetch_swiss_knife_labels("", 1), [])
            self.assertEqual(fetch_swiss_knife_labels("not-hex", 1), [])
            self.assertEqual(fetch_swiss_knife_labels("0xshort", 1), [])
            mock_fetch.assert_not_called()

    @patch("utils.swiss_knife.fetch_json")
    def test_caches_repeat_lookups(self, mock_fetch: object) -> None:
        mock_fetch.return_value = ["Curve.fi: 3pool"]  # type: ignore[attr-defined]
        addr = "0x" + "d0" * 20
        fetch_swiss_knife_labels(addr, 1)
        fetch_swiss_knife_labels(addr, 1)
        fetch_swiss_knife_labels(addr, 1)
        self.assertEqual(mock_fetch.call_count, 1)  # type: ignore[attr-defined]


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

"""Tests for utils."""

import importlib
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from utils.config import Config, ProtocolConfig


class TestConfig(unittest.TestCase):
    """Tests for the Config class."""

    def test_get_env(self):
        with patch.dict(os.environ, {"TEST_VAR": "test_value"}):
            self.assertEqual(Config.get_env("TEST_VAR"), "test_value")
            self.assertEqual(Config.get_env("NONEXISTENT_VAR", "default"), "default")

    def test_get_env_int(self):
        with patch.dict(os.environ, {"TEST_INT": "42", "TEST_INVALID": "not_an_int"}):
            self.assertEqual(Config.get_env_int("TEST_INT", 0), 42)
            self.assertEqual(Config.get_env_int("NONEXISTENT_VAR", 10), 10)
            self.assertEqual(Config.get_env_int("TEST_INVALID", 10), 10)

    def test_get_env_float(self):
        with patch.dict(os.environ, {"TEST_FLOAT": "3.14", "TEST_INVALID": "not_a_float"}):
            self.assertAlmostEqual(Config.get_env_float("TEST_FLOAT", 0.0), 3.14)
            self.assertAlmostEqual(Config.get_env_float("NONEXISTENT_VAR", 2.71), 2.71)
            self.assertAlmostEqual(Config.get_env_float("TEST_INVALID", 2.71), 2.71)

    def test_get_env_bool(self):
        with patch.dict(
            os.environ,
            {
                "TEST_TRUE1": "true",
                "TEST_TRUE2": "yes",
                "TEST_TRUE3": "1",
                "TEST_FALSE": "false",
            },
        ):
            self.assertTrue(Config.get_env_bool("TEST_TRUE1", False))
            self.assertTrue(Config.get_env_bool("TEST_TRUE2", False))
            self.assertTrue(Config.get_env_bool("TEST_TRUE3", False))
            self.assertFalse(Config.get_env_bool("TEST_FALSE", True))
            self.assertTrue(Config.get_env_bool("NONEXISTENT_VAR", True))

    def test_get_protocol_config(self):
        with patch.dict(
            os.environ,
            {
                "AAVE_ALERT_THRESHOLD": "0.96",
                "AAVE_CRITICAL_THRESHOLD": "0.99",
                "AAVE_ENABLE_NOTIFICATIONS": "false",
            },
        ):
            config = Config.get_protocol_config("aave")
            self.assertIsInstance(config, ProtocolConfig)
            self.assertEqual(config.name, "aave")
            self.assertAlmostEqual(config.alert_threshold, 0.96)
            self.assertAlmostEqual(config.critical_threshold, 0.99)
            self.assertFalse(config.enable_notifications)


class TestDefiLlama(unittest.TestCase):
    """Tests for the DeFiLlama stablecoin price helper."""

    @patch("utils.defillama.request_with_retry")
    def test_fetch_prices_uses_retrying_http_client(self, mock_request):
        from decimal import Decimal

        from utils.defillama import CURRENT_PRICES_URL, fetch_prices

        response = MagicMock()
        response.json.return_value = {
            "coins": {
                "ethereum:0xtoken": {"price": 1.01},
                "ethereum:0xmissing": {"symbol": "MISSING"},
            }
        }
        mock_request.return_value = response

        prices = fetch_prices(["ethereum:0xtoken", "ethereum:0xmissing"])

        self.assertEqual(prices, {"ethereum:0xtoken": Decimal("1.01")})
        mock_request.assert_called_once_with(
            "get",
            f"{CURRENT_PRICES_URL}/ethereum:0xtoken,ethereum:0xmissing",
            headers={"Accept": "application/json"},
        )

    @patch("utils.defillama.request_with_retry", side_effect=RuntimeError("upstream timeout"))
    def test_fetch_prices_raises_on_api_error(self, _mock_request):
        from utils.defillama import fetch_prices

        with self.assertRaises(RuntimeError):
            fetch_prices(["ethereum:0xtoken"])

    @patch("utils.defillama.request_with_retry")
    def test_fetch_prices_skips_request_for_empty_input(self, mock_request):
        from utils.defillama import fetch_prices

        self.assertEqual(fetch_prices([]), {})
        mock_request.assert_not_called()


class TestUstbCachePath(unittest.TestCase):
    """Tests for USTB cache path handling under the hardened service."""

    def test_ustb_cache_file_respects_cache_dir(self):
        for module_name in ("protocols.ustb.main", "utils.cache"):
            sys.modules.pop(module_name, None)

        with patch.dict(os.environ, {"CACHE_DIR": "/srv/cache"}):
            ustb_main = importlib.import_module("protocols.ustb.main")

        try:
            self.assertEqual(ustb_main.CACHE_FILE, "/srv/cache/cache-id.txt")
        finally:
            for module_name in ("protocols.ustb.main", "utils.cache"):
                sys.modules.pop(module_name, None)

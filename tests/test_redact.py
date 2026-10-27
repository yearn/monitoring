"""Tests for utils/redact.py."""

import os
import unittest
from unittest.mock import patch

from utils.redact import redact_secrets, safe_url


class TestRedactSecrets(unittest.TestCase):
    def test_configured_provider_url_keeps_host_only(self):
        url = "https://eth-mainnet.g.alchemy.com/v2/abcDEF1234567890xyz"
        with patch.dict(os.environ, {"PROVIDER_URL_MAINNET": url}, clear=True):
            text = redact_secrets(f"All providers failed. Errors:\n{url}: 429 Client Error for url: {url}")

        self.assertNotIn("abcDEF1234567890xyz", text)
        self.assertIn("https://eth-mainnet.g.alchemy.com/***", text)

    def test_configured_secret_env_values_are_masked(self):
        env = {
            "ETHERSCAN_TOKEN": "ETHERSCANSECRET123",
            "SAFE_API_KEY_2": "safe-key-second-0001",
            "LIQUIDITY_WEBHOOK_SECRET": "webhooksecretvalue",
        }
        with patch.dict(os.environ, env, clear=True):
            text = redact_secrets("ETHERSCANSECRET123 safe-key-second-0001 webhooksecretvalue")

        self.assertEqual(text, "*** *** ***")

    def test_pipe_separated_token_list_masks_each_token(self):
        with patch.dict(os.environ, {"ALERTS_API_TOKENS": "a" * 64 + "|" + "b" * 64}, clear=True):
            text = redact_secrets(f"Bearer {'b' * 64}")

        self.assertEqual(text, "Bearer ***")

    def test_short_and_non_secret_env_values_are_left_alone(self):
        env = {"TELEGRAM_CHAT_ID_AAVE": "-1001234567890", "LOG_LEVEL": "INFO", "SHORT_KEY": "abc"}
        with patch.dict(os.environ, env, clear=True):
            text = redact_secrets("chat -1001234567890 level INFO abc")

        self.assertEqual(text, "chat -1001234567890 level INFO abc")

    def test_patterns_catch_unconfigured_credentials(self):
        cases = {
            "https://api.telegram.org/bot123456:AAH-secret_token/sendMessage": "bot***",
            "https://api.etherscan.io/v2/api?chainid=1&apikey=ABCDEF123456": "apikey=***",
            "https://mainnet.infura.io/v3/0123456789abcdef0123456789abcdef": "/v3/***",
            "https://x.quiknode.pro/0123456789abcdef0123/": "quiknode.pro/***",
        }
        with patch.dict(os.environ, {}, clear=True):
            for raw, marker in cases.items():
                with self.subTest(raw=raw):
                    redacted = redact_secrets(raw)
                    self.assertIn(marker, redacted)
                    self.assertNotIn("0123456789abcdef0123", redacted)
                    self.assertNotIn("ABCDEF123456", redacted)
                    self.assertNotIn("secret_token", redacted)

    def test_addresses_and_explorer_links_survive(self):
        text = (
            "token=0x4c9EDD5852cd905f086C759E8383e09bff1E68B3 "
            "[tx](https://etherscan.io/tx/0x9f2c4d1e8b7a6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d)"
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(redact_secrets(text), text)


class TestSafeUrl(unittest.TestCase):
    def test_strips_path_and_query(self):
        self.assertEqual(safe_url("https://eth-mainnet.g.alchemy.com/v2/key"), "https://eth-mainnet.g.alchemy.com/***")
        self.assertEqual(safe_url("https://rpc.example/?key=1"), "https://rpc.example/***")

    def test_keeps_bare_public_rpc(self):
        self.assertEqual(safe_url("https://mainnet.base.org"), "https://mainnet.base.org")


if __name__ == "__main__":
    unittest.main()

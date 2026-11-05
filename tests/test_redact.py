"""Tests for utils/redact.py."""

import os
import unittest
from unittest.mock import patch

import requests

from utils.redact import REDACTED, redact_secrets, safe_url
from utils.telegram import escape_markdown


class TestRedactSecrets(unittest.TestCase):
    def test_configured_provider_url_keeps_host_only(self):
        url = "https://eth-mainnet.g.alchemy.com/v2/abcDEF1234567890xyz"
        with patch.dict(os.environ, {"PROVIDER_URL_MAINNET": url}, clear=True):
            text = redact_secrets(f"All providers failed. Errors:\n{url}: 429 Client Error for url: {url}")

        self.assertNotIn("abcDEF1234567890xyz", text)
        self.assertIn("https://eth-mainnet.g.alchemy.com/REDACTED", text)

    def test_connection_error_hides_configured_provider_path(self):
        key = "rpc-secret-1234567890"
        url = f"https://eth-mainnet.g.alchemy.com/v2/{key}"
        with (
            patch.dict(os.environ, {"PROVIDER_URL_MAINNET": url}, clear=True),
            patch("urllib3.util.connection.create_connection", side_effect=OSError("connection failed")),
            requests.Session() as session,
        ):
            session.trust_env = False
            with self.assertRaises(requests.ConnectionError) as ctx:
                session.get(url, timeout=1)
            self.assertIn(f"/v2/{key}", str(ctx.exception))
            self.assertNotIn(key, redact_secrets(str(ctx.exception)))

    def test_configured_provider_relative_paths_are_masked(self):
        for path in (
            "/v3/rpc-secret-1234567890",
            "/custom/rpc-secret-1234567890/",
            "/?credential=rpc-secret-1234567890",
        ):
            with (
                self.subTest(path=path),
                patch.dict(os.environ, {"PROVIDER_URL_MAINNET": f"https://rpc.example{path}"}, clear=True),
            ):
                text = redact_secrets(f"Max retries exceeded with url: {path} (connection failed)")
                self.assertNotIn("rpc-secret-1234567890", text)

    def test_public_provider_paths_preserve_explorer_links(self):
        for path in ("/", "/eth", "/rpc", "/ethereum"):
            with (
                self.subTest(path=path),
                patch.dict(os.environ, {"PROVIDER_URL_MAINNET": f"https://rpc.example{path}"}, clear=True),
            ):
                text = "See https://etherscan.io/address/0x123 or https://ethereum.org or /tmp/monitor.log"
                self.assertEqual(redact_secrets(text), text)

    def test_configured_secret_env_values_are_masked(self):
        env = {
            "ETHERSCAN_TOKEN": "ETHERSCANSECRET123",
            "SAFE_API_KEY_2": "safe-key-second-0001",
            "LIQUIDITY_WEBHOOK_SECRET": "webhooksecretvalue",
        }
        with patch.dict(os.environ, env, clear=True):
            text = redact_secrets("ETHERSCANSECRET123 safe-key-second-0001 webhooksecretvalue")

        self.assertEqual(text, "REDACTED REDACTED REDACTED")

    def test_pipe_separated_token_list_masks_each_token(self):
        with patch.dict(os.environ, {"ALERTS_API_TOKENS": "a" * 64 + "|" + "b" * 64}, clear=True):
            text = redact_secrets(f"Bearer {'b' * 64}")

        self.assertEqual(text, "Bearer REDACTED")

    def test_short_and_non_secret_env_values_are_left_alone(self):
        env = {"TELEGRAM_CHAT_ID_AAVE": "-1001234567890", "LOG_LEVEL": "INFO", "SHORT_KEY": "abc"}
        with patch.dict(os.environ, env, clear=True):
            text = redact_secrets("chat -1001234567890 level INFO abc")

        self.assertEqual(text, "chat -1001234567890 level INFO abc")

    def test_patterns_catch_unconfigured_credentials(self):
        cases = {
            "https://api.telegram.org/bot123456:AAH-secret_token/sendMessage": "botREDACTED",
            "https://api.etherscan.io/v2/api?chainid=1&apikey=ABCDEF123456": "apikey=REDACTED",
            "https://mainnet.infura.io/v3/0123456789abcdef0123456789abcdef": "/v3/REDACTED",
            "https://x.quiknode.pro/0123456789abcdef0123/": "quiknode.pro/REDACTED",
        }
        with patch.dict(os.environ, {}, clear=True):
            for raw, marker in cases.items():
                with self.subTest(raw=raw):
                    redacted = redact_secrets(raw)
                    self.assertIn(marker, redacted)
                    self.assertNotIn("0123456789abcdef0123", redacted)
                    self.assertNotIn("ABCDEF123456", redacted)
                    self.assertNotIn("secret_token", redacted)

    def test_markdown_escaped_configured_secrets_are_masked(self):
        url = "https://rpc.example/v2/ab_cdEFgh12_ijklMNop"
        env = {"PROVIDER_URL_MAINNET": url, "SOME_API_KEY": "key_with*marks_123"}
        with patch.dict(os.environ, env, clear=True):
            text = redact_secrets(escape_markdown(f"failed on {url} with key_with*marks_123"))

        self.assertNotIn("cdEFgh12", text)
        self.assertNotIn("marks", text)
        self.assertIn("https://rpc.example/REDACTED", text)

    def test_markdown_escaped_unconfigured_credentials_are_masked(self):
        raw = "https://mainnet.infura.io/v3/ab_cdef0123456789_abcdef https://api.telegram.org/bot123:AA_bc_def/x"
        with patch.dict(os.environ, {}, clear=True):
            text = redact_secrets(escape_markdown(raw))

        self.assertNotIn("cdef0123456789", text)
        self.assertNotIn("bc", text.split("bot")[-1])

    def test_suffixed_secret_names_are_masked(self):
        env = {"TELEGRAM_BOT_TOKEN_DEFAULT": "123456789:AAHdefaulttokenvalue", "OTHER_SECRET_X": "othersecret99"}
        with patch.dict(os.environ, env, clear=True):
            text = redact_secrets("config 123456789:AAHdefaulttokenvalue othersecret99")

        self.assertEqual(text, "config REDACTED REDACTED")

    def test_bare_bot_token_pattern(self):
        token = "1234567890:AAHabcdefghijklmnopqrstuvwxyz0123456"
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(redact_secrets(f"token {token} invalid"), "token REDACTED invalid")

    def test_schemeless_provider_url_is_masked(self):
        url = "eth-mainnet.g.alchemy.com/v2/abcDEF1234567890xyz"
        with patch.dict(os.environ, {"PROVIDER_URL_MAINNET": url}, clear=True):
            text = redact_secrets(f"Invalid URL format: {url}")

        self.assertEqual(text, "Invalid URL format: eth-mainnet.g.alchemy.com/REDACTED")

    def test_marker_is_safe_in_telegram_markdown(self):
        self.assertFalse(set(REDACTED) & set("_*`["))

    def test_addresses_and_explorer_links_survive(self):
        text = (
            "token=0x4c9EDD5852cd905f086C759E8383e09bff1E68B3 "
            "[tx](https://etherscan.io/tx/0x9f2c4d1e8b7a6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d)"
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(redact_secrets(text), text)


class TestSafeUrl(unittest.TestCase):
    def test_strips_path_and_query(self):
        self.assertEqual(
            safe_url("https://eth-mainnet.g.alchemy.com/v2/key"), "https://eth-mainnet.g.alchemy.com/REDACTED"
        )
        self.assertEqual(safe_url("https://rpc.example/?key=1"), "https://rpc.example/REDACTED")

    def test_masks_schemeless_url(self):
        self.assertEqual(safe_url("eth-mainnet.g.alchemy.com/v2/key"), "eth-mainnet.g.alchemy.com/REDACTED")
        self.assertEqual(safe_url("localhost:8545/secret"), "localhost:8545/REDACTED")
        self.assertEqual(safe_url("abcDEF1234567890xyz"), "REDACTED")
        self.assertEqual(safe_url("rpc.example.org"), "rpc.example.org")

    def test_keeps_bare_public_rpc(self):
        self.assertEqual(safe_url("https://mainnet.base.org"), "https://mainnet.base.org")


if __name__ == "__main__":
    unittest.main()

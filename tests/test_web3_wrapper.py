"""Tests for web3_wrapper."""

import os
import unittest
from unittest.mock import patch

from web3 import Web3

from utils.chains import PUBLIC_RPC_URLS, Chain
from utils.web3_wrapper import (
    MAX_BACKOFF_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    MultiHTTPProvider,
    ProviderConnectionError,
    RetryProviders,
    Web3Client,
    retry_with_provider_rotation,
)


class _FakeProvider:
    """Minimal stand-in exposing the attributes retry_with_provider_rotation needs."""

    def __init__(self, side_effects):
        self.provider_urls = ["http://a", "http://b", "http://c", "http://d"]
        self.max_retries = 3
        self.backoff_factor = 2
        self.endpoint_uri = self.provider_urls[0]
        self._side_effects = list(side_effects)
        self.call_count = 0

    def _rotate_provider(self):
        idx = self.provider_urls.index(self.endpoint_uri)
        self.endpoint_uri = self.provider_urls[(idx + 1) % len(self.provider_urls)]

    @retry_with_provider_rotation
    def make_request(self):
        result = self._side_effects[self.call_count]
        self.call_count += 1
        if isinstance(result, Exception):
            raise result
        return result


class TestRetryWithProviderRotation(unittest.TestCase):
    """Tests for the provider-rotation retry decorator in utils.web3_wrapper."""

    def test_revert_fails_fast_without_retry(self):
        """Deterministic reverts must raise immediately, not retry across providers."""
        provider = _FakeProvider([ValueError("execution reverted: 0x")])
        with patch("utils.web3_wrapper.time.sleep") as mock_sleep:
            with self.assertRaises(ValueError):
                provider.make_request()
        self.assertEqual(provider.call_count, 1)
        mock_sleep.assert_not_called()

    def test_revert_marker_is_case_insensitive(self):
        provider = _FakeProvider([RuntimeError("('Execution Reverted', '0x')")])
        with patch("utils.web3_wrapper.time.sleep"):
            with self.assertRaises(RuntimeError):
                provider.make_request()
        self.assertEqual(provider.call_count, 1)

    def test_decode_failure_fails_fast_without_retry(self):
        """Empty/malformed return data (e.g. symbol() on a non-ERC20) is a
        contract-shape mismatch, deterministic across providers, so it must
        raise immediately instead of rotating through every RPC."""
        provider = _FakeProvider(
            [
                ValueError(
                    "Could not decode contract function call to symbol() with return data: b'', output_types: ['string']"
                )
            ]
        )
        with patch("utils.web3_wrapper.time.sleep") as mock_sleep:
            with self.assertRaises(ValueError):
                provider.make_request()
        self.assertEqual(provider.call_count, 1)
        mock_sleep.assert_not_called()

    def test_transient_error_retries_then_succeeds(self):
        provider = _FakeProvider([ConnectionError("boom"), ConnectionError("boom"), "ok"])
        with patch("utils.web3_wrapper.time.sleep"):
            self.assertEqual(provider.make_request(), "ok")
        self.assertEqual(provider.call_count, 3)

    def test_backoff_is_capped(self):
        """Exponential backoff must never exceed MAX_BACKOFF_SECONDS per attempt."""
        # All 12 attempts (3 retries * 4 providers) fail with a transient error.
        provider = _FakeProvider([ConnectionError("boom")] * 12)
        with patch("utils.web3_wrapper.time.sleep") as mock_sleep:
            with self.assertRaises(ProviderConnectionError):
                provider.make_request()
        slept = [call.args[0] for call in mock_sleep.call_args_list]
        self.assertTrue(slept)
        self.assertTrue(all(s <= MAX_BACKOFF_SECONDS for s in slept))

    def test_batch_rpc_error_rotates_underlying_web3_provider(self):
        """Batch-level retries must switch the provider that sends the request."""

        class _FailOnceBatch:
            def __init__(self):
                self.call_count = 0

            def execute(self):
                self.call_count += 1
                if self.call_count == 1:
                    raise RuntimeError("header not found")
                return ["ok"]

        provider_urls = ["https://rpc-a.example", "https://rpc-b.example"]
        provider = MultiHTTPProvider(provider_urls, max_retries=1, backoff_factor=0)
        client = Web3Client.__new__(Web3Client)
        RetryProviders.__init__(client, provider_urls, max_retries=1, backoff_factor=0)
        client.w3 = Web3(provider)
        batch = _FailOnceBatch()

        with patch("utils.web3_wrapper.time.sleep"):
            self.assertEqual(client.execute_batch(batch), ["ok"])

        self.assertEqual(batch.call_count, 2)
        self.assertEqual(provider.endpoint_uri, provider_urls[1])
        self.assertEqual(client.endpoint_uri, provider_urls[1])


class TestDefaultProviderUrls(unittest.TestCase):
    """Public RPC fallbacks used only when no PROVIDER_URL_{CHAIN}* env var is set."""

    def _client(self, chain: Chain) -> Web3Client:
        client = Web3Client.__new__(Web3Client)
        client.chain = chain
        return client

    def _env_without_providers(self) -> dict[str, str]:
        return {key: value for key, value in os.environ.items() if not key.startswith("PROVIDER_URL_")}

    def test_production_mapping_has_hyperevm_public_rpc(self):
        self.assertEqual(PUBLIC_RPC_URLS[Chain.HYPEREVM.chain_id], "https://rpc.hyperliquid.xyz/evm")

    def test_hyperevm_falls_back_to_public_rpc(self):
        with (
            patch.dict(os.environ, self._env_without_providers(), clear=True),
            # conftest blanks the mapping for isolation; restore the production values here.
            patch("utils.web3_wrapper.DEFAULT_PROVIDER_URLS", PUBLIC_RPC_URLS),
        ):
            self.assertEqual(self._client(Chain.HYPEREVM)._get_provider_urls(), ["https://rpc.hyperliquid.xyz/evm"])

    def test_env_provider_disables_public_fallback(self):
        env = {**self._env_without_providers(), "PROVIDER_URL_HYPEREVM": "https://custom.example/evm"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("utils.web3_wrapper.DEFAULT_PROVIDER_URLS", PUBLIC_RPC_URLS),
        ):
            self.assertEqual(self._client(Chain.HYPEREVM)._get_provider_urls(), ["https://custom.example/evm"])

    def test_chain_without_default_still_requires_env(self):
        with (
            patch.dict(os.environ, self._env_without_providers(), clear=True),
            patch("utils.web3_wrapper.DEFAULT_PROVIDER_URLS", PUBLIC_RPC_URLS),
        ):
            with self.assertRaisesRegex(ValueError, "No providers found for chain MAINNET"):
                self._client(Chain.MAINNET)._get_provider_urls()

    def test_http_provider_uses_bounded_timeout(self):
        provider = MultiHTTPProvider(["https://custom.example/evm"])
        self.assertEqual(provider.request_kwargs["timeout"], REQUEST_TIMEOUT_SECONDS)
        self.assertLessEqual(REQUEST_TIMEOUT_SECONDS, 120)

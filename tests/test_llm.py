"""Tests for utils/llm/ package."""

import os
import unittest
from unittest.mock import MagicMock, patch

from utils.llm.base import LLMError, LLMProvider
from utils.llm.factory import _PROVIDER_DEFAULTS, get_llm_provider, reset_provider
from utils.llm.openai_compat import OpenAICompatProvider


class TestLLMProviderBase(unittest.TestCase):
    """Tests for the LLMProvider abstract base class."""

    def test_cannot_instantiate_abstract(self) -> None:
        with self.assertRaises(TypeError):
            LLMProvider()  # type: ignore[abstract]


class TestOpenAICompatProvider(unittest.TestCase):
    """Tests for OpenAICompatProvider."""

    @patch("openai.OpenAI")
    def test_complete_success(self, mock_openai_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        mock_choice = MagicMock()
        mock_choice.message.content = "This transaction updates the collateral factor."
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response

        provider = OpenAICompatProvider(api_key="test-key", base_url="https://api.test.ai/v1", model="test-model")

        result = provider.complete("Explain this tx")

        self.assertEqual(result, "This transaction updates the collateral factor.")
        mock_client.chat.completions.create.assert_called_once_with(
            model="test-model",
            messages=[{"role": "user", "content": "Explain this tx"}],
        )

    @patch("openai.OpenAI")
    def test_complete_empty_response_raises(self, mock_openai_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response

        provider = OpenAICompatProvider(api_key="test-key", base_url="https://api.test.ai/v1", model="test-model")

        with self.assertRaises(LLMError):
            provider.complete("Explain this tx")

    @patch("openai.OpenAI")
    def test_complete_api_error_raises(self, mock_openai_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = Exception("Connection timeout")

        provider = OpenAICompatProvider(api_key="test-key", base_url="https://api.test.ai/v1", model="test-model")

        with self.assertRaises(LLMError) as ctx:
            provider.complete("Explain this tx")
        self.assertIn("Connection timeout", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_model_name_property(self, mock_openai_cls: MagicMock) -> None:
        provider = OpenAICompatProvider(api_key="key", base_url="https://api.test.ai/v1", model="llama-3.3-70b")
        self.assertEqual(provider.model_name, "llama-3.3-70b")

    @patch("openai.OpenAI")
    def test_strips_whitespace_from_response(self, mock_openai_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        mock_choice = MagicMock()
        mock_choice.message.content = "  Some explanation with spaces  \n"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response

        provider = OpenAICompatProvider(api_key="key", base_url="https://api.test.ai/v1", model="model")
        result = provider.complete("prompt")
        self.assertEqual(result, "Some explanation with spaces")


class TestAnthropicProvider(unittest.TestCase):
    """Tests for AnthropicProvider."""

    @patch("anthropic.Anthropic")
    def test_complete_success(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "This pauses the protocol."
        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_client.messages.create.return_value = mock_response

        from utils.llm.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        result = provider.complete("Explain this tx")

        self.assertEqual(result, "This pauses the protocol.")
        mock_client.messages.create.assert_called_once_with(
            model="claude-haiku-4-5-20251001",
            max_tokens=10000,
            messages=[{"role": "user", "content": "Explain this tx"}],
        )

    @patch("anthropic.Anthropic")
    def test_complete_api_error_raises(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = Exception("Rate limit exceeded")

        from utils.llm.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        with self.assertRaises(LLMError) as ctx:
            provider.complete("Explain this tx")
        self.assertIn("Rate limit exceeded", str(ctx.exception))

    @patch("anthropic.Anthropic")
    def test_non_text_block_raises(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client

        mock_block = MagicMock()
        mock_block.type = "tool_use"
        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_client.messages.create.return_value = mock_response

        from utils.llm.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
        with self.assertRaises(LLMError):
            provider.complete("Explain this tx")

    @patch("anthropic.Anthropic")
    def test_model_name_property(self, mock_anthropic_cls: MagicMock) -> None:
        from utils.llm.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(api_key="key", model="claude-haiku-4-5-20251001")
        self.assertEqual(provider.model_name, "claude-haiku-4-5-20251001")


class TestFactory(unittest.TestCase):
    """Tests for the LLM provider factory."""

    def setUp(self) -> None:
        reset_provider()

    def tearDown(self) -> None:
        reset_provider()

    def test_missing_api_key_raises(self) -> None:
        env = {"LLM_PROVIDER": "venice"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(LLMError) as ctx:
                get_llm_provider()
            self.assertIn("LLM_API_KEY", str(ctx.exception))

    @patch("openai.OpenAI")
    def test_venice_defaults(self, mock_openai_cls: MagicMock) -> None:
        env = {"LLM_PROVIDER": "venice", "LLM_API_KEY": "test-key"}
        with patch.dict(os.environ, env, clear=True):
            provider = get_llm_provider()
            self.assertEqual(provider.model_name, "deepseek-v4-flash")

    @patch("openai.OpenAI")
    def test_openai_defaults(self, mock_openai_cls: MagicMock) -> None:
        env = {"LLM_PROVIDER": "openai", "LLM_API_KEY": "sk-test"}
        with patch.dict(os.environ, env, clear=True):
            provider = get_llm_provider()
            self.assertEqual(provider.model_name, "gpt-4o-mini")

    @patch("anthropic.Anthropic")
    def test_anthropic_defaults(self, mock_anthropic_cls: MagicMock) -> None:
        env = {"LLM_PROVIDER": "anthropic", "LLM_API_KEY": "sk-ant-test"}
        with patch.dict(os.environ, env, clear=True):
            provider = get_llm_provider()
            self.assertEqual(provider.model_name, "claude-haiku-4-5-20251001")

    @patch("anthropic.Anthropic")
    def test_anthropic_custom_model(self, mock_anthropic_cls: MagicMock) -> None:
        env = {"LLM_PROVIDER": "anthropic", "LLM_API_KEY": "sk-ant-test", "LLM_MODEL": "claude-sonnet-4-6"}
        with patch.dict(os.environ, env, clear=True):
            provider = get_llm_provider()
            self.assertEqual(provider.model_name, "claude-sonnet-4-6")

    @patch("openai.OpenAI")
    def test_custom_overrides(self, mock_openai_cls: MagicMock) -> None:
        env = {
            "LLM_PROVIDER": "venice",
            "LLM_API_KEY": "test-key",
            "LLM_BASE_URL": "https://custom.api/v1",
            "LLM_MODEL": "custom-model",
        }
        with patch.dict(os.environ, env, clear=True):
            provider = get_llm_provider()
            self.assertEqual(provider.model_name, "custom-model")

    @patch("openai.OpenAI")
    def test_singleton_caching(self, mock_openai_cls: MagicMock) -> None:
        env = {"LLM_PROVIDER": "venice", "LLM_API_KEY": "test-key"}
        with patch.dict(os.environ, env, clear=True):
            p1 = get_llm_provider()
            p2 = get_llm_provider()
            self.assertIs(p1, p2)

    def test_unknown_provider_without_url_raises(self) -> None:
        env = {"LLM_PROVIDER": "unknown-provider", "LLM_API_KEY": "test-key", "LLM_MODEL": "some-model"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(LLMError) as ctx:
                get_llm_provider()
            self.assertIn("LLM_BASE_URL", str(ctx.exception))

    def test_provider_defaults_contain_model(self) -> None:
        for name, defaults in _PROVIDER_DEFAULTS.items():
            self.assertIn("model", defaults, f"Missing model for {name}")


if __name__ == "__main__":
    unittest.main()

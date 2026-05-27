"""Anthropic (Claude) LLM provider.

Uses the native Anthropic API which has a different format from
the OpenAI chat completions API.
"""

from utils.llm.base import LLMError, LLMProvider
from utils.logging import get_logger

logger = get_logger("utils.llm.anthropic_provider")


class AnthropicProvider(LLMProvider):
    """LLM provider for the Anthropic (Claude) API."""

    def __init__(self, api_key: str, model: str) -> None:
        """Initialize the provider.

        Args:
            api_key: Anthropic API key.
            model: Model identifier (e.g. claude-haiku-4-5-20251001).
        """
        try:
            from anthropic import Anthropic
        except ImportError:
            raise LLMError("anthropic package not installed. Install with: uv pip install 'monitoring-scripts-py[ai]'")
        self._model = model
        self._client = Anthropic(api_key=api_key)
        logger.info("Initialized Anthropic provider: model=%s", model)

    def complete(self, prompt: str, system_prompt: str = "") -> str:
        """Generate a completion using the Anthropic messages API.

        The static ``system_prompt`` is sent as a cacheable system block so
        repeated alerts within the 5-minute cache window pay input-token cost
        for the (large) instruction prompt only once.
        """
        kwargs: dict = {
            "model": self._model,
            "max_tokens": 10000,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            kwargs["system"] = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        try:
            response = self._client.messages.create(**kwargs)
            block = response.content[0]
            if block.type != "text":
                raise LLMError(f"Unexpected response block type: {block.type}")
            return block.text.strip()
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"Anthropic API call failed: {e}") from e

    @property
    def model_name(self) -> str:
        """Return the model identifier."""
        return self._model

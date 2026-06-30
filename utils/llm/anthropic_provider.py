"""Anthropic (Claude) LLM provider.

Uses the native Anthropic API which has a different format from
the OpenAI chat completions API.
"""

from typing import Any

from utils.llm.base import LLMError, LLMProvider, wrap_llm_errors
from utils.logger import get_logger

logger = get_logger("utils.llm.anthropic_provider")

# Tool name used to force a schema-constrained response.
_STRUCTURED_TOOL = "emit_explanation"


class AnthropicProvider(LLMProvider):
    """LLM provider for the Anthropic (Claude) API."""

    def __init__(self, api_key: str, model: str, structured_output: bool = True) -> None:
        """Initialize the provider.

        Args:
            api_key: Anthropic API key.
            model: Model identifier (e.g. claude-haiku-4-5-20251001).
            structured_output: Whether to advertise structured output. Defaults
                to True — Claude models support forced tool use, which we use to
                constrain the response to a schema.
        """
        try:
            from anthropic import Anthropic
        except ImportError:
            raise LLMError("anthropic package not installed. Install with: uv pip install 'monitoring-scripts-py[ai]'")
        self._model = model
        self._client = Anthropic(api_key=api_key)
        self._structured_output = structured_output
        logger.info("Initialized Anthropic provider: model=%s structured=%s", model, structured_output)

    def complete(self, prompt: str, system_prompt: str = "") -> str:
        """Generate a completion using the Anthropic messages API.

        The static ``system_prompt`` is sent as a cacheable system block so
        repeated alerts within the 5-minute cache window pay input-token cost
        for the (large) instruction prompt only once.
        """
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 10000,
            "messages": [{"role": "user", "content": prompt}],
        }
        self._add_system(kwargs, system_prompt)
        with wrap_llm_errors("Anthropic API call failed"):
            response = self._client.messages.create(**kwargs)
            block = response.content[0]
            if block.type != "text":
                raise LLMError(f"Unexpected response block type: {block.type}")
            return block.text.strip()

    @property
    def supports_structured_output(self) -> bool:
        """Return whether structured output (forced tool use) is enabled."""
        return self._structured_output

    def complete_structured(self, prompt: str, schema: dict[str, Any], system_prompt: str = "") -> dict[str, Any]:
        """Constrain the response to ``schema`` via forced tool use and return it."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 10000,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {"name": _STRUCTURED_TOOL, "description": "Emit the transaction explanation.", "input_schema": schema}
            ],
            "tool_choice": {"type": "tool", "name": _STRUCTURED_TOOL},
        }
        self._add_system(kwargs, system_prompt)
        with wrap_llm_errors("Anthropic structured call failed"):
            response = self._client.messages.create(**kwargs)
            for block in response.content:
                if block.type == "tool_use":
                    return dict(block.input)
            raise LLMError("Anthropic response contained no tool_use block")

    def _add_system(self, kwargs: dict[str, Any], system_prompt: str) -> None:
        """Attach a cacheable system block to ``kwargs`` when a prompt is given."""
        if system_prompt:
            kwargs["system"] = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]

    @property
    def model_name(self) -> str:
        """Return the model identifier."""
        return self._model

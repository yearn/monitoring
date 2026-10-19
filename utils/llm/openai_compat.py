"""OpenAI-compatible LLM provider.

Works with any provider that implements the OpenAI chat completions API:
- Venice.ai (https://api.venice.ai/api/v1)
- OpenAI (https://api.openai.com/v1)
- Together AI, Groq, Ollama, etc.
"""

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

from utils.llm.base import LLMError, LLMProvider, LLMUsage, wrap_llm_errors
from utils.logger import get_logger

logger = get_logger("utils.llm.openai_compat")


class OpenAICompatProvider(LLMProvider):
    """LLM provider for any OpenAI-compatible API."""

    def __init__(self, api_key: str, base_url: str, model: str, structured_output: bool = False) -> None:
        """Initialize the provider.

        Args:
            api_key: API key for the provider.
            base_url: Base URL for the API (e.g. https://api.venice.ai/api/v1).
            model: Model identifier (e.g. llama-3.3-70b, gpt-4o-mini).
            structured_output: Whether to advertise JSON-schema structured output.
                Off by default because support varies across OpenAI-compatible
                backends (and across models within one backend).
        """
        try:
            from openai import OpenAI
        except ImportError:
            raise LLMError("openai package not installed. Install with: uv pip install 'monitoring-scripts-py[ai]'")
        self._model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._structured_output = structured_output
        logger.info(
            "Initialized OpenAI-compatible provider: base_url=%s model=%s structured=%s",
            base_url,
            model,
            structured_output,
        )

    def complete(self, prompt: str, system_prompt: str = "") -> str:
        """Generate a completion using the OpenAI chat completions API."""
        with wrap_llm_errors("LLM API call failed"):
            start = time.monotonic()
            response = self._client.chat.completions.create(
                model=self._model,
                messages=self._build_messages(prompt, system_prompt),
            )
            self.last_usage = _usage(response, time.monotonic() - start)
            content = response.choices[0].message.content
            if not content:
                raise LLMError("Empty response from LLM")
            return content.strip()

    @property
    def supports_structured_output(self) -> bool:
        """Return whether JSON-schema output was enabled for this provider."""
        return self._structured_output

    def complete_structured(self, prompt: str, schema: dict[str, Any], system_prompt: str = "") -> dict[str, Any]:
        """Request a JSON-schema-constrained response and return it parsed."""
        with wrap_llm_errors("Structured LLM call failed"):
            start = time.monotonic()
            response = self._client.chat.completions.create(
                model=self._model,
                messages=self._build_messages(prompt, system_prompt),
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "explanation", "schema": schema, "strict": True},
                },
            )
            self.last_usage = _usage(response, time.monotonic() - start)
            content = response.choices[0].message.content
            if not content:
                raise LLMError("Empty response from LLM")
            try:
                parsed: dict[str, Any] = json.loads(content)
            except json.JSONDecodeError as e:
                raise LLMError(f"Structured response was not valid JSON: {e}") from e
            return parsed

    def _build_messages(self, prompt: str, system_prompt: str) -> list[ChatCompletionMessageParam]:
        """Assemble chat messages, prepending the system prompt when present."""
        messages: list[ChatCompletionMessageParam] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return messages

    @property
    def model_name(self) -> str:
        """Return the model identifier."""
        return self._model


def _usage(response: Any, latency_s: float) -> LLMUsage:
    """Usage from a chat-completions response; prefix-cache hits come in ``prompt_tokens_details``.

    Venice reports automatic prefix caching as ``prompt_tokens_details.cached_tokens``
    (and ``cache_write_tokens`` where the model bills writes); fields it omits stay None.
    """
    usage = getattr(response, "usage", None)
    details = getattr(usage, "prompt_tokens_details", None)
    return LLMUsage(
        input_tokens=getattr(usage, "prompt_tokens", None),
        cached_input_tokens=getattr(details, "cached_tokens", None),
        cache_write_tokens=getattr(details, "cache_write_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        latency_s=latency_s,
    )

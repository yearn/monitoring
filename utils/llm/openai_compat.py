"""OpenAI-compatible LLM provider.

Works with any provider that implements the OpenAI chat completions API:
- Venice.ai (https://api.venice.ai/api/v1)
- OpenAI (https://api.openai.com/v1)
- Together AI, Groq, Ollama, etc.
"""

import json
from typing import Any

from utils.llm.base import LLMError, LLMProvider
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
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=self._build_messages(prompt, system_prompt),
            )
            content = response.choices[0].message.content
            if not content:
                raise LLMError("Empty response from LLM")
            return content.strip()
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"LLM API call failed: {e}") from e

    @property
    def supports_structured_output(self) -> bool:
        """Return whether JSON-schema output was enabled for this provider."""
        return self._structured_output

    def complete_structured(self, prompt: str, schema: dict[str, Any], system_prompt: str = "") -> dict[str, Any]:
        """Request a JSON-schema-constrained response and return it parsed."""
        try:
            response = self._client.chat.completions.create(  # type: ignore[call-overload]
                model=self._model,
                messages=self._build_messages(prompt, system_prompt),
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "explanation", "schema": schema, "strict": True},
                },
            )
            content = response.choices[0].message.content
            if not content:
                raise LLMError("Empty response from LLM")
            parsed: dict[str, Any] = json.loads(content)
            return parsed
        except LLMError:
            raise
        except json.JSONDecodeError as e:
            raise LLMError(f"Structured response was not valid JSON: {e}") from e
        except Exception as e:
            raise LLMError(f"Structured LLM call failed: {e}") from e

    def _build_messages(self, prompt: str, system_prompt: str) -> list[dict[str, str]]:
        """Assemble chat messages, prepending the system prompt when present."""
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return messages

    @property
    def model_name(self) -> str:
        """Return the model identifier."""
        return self._model

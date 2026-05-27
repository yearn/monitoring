"""OpenAI-compatible LLM provider.

Works with any provider that implements the OpenAI chat completions API:
- Venice.ai (https://api.venice.ai/api/v1)
- OpenAI (https://api.openai.com/v1)
- Together AI, Groq, Ollama, etc.
"""

from utils.llm.base import LLMError, LLMProvider
from utils.logging import get_logger

logger = get_logger("utils.llm.openai_compat")


class OpenAICompatProvider(LLMProvider):
    """LLM provider for any OpenAI-compatible API."""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        """Initialize the provider.

        Args:
            api_key: API key for the provider.
            base_url: Base URL for the API (e.g. https://api.venice.ai/api/v1).
            model: Model identifier (e.g. llama-3.3-70b, gpt-4o-mini).
        """
        try:
            from openai import OpenAI
        except ImportError:
            raise LLMError("openai package not installed. Install with: uv pip install 'monitoring-scripts-py[ai]'")
        self._model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        logger.info("Initialized OpenAI-compatible provider: base_url=%s model=%s", base_url, model)

    def complete(self, prompt: str, system_prompt: str = "") -> str:
        """Generate a completion using the OpenAI chat completions API."""
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
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
    def model_name(self) -> str:
        """Return the model identifier."""
        return self._model

"""Abstract base class for LLM providers."""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class LLMError(Exception):
    """Exception raised for LLM API errors."""


@contextmanager
def wrap_llm_errors(label: str) -> Iterator[None]:
    """Translate any non-``LLMError`` raised inside the block into an ``LLMError``.

    Existing ``LLMError``s (and their messages) pass through untouched; anything
    else becomes ``LLMError(f"{label}: {e}")``. Providers wrap their API calls
    with this so the "re-raise LLMError, wrap everything else" contract lives in
    one place instead of being copy-pasted into every method.

    Args:
        label: Human-readable prefix describing the failed call.
    """
    try:
        yield
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(f"{label}: {e}") from e


class LLMProvider(ABC):
    """Interface for LLM providers used to generate transaction explanations."""

    @abstractmethod
    def complete(self, prompt: str, system_prompt: str = "") -> str:
        """Generate a completion for the given prompt.

        Args:
            prompt: The user prompt (per-transaction context) to send to the LLM.
            system_prompt: Optional system prompt carrying the static instructions
                (brevity rules, output format). Passed via the provider's native
                system role so it can be cached and followed more reliably than
                when inlined into the user message.

        Returns:
            The generated text response.

        Raises:
            LLMError: If the API call fails.
        """

    @property
    def supports_structured_output(self) -> bool:
        """Whether this provider can return JSON matching a supplied schema.

        Defaults to False. Providers that implement :meth:`complete_structured`
        override this; the explainer only takes the structured path when it's
        True, falling back to text parsing otherwise.
        """
        return False

    def complete_structured(self, prompt: str, schema: dict[str, Any], system_prompt: str = "") -> dict[str, Any]:
        """Generate a completion constrained to ``schema`` and return it parsed.

        Args:
            prompt: The user prompt.
            schema: A JSON Schema object the response must conform to.
            system_prompt: Optional system prompt (see :meth:`complete`).

        Returns:
            The decoded JSON object matching ``schema``.

        Raises:
            LLMError: If structured output is unsupported, the call fails, or the
                response can't be parsed. Callers should fall back to
                :meth:`complete` on this error.
        """
        raise LLMError("structured output is not supported by this provider")

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Return the model identifier being used."""

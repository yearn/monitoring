"""Abstract base class for LLM providers."""

from abc import ABC, abstractmethod


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
    @abstractmethod
    def model_name(self) -> str:
        """Return the model identifier being used."""


class LLMError(Exception):
    """Exception raised for LLM API errors."""

"""Provider that tries an ordered list of LLM providers until one succeeds."""

from collections.abc import Callable
from typing import Any, TypeVar

from utils.llm.base import LLMError, LLMProvider, LLMUnavailableError
from utils.logger import get_logger

logger = get_logger("utils.llm.fallback")

T = TypeVar("T")


class FallbackProvider(LLMProvider):
    """Try each provider in order, falling through on ``LLMError``.

    A provider that raises ``LLMUnavailableError`` (no binary, auth/credit
    failure, timeout) is dropped for the rest of the process so a dead CLI
    isn't retried — and waited on — for every stage of every alert. Any other
    ``LLMError`` (bad or empty response) falls through for this call only.
    """

    def __init__(self, providers: list[LLMProvider]) -> None:
        """Initialize the chain.

        Args:
            providers: Providers in priority order. Must not be empty.
        """
        if not providers:
            raise LLMError("FallbackProvider needs at least one provider")
        self._providers = list(providers)
        self._disabled: set[int] = set()
        self._last_used: LLMProvider | None = None
        logger.info("LLM fallback chain: %s", " -> ".join(p.model_name for p in self._providers))

    def complete(self, prompt: str, system_prompt: str = "") -> str:
        """Return the first successful completion in chain order."""
        return self._first_success("complete", lambda p: p.complete(prompt, system_prompt=system_prompt))

    @property
    def supports_structured_output(self) -> bool:
        """True when any still-active provider supports structured output."""
        return any(p.supports_structured_output for p in self._active())

    def complete_structured(self, prompt: str, schema: dict[str, Any], system_prompt: str = "") -> dict[str, Any]:
        """Return the first successful structured completion, skipping providers without support."""
        return self._first_success(
            "complete_structured",
            lambda p: p.complete_structured(prompt, schema, system_prompt=system_prompt),
            only_structured=True,
        )

    def _active(self) -> list[LLMProvider]:
        """Providers not yet disabled, in priority order."""
        return [p for i, p in enumerate(self._providers) if i not in self._disabled]

    def _first_success(self, op: str, call: Callable[[LLMProvider], T], only_structured: bool = False) -> T:
        """Run ``call`` against each active provider until one returns.

        Raises:
            LLMError: If every eligible provider failed.
        """
        errors: list[str] = []
        for index, provider in enumerate(self._providers):
            if index in self._disabled or (only_structured and not provider.supports_structured_output):
                continue
            try:
                result = call(provider)
            except LLMUnavailableError as e:
                self._disabled.add(index)
                logger.warning("%s unavailable, disabling for this run: %s", provider.model_name, e)
                errors.append(f"{provider.model_name}: {e}")
                continue
            except LLMError as e:
                logger.warning("%s %s failed, trying next provider: %s", provider.model_name, op, e)
                errors.append(f"{provider.model_name}: {e}")
                continue
            self._last_used = provider
            return result
        if not errors:
            skipped = "disabled after earlier failures" if self._disabled else "no eligible provider"
            raise LLMError(f"No LLM provider available for {op}: {skipped}")
        raise LLMError(f"All LLM providers failed for {op}: " + "; ".join(errors))

    @property
    def model_name(self) -> str:
        """Model of the provider that last answered, or the chain description before any call."""
        if self._last_used is not None:
            return self._last_used.model_name
        return " -> ".join(p.model_name for p in self._providers)

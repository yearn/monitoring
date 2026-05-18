"""Factory for creating LLM provider instances from environment configuration.

Environment variables:
    LLM_PROVIDER: Provider name (default: "venice").
    LLM_API_KEY: API key for the provider (required).
    LLM_BASE_URL: Base URL for the API (not needed for anthropic).
    LLM_MODEL: Model identifier to use.

Provider defaults:
    venice: base_url=https://api.venice.ai/api/v1, model=deepseek-v4-flash
    groq: base_url=https://api.groq.com/openai/v1, model=openai/gpt-oss-safeguard-20b
    openai: base_url=https://api.openai.com/v1, model=gpt-4o-mini
    anthropic: model=claude-haiku-4-5-20251001 (uses native Anthropic API)
    Custom: Set LLM_BASE_URL and LLM_MODEL explicitly.
"""

import os

from utils.llm.base import LLMError, LLMProvider
from utils.logging import get_logger

logger = get_logger("utils.llm.factory")

# Default configurations per provider name
_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "venice": {
        "base_url": "https://api.venice.ai/api/v1",
        "model": "deepseek-v4-flash",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-safeguard-20b",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "anthropic": {
        "model": "claude-haiku-4-5-20251001",
    },
}

# Cached singleton instance
_instance: LLMProvider | None = None


def _create_provider(provider_name: str, api_key: str, model: str, base_url: str) -> LLMProvider:
    """Create the appropriate provider instance.

    Args:
        provider_name: Provider identifier (anthropic, venice, openai, etc.).
        api_key: API key for the provider.
        model: Model identifier.
        base_url: Base URL (only used for OpenAI-compatible providers).

    Returns:
        Configured LLMProvider instance.
    """
    if provider_name == "anthropic":
        from utils.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=api_key, model=model)

    from utils.llm.openai_compat import OpenAICompatProvider

    if not base_url:
        raise LLMError(
            f"LLM_BASE_URL must be set for provider '{provider_name}'. "
            f"Known providers with defaults: {list(_PROVIDER_DEFAULTS.keys())}"
        )
    return OpenAICompatProvider(api_key=api_key, base_url=base_url, model=model)


def get_llm_provider() -> LLMProvider:
    """Create or return the cached LLM provider based on environment variables.

    Returns:
        Configured LLMProvider instance.

    Raises:
        LLMError: If LLM_API_KEY is not set.
    """
    global _instance
    if _instance is not None:
        return _instance

    provider_name = (os.getenv("LLM_PROVIDER") or "venice").lower()
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        raise LLMError("LLM_API_KEY environment variable is not set")

    defaults = _PROVIDER_DEFAULTS.get(provider_name, {})
    model = os.getenv("LLM_MODEL") or defaults.get("model", "")
    base_url = os.getenv("LLM_BASE_URL") or defaults.get("base_url", "")

    if not model:
        raise LLMError(
            f"LLM_MODEL must be set for provider '{provider_name}'. "
            f"Known providers with defaults: {list(_PROVIDER_DEFAULTS.keys())}"
        )

    logger.info("Creating LLM provider: %s (model=%s)", provider_name, model)
    _instance = _create_provider(provider_name, api_key, model, base_url)
    return _instance


def reset_provider() -> None:
    """Reset the cached provider instance. Useful for testing."""
    global _instance
    _instance = None

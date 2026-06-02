"""Factory for creating LLM provider instances from environment configuration.

Environment variables:
    LLM_PROVIDER: Provider name (default: "venice").
    LLM_API_KEY: API key for the provider (required except codex when reusing
        existing Codex auth).
    LLM_BASE_URL: Base URL for the API (not needed for anthropic).
    LLM_MODEL: Model identifier to use.
    LLM_STRUCTURED_OUTPUT: "true"/"false" to force JSON-schema structured output
        on or off. Unset uses a per-provider default (on for anthropic/openai/venice).
    LLM_CODEX_MODEL_PROVIDER: Optional Codex SDK model-provider override.
    LLM_CODEX_CWD: Optional Codex runtime working directory.

Provider defaults:
    venice: base_url=https://api.venice.ai/api/v1, model=deepseek-v4-flash
    groq: base_url=https://api.groq.com/openai/v1, model=openai/gpt-oss-safeguard-20b
    openai: base_url=https://api.openai.com/v1, model=gpt-4o-mini
    anthropic: model=claude-haiku-4-5-20251001 (uses native Anthropic API)
    codex: model=gpt-5.2-codex (uses native Codex Python SDK)
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
    "codex": {
        "model": "gpt-5.2-codex",
    },
}

# Cached singleton instance
_instance: LLMProvider | None = None


# Providers whose default model reliably supports JSON-schema structured output.
# Verified live against venice/deepseek-v4-flash and openai. groq and custom
# backends default off (support varies by model) and must opt in via
# LLM_STRUCTURED_OUTPUT. Anthropic uses forced tool use — all Claude models
# support it — so it defaults on. Codex uses the SDK's output_schema.
_STRUCTURED_OUTPUT_DEFAULTS: dict[str, bool] = {
    "anthropic": True,
    "codex": True,
    "openai": True,
    "venice": True,
    "groq": False,
}


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var; unset/blank falls back to ``default``."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _create_provider(
    provider_name: str, api_key: str | None, model: str, base_url: str, structured_output: bool
) -> LLMProvider:
    """Create the appropriate provider instance.

    Args:
        provider_name: Provider identifier (anthropic, venice, openai, etc.).
        api_key: API key for the provider.
        model: Model identifier.
        base_url: Base URL (only used for OpenAI-compatible providers).
        structured_output: Whether to enable JSON-schema structured output.

    Returns:
        Configured LLMProvider instance.
    """
    if provider_name == "anthropic":
        from utils.llm.anthropic_provider import AnthropicProvider

        if not api_key:
            raise LLMError("LLM_API_KEY environment variable is not set")
        return AnthropicProvider(api_key=api_key, model=model, structured_output=structured_output)

    if provider_name == "codex":
        from utils.llm.codex_provider import CodexProvider

        model_provider = os.getenv("LLM_CODEX_MODEL_PROVIDER") or None
        cwd = os.getenv("LLM_CODEX_CWD") or None
        return CodexProvider(
            api_key=api_key,
            model=model,
            structured_output=structured_output,
            model_provider=model_provider,
            cwd=cwd,
        )

    from utils.llm.openai_compat import OpenAICompatProvider

    if not api_key:
        raise LLMError("LLM_API_KEY environment variable is not set")
    if not base_url:
        raise LLMError(
            f"LLM_BASE_URL must be set for provider '{provider_name}'. "
            f"Known providers with defaults: {list(_PROVIDER_DEFAULTS.keys())}"
        )
    return OpenAICompatProvider(api_key=api_key, base_url=base_url, model=model, structured_output=structured_output)


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
    if not api_key and provider_name != "codex":
        raise LLMError("LLM_API_KEY environment variable is not set")

    defaults = _PROVIDER_DEFAULTS.get(provider_name, {})
    model = os.getenv("LLM_MODEL") or defaults.get("model", "")
    base_url = os.getenv("LLM_BASE_URL") or defaults.get("base_url", "")

    if not model:
        raise LLMError(
            f"LLM_MODEL must be set for provider '{provider_name}'. "
            f"Known providers with defaults: {list(_PROVIDER_DEFAULTS.keys())}"
        )

    structured_output = _env_bool("LLM_STRUCTURED_OUTPUT", _STRUCTURED_OUTPUT_DEFAULTS.get(provider_name, False))

    logger.info("Creating LLM provider: %s (model=%s, structured=%s)", provider_name, model, structured_output)
    _instance = _create_provider(provider_name, api_key, model, base_url, structured_output)
    return _instance


def reset_provider() -> None:
    """Reset the cached provider instance. Useful for testing."""
    global _instance
    if _instance is not None and hasattr(_instance, "close"):
        _instance.close()  # type: ignore[attr-defined]
    _instance = None

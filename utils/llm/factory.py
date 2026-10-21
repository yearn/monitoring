"""Factory for creating LLM provider instances from environment configuration.

By default only the API provider is used. CLIs named in LLM_CLI_PROVIDERS are
tried first, in order (they run on the CLI's own login/subscription), then the
API provider. CLIs that aren't installed are skipped; a CLI that fails (no
credits, auth, timeout) falls through to the next.

Environment variables:
    LLM_CLI_PROVIDERS: Comma-separated CLI order (default: "none").
        Set to "none" to use the API provider only.
    LLM_CLI_TIMEOUT: Seconds per CLI completion (default: 300).
    LLM_CODEX_MODEL / LLM_CLAUDE_MODEL: Optional model override per CLI; unset
        uses the CLI's own configured default.
    LLM_PROVIDER: API provider name (default: "venice").
    LLM_API_KEY: API key for the provider. Optional when a CLI is available.
    LLM_BASE_URL: Base URL for the API (not needed for anthropic).
    LLM_MODEL: Model identifier to use.
    LLM_STRUCTURED_OUTPUT: "true"/"false" to force JSON-schema structured output
        on or off. Unset uses a per-provider default (on for anthropic/openai/venice).

Provider defaults:
    venice: base_url=https://api.venice.ai/api/v1, model=deepseek-v4-flash
    groq: base_url=https://api.groq.com/openai/v1, model=openai/gpt-oss-safeguard-20b
    openai: base_url=https://api.openai.com/v1, model=gpt-4o-mini
    anthropic: model=claude-haiku-4-5-20251001 (uses native Anthropic API)
    Custom: Set LLM_BASE_URL and LLM_MODEL explicitly.
"""

import os
import shutil

from utils.llm.base import LLMError, LLMProvider
from utils.logger import get_logger

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


# Providers whose default model reliably supports JSON-schema structured output.
# Verified live against venice/deepseek-v4-flash and openai. groq and custom
# backends default off (support varies by model) and must opt in via
# LLM_STRUCTURED_OUTPUT. Anthropic uses forced tool use — all Claude models
# support it — so it defaults on.
_STRUCTURED_OUTPUT_DEFAULTS: dict[str, bool] = {
    "anthropic": True,
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
    provider_name: str, api_key: str, model: str, base_url: str, structured_output: bool
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

        return AnthropicProvider(api_key=api_key, model=model, structured_output=structured_output)

    from utils.llm.openai_compat import OpenAICompatProvider

    if not base_url:
        raise LLMError(
            f"LLM_BASE_URL must be set for provider '{provider_name}'. "
            f"Known providers with defaults: {list(_PROVIDER_DEFAULTS.keys())}"
        )
    return OpenAICompatProvider(api_key=api_key, base_url=base_url, model=model, structured_output=structured_output)


# CLI providers require explicit opt-in; the API remains the default.
_DEFAULT_CLI_ORDER = "none"
# Disabling values for LLM_CLI_PROVIDERS.
_CLI_DISABLED_VALUES = ("", "none", "off", "false", "0")


def _cli_providers() -> list[LLMProvider]:
    """Build the CLI providers named in LLM_CLI_PROVIDERS whose binaries are on PATH."""
    from utils.llm.cli_provider import DEFAULT_CLI_TIMEOUT_SECONDS, ClaudeCLIProvider, CodexCLIProvider

    raw = os.getenv("LLM_CLI_PROVIDERS")
    order = _DEFAULT_CLI_ORDER if raw is None else raw
    if order.strip().lower() in _CLI_DISABLED_VALUES:
        return []

    timeout_raw = os.getenv("LLM_CLI_TIMEOUT", "").strip()
    try:
        timeout = int(timeout_raw) if timeout_raw else DEFAULT_CLI_TIMEOUT_SECONDS
    except ValueError:
        logger.warning("Invalid LLM_CLI_TIMEOUT %r; using %ds", timeout_raw, DEFAULT_CLI_TIMEOUT_SECONDS)
        timeout = DEFAULT_CLI_TIMEOUT_SECONDS

    providers: list[LLMProvider] = []
    for name in (n.strip().lower() for n in order.split(",")):
        if not name:
            continue
        if name not in ("codex", "claude"):
            logger.warning("Unknown CLI provider %r in LLM_CLI_PROVIDERS; skipping", name)
            continue
        binary = shutil.which(name)
        if not binary:
            logger.info("%s CLI not found on PATH; skipping", name)
            continue
        model = os.getenv(f"LLM_{name.upper()}_MODEL", "")
        cls = CodexCLIProvider if name == "codex" else ClaudeCLIProvider
        providers.append(cls(binary=binary, model=model, timeout=timeout))
    return providers


def _api_provider() -> LLMProvider | None:
    """Build the API provider from LLM_* env vars, or None when LLM_API_KEY is unset."""
    provider_name = (os.getenv("LLM_PROVIDER") or "venice").lower()
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        return None

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
    return _create_provider(provider_name, api_key, model, base_url, structured_output)


def get_llm_provider() -> LLMProvider:
    """Create or return the cached LLM provider based on environment variables.

    Returns:
        The API provider alone when no CLI is available, otherwise a
        ``FallbackProvider`` chaining the CLIs and the API provider in priority order.

    Raises:
        LLMError: If no CLI is available and the API provider is unset or fails to initialize.
    """
    global _instance
    if _instance is not None:
        return _instance

    providers = _cli_providers()
    if not providers:
        api = _api_provider()
        if api is None:
            raise LLMError("No LLM CLI (codex/claude) found and LLM_API_KEY environment variable is not set")
        _instance = api
        return _instance

    # A broken API config (missing SDK, no base URL) must not block working CLIs;
    # it only costs the last-resort fallback.
    try:
        api = _api_provider()
    except LLMError as e:
        logger.warning("API LLM provider unavailable, continuing with CLIs only: %s", e)
        api = None
    if api is not None:
        providers.append(api)

    from utils.llm.fallback import FallbackProvider

    # Always wrap CLIs, even a lone one, so a CLI that's out of credits or timing
    # out is disabled after its first failure instead of retried on every call.
    _instance = FallbackProvider(providers)
    return _instance


def reset_provider() -> None:
    """Reset the cached provider instance. Useful for testing."""
    global _instance
    _instance = None

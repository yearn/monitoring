"""Scrub credentials out of text before it is logged, stored, or sent to Telegram.

Exception messages routinely carry secrets: ``requests`` errors quote the full
URL (Alchemy/Infura keys live in the URL path, Etherscan keys in ``apikey=``),
and Telegram API URLs embed the bot token. Crash alerts, automation digests and
alert history all surface exception text, so everything outbound passes
through :func:`redact_secrets`.

Two layers:
    1. Exact values of configured secrets (``PROVIDER_URL_*`` and any env var
       whose name ends in ``_KEY``, ``_TOKEN``, ``_TOKENS``, ``_SECRET`` or
       ``_PASSWORD``). RPC URLs are replaced by ``scheme://host/***`` so the
       provider stays identifiable.
    2. Pattern fallbacks for credentials that were never in the env (bot
       tokens, ``apikey=`` query params, keys in well-known RPC URL paths).
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

REDACTED = "***"

# Env var names whose values are credentials.
_SECRET_NAME_RE = re.compile(r"(_KEY|_KEY_\d+|_TOKEN|_TOKENS|_SECRET|_PASSWORD)$")
_PROVIDER_URL_PREFIX = "PROVIDER_URL"
# Shorter values are too likely to collide with ordinary text.
_MIN_SECRET_LENGTH = 8

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram bot token in API URLs: bot<id>:<token>
    (re.compile(r"bot\d+:[A-Za-z0-9_-]+"), "bot" + REDACTED),
    # Credentials in query strings: ?apikey=..., &api_key=..., &token=... Anchored on
    # ?/& so log text like "token=0x..." (an address) is left alone.
    (
        re.compile(r"(?i)([?&](?:apikey|api_key|api-key|key|token|access_token|auth|secret)=)[^&\s\"'<>)]+"),
        r"\1" + REDACTED,
    ),
    # Keys in versioned RPC paths: alchemy /v2/<key>, infura /v3/<key>, etc.
    (re.compile(r"(https?://[^\s/]+/v\d+/)[A-Za-z0-9_-]{16,}"), r"\1" + REDACTED),
    # Providers that put the key as the first path segment.
    (
        re.compile(
            r"(https?://[^\s/]*(?:quiknode\.pro|gateway\.tenderly\.co|rpc\.ankr\.com/[a-z_]+|"
            r"blastapi\.io|drpc\.org/[a-z]+/[a-z0-9_-]+|chainstack\.com|alchemyapi\.io)/)[A-Za-z0-9_-]{16,}"
        ),
        r"\1" + REDACTED,
    ),
)


def _provider_url_replacement(url: str) -> str | None:
    """Return ``scheme://host/***`` for an RPC URL that carries a path or query, else None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    if parts.path.strip("/") or parts.query or "@" in parts.netloc:
        host = parts.hostname or parts.netloc.rsplit("@", 1)[-1]
        return f"{parts.scheme}://{host}/{REDACTED}"
    return None


def _configured_secrets() -> list[tuple[str, str]]:
    """Return ``(secret, replacement)`` pairs from the environment, longest first."""
    pairs: list[tuple[str, str]] = []
    for name, value in os.environ.items():
        value = value.strip().strip("\"'")
        if len(value) < _MIN_SECRET_LENGTH:
            continue
        if name.startswith(_PROVIDER_URL_PREFIX):
            replacement = _provider_url_replacement(value)
            if replacement:
                pairs.append((value, replacement))
        elif _SECRET_NAME_RE.search(name):
            # ALERTS_API_TOKENS-style lists hold several secrets.
            for part in value.split("|") if name.endswith("_TOKENS") else (value,):
                if len(part) >= _MIN_SECRET_LENGTH:
                    pairs.append((part, REDACTED))
    # Longest first so a URL is replaced before a key it contains.
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return pairs


def redact_secrets(text: str) -> str:
    """Return ``text`` with configured secrets and credential-shaped substrings masked.

    Args:
        text: Arbitrary text such as an exception message or log line.

    Returns:
        The text with secrets replaced by ``***``.
    """
    if not text:
        return text
    for secret, replacement in _configured_secrets():
        if secret in text:
            text = text.replace(secret, replacement)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def safe_url(url: str) -> str:
    """Return a loggable form of ``url``: scheme and host only when it carries a path or query."""
    return _provider_url_replacement(url) or url

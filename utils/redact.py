"""Scrub credentials out of text before it is logged, stored, or sent to Telegram.

Exception messages routinely carry credentials: ``requests`` errors quote the
full request URL, and Telegram API URLs embed the bot token. Crash alerts,
automation digests and alert history all surface exception text, so everything
outbound passes through :func:`redact_secrets`.

Two layers:
    1. Exact values of configured secrets (``PROVIDER_URL_*`` and any env var
       whose name ends in ``_KEY``, ``_TOKEN``, ``_TOKENS``, ``_SECRET`` or
       ``_PASSWORD``, optionally followed by a suffix such as ``_DEFAULT`` or
       ``_2``). Provider URLs are replaced by ``scheme://host/REDACTED`` so the
       provider stays identifiable. Their relative request targets are also
       masked because connection errors can omit the scheme and host. Each value
       is matched both raw and in its ``escape_markdown`` form.
    2. Pattern fallbacks for credentials that were never in the env (bot
       tokens, credential query params, well-known provider URL paths).

The marker is a plain word: ``***`` would be read as bold markup by Telegram
Markdown and break the message.
"""

from __future__ import annotations

import functools
import os
import re
from urllib.parse import urlsplit

REDACTED = "REDACTED"

# Env var names whose values are credentials, e.g. ETHERSCAN_API_KEY,
# TELEGRAM_BOT_TOKEN_DEFAULT, SAFE_API_KEY_2, ALERTS_API_TOKENS.
_SECRET_NAME_RE = re.compile(r"(_KEY|_TOKEN|_TOKENS|_SECRET|_PASSWORD)(_[A-Z0-9]+)?$")
_PROVIDER_URL_PREFIX = "PROVIDER_URL"
# Shorter values are too likely to collide with ordinary text.
_MIN_SECRET_LENGTH = 8
# Characters escape_markdown (utils/telegram.py) prefixes with a backslash.
_MARKDOWN_SPECIALS = "\\_*`["

# Credential characters, plus "\" so a value passed through escape_markdown
# ("ab\_cd") is still matched as one run.
_CRED = r"[A-Za-z0-9_\\-]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram bot token in API URLs: bot<id>:<token>
    (re.compile(rf"bot\d+:{_CRED}+"), "bot" + REDACTED),
    # Bare Telegram bot token: <bot id>:AA<secret>
    (re.compile(rf"(?<![\w/])\d{{6,12}}:AA{_CRED}{{30,}}"), REDACTED),
    # Credentials in query strings: ?apikey=..., &api_key=..., &token=... Anchored on
    # ?/& so log text like "token=0x..." (an address) is left alone.
    (
        re.compile(r"(?i)([?&](?:apikey|api_key|api-key|key|token|access_token|auth|secret)=)[^&\s\"'<>)]+"),
        r"\1" + REDACTED,
    ),
    # Credentials in versioned provider paths: /v2/<key>, /v3/<key>, etc.
    (re.compile(rf"(https?://[^\s/]+/v\d+/){_CRED}{{16,}}"), r"\1" + REDACTED),
    # Providers that put the credential as the first path segment.
    (
        re.compile(
            r"(https?://[^\s/]*(?:quiknode\.pro|gateway\.tenderly\.co|rpc\.ankr\.com/[a-z_]+|"
            rf"blastapi\.io|drpc\.org/[a-z]+/[a-z0-9_-]+|chainstack\.com|alchemyapi\.io)/){_CRED}{{16,}}"
        ),
        r"\1" + REDACTED,
    ),
)


def _escape_markdown(text: str) -> str:
    """Mirror ``utils.telegram.escape_markdown`` (not imported: telegram imports this module)."""
    for ch in _MARKDOWN_SPECIALS:
        text = text.replace(ch, f"\\{ch}")
    return text


def _mask_url(url: str) -> str | None:
    """Return ``url`` with everything after the host masked, or None if it carries no credential.

    Handles scheme-less values (``host/v2/<key>``) as well, since a misconfigured
    provider URL is exactly the one that gets logged as invalid.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    if parts.scheme and parts.netloc:
        if parts.path.strip("/") or parts.query or "@" in parts.netloc:
            host = parts.hostname or parts.netloc.rsplit("@", 1)[-1]
            return f"{parts.scheme}://{host}/{REDACTED}"
        return None
    separator = re.search(r"[/?#]", url)
    host = url[: separator.start()] if separator else url
    rest = url[separator.end() :] if separator else ""
    if "@" in host:
        return f"{host.rsplit('@', 1)[-1]}/{REDACTED}"
    if rest.strip("/"):
        return f"{host}/{REDACTED}"
    # A lone token without dots is more likely a bare credential than a hostname.
    return None if "." in host or ":" in host else REDACTED


def _secret_env_items() -> tuple[tuple[str, str], ...]:
    """Return the env vars that hold credentials, in a hashable form for caching."""
    return tuple(
        (name, value)
        for name, value in os.environ.items()
        if name.startswith(_PROVIDER_URL_PREFIX) or _SECRET_NAME_RE.search(name)
    )


@functools.lru_cache(maxsize=8)
def _secret_pairs(items: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """Build ``(secret, replacement)`` pairs, longest first, for the given env items."""
    pairs: list[tuple[str, str]] = []
    for name, raw_value in items:
        value = raw_value.strip().strip("\"'")
        if len(value) < _MIN_SECRET_LENGTH:
            continue
        if name.startswith(_PROVIDER_URL_PREFIX):
            replacement = _mask_url(value)
            if not replacement or replacement == value:
                continue
            pairs.append((value, replacement))
            try:
                parts = urlsplit(value)
            except ValueError:
                # Malformed (e.g. "https://[broken/..."): _mask_url already masks the
                # whole value, and there is no request target to derive.
                continue
            if parts.scheme and parts.netloc:
                # urllib3 connection errors quote only the request target (the
                # path and query) rather than the configured absolute URL.
                target = parts.path or "/"
                if parts.query:
                    target += "?" + parts.query
                # Scope this to connection-error URLs: public targets like /eth
                # must not replace substrings in links such as https://etherscan.io.
                if target != "/":
                    pairs.append(("url: " + target, "url: /" + REDACTED))
        else:
            match = _SECRET_NAME_RE.search(name)
            # ALERTS_API_TOKENS-style lists hold several secrets.
            parts_list = value.split("|") if match and match.group(1) == "_TOKENS" else [value]
            pairs.extend((part, REDACTED) for part in parts_list if len(part) >= _MIN_SECRET_LENGTH)
    # Alert text is often escape_markdown()-ed before it is sent, which breaks an
    # exact match on values containing "_" or "*".
    pairs.extend((_escape_markdown(secret), repl) for secret, repl in list(pairs) if _escape_markdown(secret) != secret)
    # Longest first so a URL is replaced before a credential it contains.
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return tuple(pairs)


def redact_secrets(text: str) -> str:
    """Return ``text`` with configured secrets and credential-shaped substrings masked.

    Args:
        text: Arbitrary text such as an exception message or log line.

    Returns:
        The text with secrets replaced by ``REDACTED``.
    """
    if not text:
        return text
    for secret, replacement in _secret_pairs(_secret_env_items()):
        if secret in text:
            text = text.replace(secret, replacement)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def safe_url(url: str) -> str:
    """Return a loggable form of ``url``: only scheme and host when it carries a path or query.

    Args:
        url: A provider URL, possibly malformed or missing its scheme.

    Returns:
        The masked URL, or ``url`` unchanged when it holds nothing sensitive.
    """
    return _mask_url(url) or url

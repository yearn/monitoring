"""Fetch verified contract source from Etherscan and extract relevant natspec.

Used by the AI transaction explainer to ground LLM summaries in the actual
contract semantics (function natspec, state-variable docs) rather than
guessing from function names alone.

Etherscan v2 uses a single multichain API key.
"""

import json
import os
import re
from dataclasses import dataclass

from utils.http import fetch_json
from utils.logging import get_logger

logger = get_logger("utils.source_context")

ETHERSCAN_V2_API_URL = "https://api.etherscan.io/v2/api"

# Etherscan source is capped at ~500KB; trim our extracted snippet hard so we
# never blow the LLM prompt up. The natspec for one function + a state var is
# always under a few hundred chars in practice.
MAX_SNIPPET_CHARS = 4000

# In-memory cache keyed by (chain_id, address_lower) -> concatenated source.
# Workflows are short-lived so a per-process dict is sufficient.
_source_cache: dict[tuple[int, str], str | None] = {}


@dataclass(frozen=True)
class SourceContext:
    """Extracted natspec/source snippet for a specific function call."""

    contract_name: str
    function_snippet: str  # natspec + function signature line
    state_var_snippets: list[str]  # natspec + declaration for each mutated state var


def _fetch_source(chain_id: int, address: str) -> tuple[str, str] | None:
    """Fetch (contract_name, concatenated_source) for a verified contract.

    Returns None if the API key is missing, the contract is unverified, or the
    request fails. Caches by (chain_id, address) so repeated calls during the
    same run hit the API only once.
    """
    api_key = os.getenv("ETHERSCAN_TOKEN")
    if not api_key:
        return None

    address_lower = address.lower()
    cache_key = (chain_id, address_lower)
    if cache_key in _source_cache:
        cached = _source_cache[cache_key]
        return ("", cached) if cached else None

    params = {
        "chainid": str(chain_id),
        "module": "contract",
        "action": "getsourcecode",
        "address": address,
        "apikey": api_key,
    }
    data = fetch_json(ETHERSCAN_V2_API_URL, params=params)
    if not data or data.get("status") != "1":
        _source_cache[cache_key] = None
        return None

    results = data.get("result") or []
    if not results:
        _source_cache[cache_key] = None
        return None

    entry = results[0]
    raw_source = entry.get("SourceCode") or ""
    contract_name = entry.get("ContractName") or ""

    if not raw_source:
        _source_cache[cache_key] = None
        return None

    concatenated = _concat_sources(raw_source)
    _source_cache[cache_key] = concatenated
    return (contract_name, concatenated)


def _concat_sources(raw_source: str) -> str:
    """Etherscan returns either a single-file string or a JSON blob of files.

    Multi-file: wrapped in double braces `{{ ... }}` (standard JSON input format).
    Returns a single concatenated string for searching.
    """
    stripped = raw_source.strip()
    if stripped.startswith("{{") and stripped.endswith("}}"):
        # Standard JSON input: strip the outer braces
        try:
            parsed = json.loads(stripped[1:-1])
        except json.JSONDecodeError:
            return raw_source
        sources = parsed.get("sources") or {}
        return "\n\n".join(f["content"] for f in sources.values() if isinstance(f, dict) and "content" in f)

    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and "sources" in parsed:
                sources = parsed["sources"]
                return "\n\n".join(f["content"] for f in sources.values() if isinstance(f, dict) and "content" in f)
        except json.JSONDecodeError:
            pass

    return raw_source


_NATSPEC_LINE = r"(?:[ \t]*///.*\n|[ \t]*\*[^/].*\n|[ \t]*/\*\*[\s\S]*?\*/[ \t]*\n)"
_NATSPEC_BLOCK = rf"(?:(?:{_NATSPEC_LINE})+)?"


def _extract_function_snippet(source: str, function_name: str) -> str:
    """Find a function definition and any preceding natspec comment block.

    Returns the natspec lines + the function signature line, or "" if not found.
    """
    pattern = re.compile(
        rf"({_NATSPEC_BLOCK})([ \t]*function\s+{re.escape(function_name)}\b[^{{;]*[{{;])",
        re.MULTILINE,
    )
    match = pattern.search(source)
    if not match:
        return ""

    natspec = match.group(1) or ""
    signature_line = match.group(2).strip()
    return f"{natspec.rstrip()}\n{signature_line}".strip()


def _find_state_var_writes(source: str, function_name: str) -> list[str]:
    """Find state variable names assigned to inside the function body.

    Locates `<name> = ...` assignments (excluding `==`, declarations, and `:=`).
    Returns variable names in order encountered, deduplicated.
    """
    body = _extract_function_body(source, function_name)
    if not body:
        return []

    # Match `<name> = ` where the name starts a statement (preceded by ;, {, } or
    # newline). This excludes typed local declarations like `uint256 local = 1`
    # because they're preceded by a type identifier on the same line.
    assignment_pattern = re.compile(r"(?:^|[;{}\n])\s*([a-zA-Z_]\w*)\s*=(?!=)", re.MULTILINE)
    seen: set[str] = set()
    ordered: list[str] = []
    keywords = {
        "if",
        "for",
        "while",
        "require",
        "revert",
        "return",
        "emit",
        "assembly",
        "unchecked",
    }
    for m in assignment_pattern.finditer(body):
        name = m.group(1)
        if name in keywords or name.startswith("_"):
            continue
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _extract_function_body(source: str, function_name: str) -> str:
    """Return the body of `function <name>(...) { ... }` or "" if not found."""
    pattern = re.compile(rf"function\s+{re.escape(function_name)}\b[^{{]*\{{", re.MULTILINE)
    match = pattern.search(source)
    if not match:
        return ""

    start = match.end()
    depth = 1
    i = start
    while i < len(source) and depth > 0:
        c = source[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    if depth != 0:
        return ""
    return source[start : i - 1]


def _extract_state_var_snippet(source: str, var_name: str) -> str:
    """Find a state variable declaration with any preceding natspec.

    Matches lines like `uint256 public maxSlippage;` or `mapping(...) public foo;`.
    Skips matches inside function bodies (heuristic: requires `public`, `private`,
    `internal`, `external`, `immutable`, or `constant` modifier).
    """
    pattern = re.compile(
        rf"({_NATSPEC_BLOCK})("
        rf"[ \t]*"
        rf"(?:mapping\s*\([^)]+\)|[a-zA-Z_]\w*(?:\[[^\]]*\])?)"
        rf"\s+"
        rf"(?:public|private|internal|external)"
        rf"(?:\s+(?:immutable|constant|override(?:\s*\([^)]*\))?|virtual))*"
        rf"\s+{re.escape(var_name)}\s*[=;])",
        re.MULTILINE,
    )
    match = pattern.search(source)
    if not match:
        return ""

    natspec = match.group(1) or ""
    decl_line = match.group(2).strip()
    return f"{natspec.rstrip()}\n{decl_line}".strip()


def get_source_context(chain_id: int, address: str, function_name: str) -> SourceContext | None:
    """Fetch source and extract natspec for `function_name` and its state writes.

    Best-effort: returns None if the contract is unverified, the API key is
    missing, or the function cannot be located in the source.
    """
    fetched = _fetch_source(chain_id, address)
    if not fetched:
        return None
    contract_name, source = fetched

    func_snippet = _extract_function_snippet(source, function_name)
    if not func_snippet:
        return None

    var_names = _find_state_var_writes(source, function_name)
    var_snippets: list[str] = []
    total = len(func_snippet)
    for name in var_names:
        snippet = _extract_state_var_snippet(source, name)
        if not snippet:
            continue
        if total + len(snippet) > MAX_SNIPPET_CHARS:
            break
        var_snippets.append(snippet)
        total += len(snippet)

    return SourceContext(
        contract_name=contract_name,
        function_snippet=func_snippet,
        state_var_snippets=var_snippets,
    )


def format_source_context(ctx: SourceContext) -> str:
    """Format a SourceContext into a prompt-ready string."""
    lines: list[str] = []
    if ctx.contract_name:
        lines.append(f"Contract: {ctx.contract_name}")
    lines.append("")
    lines.append(ctx.function_snippet)
    if ctx.state_var_snippets:
        lines.append("")
        lines.append("Relevant state variables:")
        for snippet in ctx.state_var_snippets:
            lines.append("")
            lines.append(snippet)
    return "\n".join(lines)


def reset_cache() -> None:
    """Reset the in-memory source cache. Useful for testing."""
    _source_cache.clear()

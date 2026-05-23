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

# Per-process cache: (chain_id, address_lower) -> (contract_name, source) or None for miss.
# Workflows are short-lived so a process-lifetime dict is sufficient.
_source_cache: dict[tuple[int, str], tuple[str, str] | None] = {}

_NATSPEC_LINE = r"(?:[ \t]*///.*\n|[ \t]*\*[^/].*\n|[ \t]*/\*\*[\s\S]*?\*/[ \t]*\n)"
_NATSPEC_BLOCK = rf"(?:(?:{_NATSPEC_LINE})+)?"

# Statement-leading LHS of an assignment. Captures the var name from:
#   `x = v` / `x[k] = v` / `obj.x = v` / `getStorage().x[k] = v` (diamond pattern).
# Excludes `==` and typed locals like `uint256 x = 1` (those have a type identifier
# directly before x with no `.` separator, so the optional prefix won't match).
_ASSIGNMENT_RE = re.compile(
    r"(?:^|[;{}\n])\s*(?:\w+(?:\(\))?\.)?([a-zA-Z_]\w*)(?:\[[^\]]*\]|\.\w+)*\s*=(?!=)",
    re.MULTILINE,
)

_CONTROL_KEYWORDS = frozenset({"if", "for", "while", "require", "revert", "return", "emit", "assembly", "unchecked"})

# Proxy contract names that are not informative on their own — when the target is
# named one of these, prefer the implementation contract's name.
_GENERIC_PROXY_NAMES = frozenset(
    {
        "TransparentUpgradeableProxy",
        "ERC1967Proxy",
        "BeaconProxy",
        "EIP173Proxy",
        "Proxy",
        "UpgradeableProxy",
        "InitializableImmutableAdminUpgradeabilityProxy",
        "InitializableAdminUpgradeabilityProxy",
    }
)


@dataclass(frozen=True)
class SourceContext:
    """Extracted natspec/source snippet for a specific function call."""

    contract_name: str
    function_snippet: str  # natspec + function signature line
    state_var_snippets: list[str]  # natspec + declaration for each mutated state var


def fetch_source(chain_id: int, address: str) -> tuple[str, str] | None:
    """Fetch (contract_name, concatenated_source) for a verified contract.

    Returns None if the API key is missing, the contract is unverified, or the
    request fails. Caches by (chain_id, address) so repeated calls during the
    same run hit the API only once.
    """
    api_key = os.getenv("ETHERSCAN_TOKEN")
    if not api_key:
        return None

    cache_key = (chain_id, address.lower())
    if cache_key in _source_cache:
        return _source_cache[cache_key]

    params = {
        "chainid": str(chain_id),
        "module": "contract",
        "action": "getsourcecode",
        "address": address,
        "apikey": api_key,
    }
    data = fetch_json(ETHERSCAN_V2_API_URL, params=params)
    results = (data or {}).get("result") or [] if (data or {}).get("status") == "1" else []
    entry = results[0] if results else {}
    raw_source = entry.get("SourceCode") or ""

    if not raw_source:
        _source_cache[cache_key] = None
        return None

    result = (entry.get("ContractName") or "", _concat_sources(raw_source))
    _source_cache[cache_key] = result
    return result


def _concat_sources(raw_source: str) -> str:
    """Etherscan returns either a single-file string or a JSON blob of files.

    Multi-file solc input is wrapped in double braces `{{ ... }}`; standard JSON
    has single braces. Returns concatenated file contents for searching.
    """
    stripped = raw_source.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return raw_source

    payload = stripped[1:-1] if stripped.startswith("{{") else stripped
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return raw_source

    sources = parsed.get("sources") if isinstance(parsed, dict) else None
    if not isinstance(sources, dict):
        return raw_source
    return "\n\n".join(f["content"] for f in sources.values() if isinstance(f, dict) and "content" in f)


def _extract_function_snippet(source: str, function_name: str) -> str:
    """Find a function definition and any preceding natspec comment block."""
    pattern = re.compile(
        rf"({_NATSPEC_BLOCK})([ \t]*function\s+{re.escape(function_name)}\b[^{{;]*[{{;])",
        re.MULTILINE,
    )
    match = pattern.search(source)
    if not match:
        return ""
    natspec = match.group(1) or ""
    return f"{natspec.rstrip()}\n{match.group(2).strip()}".strip()


def find_state_var_writes(source: str, function_name: str) -> list[str]:
    """State variable names assigned inside the function body, deduped, in order."""
    body = _extract_function_body(source, function_name)
    if not body:
        return []

    seen: set[str] = set()
    ordered: list[str] = []
    for m in _ASSIGNMENT_RE.finditer(body):
        name = m.group(1)
        if name in _CONTROL_KEYWORDS or name.startswith("_") or name in seen:
            continue
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
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    if depth != 0:
        return ""
    return source[start : i - 1]


def extract_state_var_snippet(source: str, var_name: str) -> str:
    """Find a state variable declaration with any preceding natspec.

    Requires a visibility modifier so local declarations inside function bodies
    don't match.
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
    return f"{natspec.rstrip()}\n{match.group(2).strip()}".strip()


def _build_context(contract_name: str, source: str, function_name: str) -> SourceContext | None:
    """Extract natspec snippets from a source string. None if the function isn't found."""
    func_snippet = _extract_function_snippet(source, function_name)
    if not func_snippet:
        return None

    var_names = find_state_var_writes(source, function_name)
    var_snippets: list[str] = []
    total = len(func_snippet)
    for name in var_names:
        snippet = extract_state_var_snippet(source, name)
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


def get_source_context(chain_id: int, address: str, function_name: str) -> SourceContext | None:
    """Fetch source and extract natspec for `function_name` and its state writes.

    If the function isn't present in the target's verified source, follow the
    EIP-1967 proxy slot (if any) and retry against the implementation source.
    Best-effort: returns None on any failure (unverified, missing key, no match).
    """
    fetched = fetch_source(chain_id, address)
    if fetched:
        ctx = _build_context(fetched[0], fetched[1], function_name)
        if ctx:
            return ctx

    # Function not in proxy source — try the implementation if this is a proxy.
    from utils.proxy import get_current_implementation

    impl = get_current_implementation(address, chain_id)
    if not impl or impl.lower() == address.lower():
        return None

    fetched_impl = fetch_source(chain_id, impl)
    if not fetched_impl:
        return None
    return _build_context(fetched_impl[0], fetched_impl[1], function_name)


def get_contract_label(chain_id: int, address: str) -> str:
    """Best-effort human label for a contract address.

    Resolution order:
      1. Safe utility registry (no API call) — covers MultiSendCallOnly etc.
      2. Etherscan ContractName for the address.
      3. If that name is a generic proxy wrapper, follow EIP-1967 to the impl
         and use the impl's contract name instead.

    Returns "" for EOAs, unverified contracts, missing API key, or any failure.
    """
    if not address:
        return ""

    # Lazy import to keep utils/source_context.py free of safe/ dependencies at
    # module load time (and to avoid a cycle if safe ever imports from here).
    from safe.multisend import safe_utility_label

    cheap = safe_utility_label(address)
    if cheap:
        return cheap

    # Swiss Knife has curated labels for well-known protocol contracts
    # (Circle: USDC Token, Uniswap V3 Router, etc.) — much richer than the
    # Etherscan ContractName for those. High precision, low recall, so we
    # fall through to Etherscan for the long tail of custom contracts.
    from utils.swiss_knife import fetch_swiss_knife_labels

    sk_labels = fetch_swiss_knife_labels(address, chain_id)
    if sk_labels:
        # Use the first label as the human name; it's the most descriptive
        # (e.g. "Circle: USDC Token") with subsequent labels being tags.
        return sk_labels[0]

    fetched = fetch_source(chain_id, address)
    if not fetched:
        return ""

    name = fetched[0]
    if name and name not in _GENERIC_PROXY_NAMES:
        return name

    from utils.proxy import get_current_implementation

    impl = get_current_implementation(address, chain_id)
    if not impl or impl.lower() == address.lower():
        return name

    impl_fetched = fetch_source(chain_id, impl)
    if impl_fetched and impl_fetched[0]:
        return impl_fetched[0]
    return name


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

"""Fetch verified contract source from Etherscan and extract relevant natspec.

Used by the AI transaction explainer to ground LLM summaries in the actual
contract semantics (function natspec, state-variable docs) rather than
guessing from function names alone.

Etherscan v2 uses a single multichain API key.
"""

import json
import os
import re
import threading
from dataclasses import dataclass

from utils.disk_cache import MISS, DiskCache
from utils.http_client import fetch_json
from utils.logger import get_logger

logger = get_logger("utils.source_context")

ETHERSCAN_V2_API_URL = "https://api.etherscan.io/v2/api"

# Etherscan source is capped at ~500KB; trim our extracted snippet hard so we
# never blow the LLM prompt up. The natspec for one function + a state var is
# always under a few hundred chars in practice.
MAX_SNIPPET_CHARS = 4000

# Per-process cache: (chain_id, address_lower) -> (contract_name, source, abi_json_string)
# or None for miss. Backed by an on-disk cache (below) so the same verified source is
# not re-fetched from Etherscan on every cron run; the in-memory dict still serves repeat
# lookups within a single process for free.
# The ABI is stored as the raw JSON string from Etherscan and parsed lazily by callers
# that need it — keeps the cache small for the common case where only source is read.
_source_cache: dict[tuple[int, str], tuple[str, str, str] | None] = {}
_source_cache_hits = 0
_source_cache_misses = 0
_source_cache_lock = threading.RLock()
_source_key_locks: dict[tuple[int, str], threading.Lock] = {}

# On-disk layer keyed by "chain_id-address". Verified source is immutable per address, so
# positive entries never expire; "unverified" misses get the short negative TTL so a
# contract verified later is picked up. Source can be large (~500KB) — bound the namespace
# by total bytes as well as entry count. All tunable via env.
_source_disk_cache = DiskCache(
    namespace="source-cache",
    max_entries=int(os.getenv("SOURCE_CACHE_MAX_ENTRIES", "5000")),
    max_bytes=int(os.getenv("SOURCE_CACHE_MAX_BYTES", str(256 * 1024 * 1024))),
)


def _disk_key(chain_id: int, address: str) -> str:
    return f"{chain_id}-{address.lower()}"


def _lock_for_key(cache_key: tuple[int, str]) -> threading.Lock:
    with _source_cache_lock:
        return _source_key_locks.setdefault(cache_key, threading.Lock())


def _record_cache_event(source: str, hit: bool, cache_key: tuple[int, str]) -> None:
    global _source_cache_hits, _source_cache_misses
    with _source_cache_lock:
        if hit:
            _source_cache_hits += 1
        else:
            _source_cache_misses += 1
        hits = _source_cache_hits
        misses = _source_cache_misses
    logger.debug(
        "source cache %s %s for %s:%s (hits=%s misses=%s)",
        source,
        "hit" if hit else "miss",
        cache_key[0],
        cache_key[1],
        hits,
        misses,
    )


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


def _fetch_etherscan_contract(chain_id: int, address: str) -> tuple[str, str, str] | None:
    """Internal: fetch and cache (contract_name, source, abi_json_string).

    Single Etherscan call shared by `fetch_source` (for natspec) and
    `fetch_function_input_names` (for parameter labels).
    """
    api_key = os.getenv("ETHERSCAN_TOKEN")
    if not api_key:
        return None

    cache_key = (chain_id, address.lower())
    with _source_cache_lock:
        if cache_key in _source_cache:
            _record_cache_event("memory", True, cache_key)
            return _source_cache[cache_key]

    with _lock_for_key(cache_key):
        with _source_cache_lock:
            if cache_key in _source_cache:
                _record_cache_event("memory", True, cache_key)
                return _source_cache[cache_key]

        disk_key = _disk_key(chain_id, address)
        disk_val = _source_disk_cache.get(disk_key)
        if disk_val is not MISS:
            _record_cache_event("disk", True, cache_key)
            if disk_val is None:
                # Cached negative: a prior run saw this contract unverified.
                with _source_cache_lock:
                    _source_cache[cache_key] = None
                return None
            if isinstance(disk_val, (list, tuple)) and len(disk_val) == 3:
                record = (disk_val[0], disk_val[1], disk_val[2])
                with _source_cache_lock:
                    _source_cache[cache_key] = record
                return record
            # Unexpected shape — fall through to a live fetch.
        else:
            _record_cache_event("disk", False, cache_key)

        params = {
            "chainid": str(chain_id),
            "module": "contract",
            "action": "getsourcecode",
            "address": address,
            "apikey": api_key,
        }
        data = fetch_json(ETHERSCAN_V2_API_URL, params=params)
        # A clean response is status "1" with a result array; anything else (None on a
        # request error, or status "0") is treated as transient and not persisted, so an
        # Etherscan blip can't poison the disk cache as a day-long "unverified".
        status_ok = data is not None and data.get("status") == "1"
        results = (data or {}).get("result") or [] if status_ok else []
        entry = results[0] if results else {}
        raw_source = entry.get("SourceCode") or ""

        if not raw_source:
            with _source_cache_lock:
                _source_cache[cache_key] = None
            if status_ok:
                _source_disk_cache.set_negative(disk_key)
            return None

        result = (
            entry.get("ContractName") or "",
            _concat_sources(raw_source),
            entry.get("ABI") or "",
        )
        with _source_cache_lock:
            _source_cache[cache_key] = result
        _source_disk_cache.set_positive(disk_key, list(result))
        return result


def fetch_source(chain_id: int, address: str) -> tuple[str, str] | None:
    """Fetch (contract_name, concatenated_source) for a verified contract.

    Returns None if the API key is missing, the contract is unverified, or the
    request fails. Caches by (chain_id, address) so repeated calls during the
    same run hit the API only once.
    """
    record = _fetch_etherscan_contract(chain_id, address)
    return None if record is None else (record[0], record[1])


def _parse_abi(abi_json: str) -> list[dict] | None:
    """Parse Etherscan's ABI string. Returns None for unverified/malformed."""
    if not abi_json or abi_json == "Contract source code not verified":
        return None
    try:
        parsed = json.loads(abi_json)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def fetch_function_input_names(chain_id: int, address: str, function_name: str) -> list[str] | None:
    """Return parameter names for ``function_name`` on the verified ABI, or None.

    Used by the explainer to render decoded calldata with named parameters
    (``_maxSlippage: 95e16``) instead of bare types (``uint256: 95e16``).
    Follows EIP-1967 to the implementation when the target is a generic
    proxy — the proxy's ABI has the proxy's own functions, not the impl's.
    """
    record = _fetch_etherscan_contract(chain_id, address)
    if record is None:
        return None

    names = _function_input_names_from_abi(record[2], function_name)
    if names is not None:
        return names

    # Function isn't in target ABI — try the impl if this is a generic proxy.
    if record[0] and record[0] not in _GENERIC_PROXY_NAMES:
        return None

    from utils.proxy import get_current_implementation

    impl = get_current_implementation(address, chain_id)
    if not impl or impl.lower() == address.lower():
        return None
    impl_record = _fetch_etherscan_contract(chain_id, impl)
    if impl_record is None:
        return None
    return _function_input_names_from_abi(impl_record[2], function_name)


def get_verification_status(chain_id: int, address: str) -> bool | None:
    """Tri-state Etherscan verification check for ``address``.

    Returns ``True`` if the contract has verified source, ``False`` if Etherscan
    explicitly reports it unverified, and ``None`` when we can't tell (no API
    key, request error). The ``None`` case is deliberate — flagging a target as
    "UNVERIFIED" on a transient failure would cry wolf, so callers should only
    surface the warning on an explicit ``False``.
    """
    api_key = os.getenv("ETHERSCAN_TOKEN")
    if not api_key:
        return None

    # Verified contracts are already cached by source-context collection.
    if _fetch_etherscan_contract(chain_id, address) is not None:
        return True

    # Cache miss → unverified or a transient error. One explicit call to
    # disambiguate: Etherscan returns status "1" with an empty SourceCode for a
    # genuinely unverified contract, and status "0" on error.
    params = {
        "chainid": str(chain_id),
        "module": "contract",
        "action": "getsourcecode",
        "address": address,
        "apikey": api_key,
    }
    data = fetch_json(ETHERSCAN_V2_API_URL, params=params)
    if not data or data.get("status") != "1":
        return None
    results = data.get("result") or []
    entry = results[0] if isinstance(results, list) and results else {}
    if not (entry.get("SourceCode") or ""):
        return False
    return None


def get_function_state_mutability(chain_id: int, address: str, function_name: str) -> str | None:
    """Return the ABI ``stateMutability`` for ``function_name`` (or None).

    One of ``"pure"`` / ``"view"`` / ``"nonpayable"`` / ``"payable"``. Follows
    EIP-1967 to the implementation for generic proxies, same as
    :func:`fetch_function_input_names`. If the function is overloaded and any
    overload is ``payable``, ``"payable"`` is returned so a value-bearing call
    is never falsely flagged as reverting.
    """
    record = _fetch_etherscan_contract(chain_id, address)
    if record is None:
        return None

    mut = _function_state_mutability_from_abi(record[2], function_name)
    if mut is not None:
        return mut

    if record[0] and record[0] not in _GENERIC_PROXY_NAMES:
        return None

    from utils.proxy import get_current_implementation

    impl = get_current_implementation(address, chain_id)
    if not impl or impl.lower() == address.lower():
        return None
    impl_record = _fetch_etherscan_contract(chain_id, impl)
    if impl_record is None:
        return None
    return _function_state_mutability_from_abi(impl_record[2], function_name)


def get_function_signature_by_selector(chain_id: int, address: str, selector_hex: str) -> str | None:
    """Return the canonical signature for a 4-byte selector from the verified ABI.

    More reliable than the Sourcify 4byte database, which returns *all* known
    signatures for a selector (collisions) and may pick the wrong one. The
    target's own ABI has exactly the function being called. Follows EIP-1967 to
    the implementation for generic proxies. Returns None when unavailable.
    """
    record = _fetch_etherscan_contract(chain_id, address)
    if record is None:
        return None

    sig = _function_signature_from_abi(record[2], selector_hex)
    if sig is not None:
        return sig

    # Selector not in the target's own ABI. If the target proxies to an impl,
    # try there. Unlike the natspec/param-name helpers we don't gate on a
    # generic-proxy *name* — custom proxies (e.g. Compound's "Unitroller") miss
    # that list — so we follow whenever an impl address resolves.
    from utils.proxy import get_current_implementation

    impl = get_current_implementation(address, chain_id)
    if not impl or impl.lower() == address.lower():
        return None
    impl_record = _fetch_etherscan_contract(chain_id, impl)
    if impl_record is None:
        return None
    return _function_signature_from_abi(impl_record[2], selector_hex)


def _function_signature_from_abi(abi_json: str, selector_hex: str) -> str | None:
    """Find the function whose 4-byte selector matches and return its signature."""
    from eth_utils import function_signature_to_4byte_selector
    from eth_utils.abi import collapse_if_tuple

    abi = _parse_abi(abi_json)
    if abi is None:
        return None
    want = selector_hex.lower()
    for entry in abi:
        if not isinstance(entry, dict) or entry.get("type") != "function":
            continue
        name = entry.get("name")
        if not name:
            continue
        inputs = entry.get("inputs") or []
        try:
            types = ",".join(collapse_if_tuple(inp) for inp in inputs)
            sig = f"{name}({types})"
            sel = "0x" + function_signature_to_4byte_selector(sig).hex()
        except Exception:  # noqa: BLE001 - malformed ABI entry, skip
            continue
        if sel.lower() == want:
            return sig
    return None


def _function_state_mutability_from_abi(abi_json: str, function_name: str) -> str | None:
    """Pull ``stateMutability`` for ``function_name`` from an ABI JSON string."""
    abi = _parse_abi(abi_json)
    if abi is None:
        return None
    muts: list[str] = []
    for entry in abi:
        if not isinstance(entry, dict) or entry.get("type") != "function":
            continue
        if entry.get("name") != function_name:
            continue
        mut = entry.get("stateMutability")
        if isinstance(mut, str):
            muts.append(mut)
    if not muts:
        return None
    return "payable" if "payable" in muts else muts[0]


def _function_input_names_from_abi(abi_json: str, function_name: str) -> list[str] | None:
    """Pull input names for ``function_name`` out of an ABI JSON string.

    Returns ``None`` if the function isn't present; an empty list if the
    function has no parameters. If any input is unnamed (anonymous param),
    return ``None`` rather than a mix — the LLM is better off without than
    with partial labels.
    """
    abi = _parse_abi(abi_json)
    if abi is None:
        return None
    for entry in abi:
        if not isinstance(entry, dict) or entry.get("type") != "function":
            continue
        if entry.get("name") != function_name:
            continue
        inputs = entry.get("inputs") or []
        names = [(inp.get("name") or "") for inp in inputs if isinstance(inp, dict)]
        if any(not n for n in names):
            return None
        return names
    return None


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

    Thin compatibility wrapper around :func:`utils.address_resolver.resolve_address_label`.
    Kept here so existing imports (and tests that patch this name) continue to work.
    """
    from utils.address_resolver import resolve_address_label

    return resolve_address_label(chain_id, address)


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
    global _source_cache_hits, _source_cache_misses
    with _source_cache_lock:
        _source_cache.clear()
        _source_key_locks.clear()
        _source_cache_hits = 0
        _source_cache_misses = 0

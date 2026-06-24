"""Compare two proxy implementations' verified source to surface upgrade diffs.

When a governance tx upgrades a proxy, the LLM normally sees just the new impl
address and a diff URL it can't follow. This module fetches both impls' source,
extracts the structural surface (function signatures + state variables in
declaration order), and produces a textual diff focused on:

- Functions added / removed / changed signature
- Storage layout safety (append-only is safe; reorderings or removals are not)

Skipped in v1:
- Function body changes (would either explode the prompt or require a body hash
  signal that's hard to interpret).
- Inherited storage from base contracts (extractor sees the flat source bundle
  as fetched from Etherscan, which usually contains inherited contracts, but we
  don't follow inheritance ourselves).
- EIP-7201 namespaced storage layouts (flagged and layout check skipped).
"""

import re
from dataclasses import dataclass
from typing import Iterable

from utils.logger import get_logger
from utils.source_context import fetch_source

logger = get_logger("utils.impl_diff")

# function <name>(<args>) <modifiers/returns until { or ;>
# Args don't typically contain nested parens in Solidity, so `[^)]*` works.
_FUNCTION_DEF_RE = re.compile(
    r"\bfunction\s+(\w+)\s*\(([^)]*)\)([^{;]*)(?:\{|;)",
    re.MULTILINE,
)

# State variable declaration: <type> [visibility] [modifiers] <name> [= value];
# Visibility is OPTIONAL — Solidity defaults state vars to internal, so plain
# `uint256 cap;` is a valid storage declaration. To avoid matching function-local
# declarations like `uint256 x = 1;`, the caller filters matches by brace depth
# (only depth==1, inside a contract body but outside any function).
_STATE_VAR_RE = re.compile(
    r"((?:mapping\s*\([^)]+(?:\([^)]*\)[^)]*)*\))|(?:[A-Za-z_]\w*(?:\[[^\]]*\])?))"  # type
    r"((?:\s+(?:public|private|internal|external|immutable|constant|override(?:\s*\([^)]*\))?|virtual))*)"  # modifiers
    r"\s+"
    r"([A-Za-z_]\w*)"  # name
    r"\s*(?:=|;)",
)

# Solidity keywords that look like types but introduce non-state-var declarations
# at depth 1 (function/struct/enum/etc. headers). Skip these as "types".
_NON_TYPE_KEYWORDS = frozenset(
    {
        "function",
        "modifier",
        "constructor",
        "receive",
        "fallback",
        "struct",
        "enum",
        "event",
        "error",
        "using",
        "contract",
        "library",
        "interface",
        "abstract",
        "pragma",
        "import",
        "type",  # `type Foo is uint256;` user-defined value types — not a storage slot
        "return",
    }
)

_VISIBILITIES = frozenset({"public", "private", "internal", "external"})
_FUNCTION_KEYWORDS_TO_SKIP = frozenset(
    {"if", "for", "while", "modifier", "function", "constructor", "receive", "fallback"}
)


@dataclass(frozen=True)
class FunctionSig:
    name: str
    args: str  # raw arg list e.g. "address _a, uint256 _b"
    visibility: str  # "external" / "public" / "internal" / "private" or ""
    modifiers: str  # remaining tokens after visibility (view, payable, onlyOwner, etc.)


@dataclass(frozen=True)
class StateVarDecl:
    name: str
    type_str: str  # canonical-ish type, e.g. "uint256", "mapping(address => uint256)"
    visibility: str  # "public" / etc.
    immutable: bool  # True if `immutable` or `constant` (NOT a storage slot)


@dataclass(frozen=True)
class ImplDiff:
    old_addr: str
    new_addr: str
    old_name: str
    new_name: str
    added_functions: list[FunctionSig]
    removed_functions: list[FunctionSig]
    changed_functions: list[tuple[FunctionSig, FunctionSig]]
    added_state_vars: list[StateVarDecl]  # net additions at the end (append-only)
    removed_state_vars: list[StateVarDecl]
    layout_changes: list[str]  # human-readable list of incompatible changes
    storage_layout_safe: bool
    namespaced_storage: bool  # if true, layout check was skipped


def _normalize_args(args: str) -> str:
    """Strip param names, collapse whitespace — so `(uint256 a)` and `(uint256 b)` match."""
    parts: list[str] = []
    for raw in args.split(","):
        raw = raw.strip()
        if not raw:
            continue
        tokens = raw.split()
        # First token is the type. Subsequent: data location keyword(s) + param name.
        type_str = tokens[0]
        # Skip location markers if present
        idx = 1
        while idx < len(tokens) and tokens[idx] in {"memory", "calldata", "storage"}:
            idx += 1
        # remaining is param name (may be absent in interface declarations)
        parts.append(type_str)
    return ",".join(parts)


def _extract_function_sigs(source: str) -> list[FunctionSig]:
    """Find every `function <name>(<args>) <modifiers>` definition in source order."""
    sigs: list[FunctionSig] = []
    for m in _FUNCTION_DEF_RE.finditer(source):
        name = m.group(1)
        if name in _FUNCTION_KEYWORDS_TO_SKIP:
            continue
        args = _normalize_args(m.group(2))
        mods = m.group(3) or ""
        tokens = mods.split()
        visibility = ""
        other: list[str] = []
        for t in tokens:
            t_clean = t.rstrip("(")
            if t_clean in _VISIBILITIES and not visibility:
                visibility = t_clean
            else:
                other.append(t)
        sigs.append(
            FunctionSig(
                name=name,
                args=args,
                visibility=visibility,
                modifiers=" ".join(other).strip(),
            )
        )
    return sigs


_SOLIDITY_NOISE_RE = re.compile(
    r'/\*[\s\S]*?\*/|//[^\n]*|"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'',
)


def _strip_solidity_noise(source: str) -> str:
    """Replace comments and string literals with same-length whitespace.

    Preserving byte offsets keeps the brace-depth array indexable against the
    original source. Newlines are preserved so line numbers stay stable.
    """
    return _SOLIDITY_NOISE_RE.sub(lambda m: "".join("\n" if c == "\n" else " " for c in m.group(0)), source)


def _brace_depths(cleaned: str) -> list[int]:
    """Return a per-character array of brace nesting depth (post-character).

    Depth at index i is the brace depth *after* processing cleaned[i]. So a
    state var declaration matched at start position p has its lexical depth
    equal to depths[p - 1] (or 0 if p == 0).
    """
    depths = [0] * len(cleaned)
    depth = 0
    for i, c in enumerate(cleaned):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        depths[i] = depth
    return depths


def _extract_state_vars(source: str) -> list[StateVarDecl]:
    """Find every state-var declaration in source order.

    Captures default-internal vars (no visibility modifier) as well as explicit
    ones. Uses brace-depth tracking to exclude function-local declarations.
    """
    cleaned = _strip_solidity_noise(source)
    depths = _brace_depths(cleaned)

    vars_out: list[StateVarDecl] = []
    seen: set[tuple[str, str]] = set()
    for m in _STATE_VAR_RE.finditer(cleaned):
        # Determine the brace depth at the START of the match. State vars live
        # at depth == 1 (inside a contract/library/interface body, outside any
        # function/modifier/constructor body).
        start = m.start()
        depth_before = depths[start - 1] if start > 0 else 0
        if depth_before != 1:
            continue

        type_str = " ".join(m.group(1).split())
        modifier_block = m.group(2) or ""
        name = m.group(3)

        # Reject false positives where the regex matched a non-state-var keyword
        # as the "type" (e.g., `event Foo(...)` or `function bar(...)` if it
        # somehow slipped through). Most are blocked by the `(=|;)` terminator,
        # but `using X for Y;` and similar edge cases get filtered here.
        if type_str.split()[0] in _NON_TYPE_KEYWORDS:
            continue

        # Visibility token if present in the modifier block
        visibility = ""
        for tok in modifier_block.split():
            if tok in _VISIBILITIES:
                visibility = tok
                break

        immutable = "immutable" in modifier_block or "constant" in modifier_block

        key = (name, type_str)
        if key in seen:
            continue
        seen.add(key)

        vars_out.append(
            StateVarDecl(
                name=name,
                type_str=type_str,
                visibility=visibility,
                immutable=immutable,
            )
        )
    return vars_out


def _is_namespaced_storage(source: str) -> bool:
    """Heuristic: EIP-7201 contracts have a `_getXxxStorage()` returning a `storage $`."""
    return bool(
        re.search(
            r"function\s+_?[gG]et\w*Storage\b[^{]*\breturns\s*\([^)]*\bstorage\b[^)]*\$",
            source,
        )
    )


def _fkey(f: FunctionSig) -> tuple[str, str]:
    """Function identity for diffing: name + arg types (handles overloads)."""
    return (f.name, f.args)


def _diff_functions(
    old_fns: list[FunctionSig], new_fns: list[FunctionSig]
) -> tuple[list[FunctionSig], list[FunctionSig], list[tuple[FunctionSig, FunctionSig]]]:
    by_old = {_fkey(f): f for f in old_fns}
    by_new = {_fkey(f): f for f in new_fns}

    added = [new for k, new in by_new.items() if k not in by_old]
    removed = [old for k, old in by_old.items() if k not in by_new]
    changed: list[tuple[FunctionSig, FunctionSig]] = []
    for k in by_old.keys() & by_new.keys():
        old = by_old[k]
        new = by_new[k]
        if old.visibility != new.visibility or old.modifiers != new.modifiers:
            changed.append((old, new))
    return added, removed, changed


# An OZ-style trailing storage gap: `uintN[K] __gap;` (or `_gap`, `gap`).
# Reserved for future upgrades; consuming part of it is the canonical safe
# pattern, so we detach the trailing gap before comparing layouts.
_STORAGE_GAP_TYPE_RE = re.compile(r"^u?int\d*\s*\[\s*(\d+)\s*\]$")


def _gap_size(v: StateVarDecl) -> int | None:
    """If `v` looks like an OZ trailing storage gap, return its size. Else None."""
    if not v.name.lower().endswith("gap"):
        return None
    m = _STORAGE_GAP_TYPE_RE.match(v.type_str.replace(" ", ""))
    return int(m.group(1)) if m else None


def _detach_trailing_gap(slots: list[StateVarDecl]) -> tuple[list[StateVarDecl], int | None]:
    """Strip the trailing storage gap (if any) and return (slots_before_gap, gap_size)."""
    if not slots:
        return slots, None
    size = _gap_size(slots[-1])
    if size is None:
        return slots, None
    return slots[:-1], size


def _storage_layout(
    old_vars: list[StateVarDecl], new_vars: list[StateVarDecl]
) -> tuple[bool, list[str], list[StateVarDecl], list[StateVarDecl]]:
    """Return (safe, layout_changes, net_added, net_removed).

    Safe upgrade patterns:
      1. Append-only: new layout begins with the old layout (in the same order).
      2. OZ storage-gap consumption: trailing `uintN[K] __gap` shrinks by exactly
         the number of new vars inserted before it. Old contracts often reserve
         a gap so future upgrades can claim slots without shifting parent
         storage. Mis-handling this would produce false "unsafe" warnings for
         most real OpenZeppelin upgradeable contracts.

    Comparison key is (name, type) since rename or type change both shift the
    bytecode-level storage layout.
    """
    # Filter out immutable/constant — they don't occupy a storage slot
    old_slots = [v for v in old_vars if not v.immutable]
    new_slots = [v for v in new_vars if not v.immutable]

    # Detach trailing gaps so we can analyze gap consumption separately.
    old_core, old_gap = _detach_trailing_gap(old_slots)
    new_core, new_gap = _detach_trailing_gap(new_slots)

    changes: list[str] = []

    # Compare positions both contracts share. Any mismatch here is unsafe.
    n_common = min(len(old_core), len(new_core))
    for i in range(n_common):
        o, n = old_core[i], new_core[i]
        if (o.name, o.type_str) != (n.name, n.type_str):
            changes.append(f"slot {i}: {o.type_str} {o.name} → {n.type_str} {n.name}")

    consumed = len(new_core) - len(old_core)
    added_at_end = list(new_core[len(old_core) :]) if consumed > 0 else []
    removed_off_end = list(old_core[len(new_core) :]) if consumed < 0 else []

    if consumed > 0:
        changes.extend(_check_gap_consumption(consumed, old_gap, new_gap))
    elif consumed < 0:
        for i, v in enumerate(removed_off_end, start=len(new_core)):
            changes.append(f"slot {i}: removed {v.type_str} {v.name}")
    else:
        changes.extend(_check_gap_only_change(old_gap, new_gap))

    safe = not changes
    return safe, changes, added_at_end, removed_off_end


def _check_gap_consumption(consumed: int, old_gap: int | None, new_gap: int | None) -> list[str]:
    """Validate that `consumed` new vars correspond to a matching gap shrink.

    If the old contract had no gap, appending at the end is still safe (no shift).
    """
    if old_gap is None:
        return []
    expected_new_gap = old_gap - consumed
    if expected_new_gap < 0:
        return [f"consumed {consumed} new slot(s) but old gap was only {old_gap}; layout overflows reserved space"]
    if expected_new_gap == 0 and new_gap is not None:
        return [f"old gap of {old_gap} fully consumed but new contract still has gap of {new_gap}"]
    if expected_new_gap > 0 and new_gap is None:
        return [f"old gap of {old_gap} not preserved (expected new gap of {expected_new_gap}, got none)"]
    if expected_new_gap > 0 and new_gap != expected_new_gap:
        return [f"gap mismatch: consumed {consumed} slot(s); expected new gap of {expected_new_gap}, got {new_gap}"]
    return []


def _check_gap_only_change(old_gap: int | None, new_gap: int | None) -> list[str]:
    """Flag gap presence/size disagreements when the non-gap layout is unchanged."""
    if old_gap is not None and new_gap is None:
        return [f"old gap of size {old_gap} removed in new layout"]
    if old_gap is None and new_gap is not None:
        return [f"new layout introduces gap of size {new_gap} not present in old"]
    if old_gap != new_gap:
        return [f"gap size changed from {old_gap} to {new_gap} without slot consumption"]
    return []


def diff_implementations(old_addr: str, new_addr: str, chain_id: int) -> ImplDiff | None:
    """Fetch both verified impls and produce a structural diff. None on any failure."""
    old = fetch_source(chain_id, old_addr)
    new = fetch_source(chain_id, new_addr)
    if not old or not new:
        return None

    old_name, old_src = old
    new_name, new_src = new

    old_fns = _extract_function_sigs(old_src)
    new_fns = _extract_function_sigs(new_src)
    added_fns, removed_fns, changed_fns = _diff_functions(old_fns, new_fns)

    old_vars = _extract_state_vars(old_src)
    new_vars = _extract_state_vars(new_src)
    namespaced = _is_namespaced_storage(old_src) or _is_namespaced_storage(new_src)

    if namespaced:
        layout_safe = True
        layout_changes: list[str] = []
        added_at_end: list[StateVarDecl] = []
        removed_off_end: list[StateVarDecl] = []
    else:
        layout_safe, layout_changes, added_at_end, removed_off_end = _storage_layout(old_vars, new_vars)

    return ImplDiff(
        old_addr=old_addr,
        new_addr=new_addr,
        old_name=old_name,
        new_name=new_name,
        added_functions=added_fns,
        removed_functions=removed_fns,
        changed_functions=changed_fns,
        added_state_vars=added_at_end,
        removed_state_vars=removed_off_end,
        layout_changes=layout_changes,
        storage_layout_safe=layout_safe,
        namespaced_storage=namespaced,
    )


def _fmt_function(f: FunctionSig) -> str:
    parts = [f"{f.name}({f.args})"]
    if f.visibility:
        parts.append(f.visibility)
    if f.modifiers:
        parts.append(f.modifiers)
    return " ".join(parts)


def _section(title: str, items: Iterable[str]) -> str:
    items = list(items)
    if not items:
        return ""
    return f"{title} ({len(items)}):\n" + "\n".join(f"  {x}" for x in items)


def format_impl_diff(diff: ImplDiff) -> str:
    """Render an ImplDiff into a prompt-ready text block."""
    lines: list[str] = [
        f"Old: {diff.old_addr}" + (f" ({diff.old_name})" if diff.old_name else ""),
        f"New: {diff.new_addr}" + (f" ({diff.new_name})" if diff.new_name else ""),
    ]

    func_blocks: list[str] = []
    if diff.added_functions:
        func_blocks.append(_section("Functions added", (f"+ {_fmt_function(f)}" for f in diff.added_functions)))
    if diff.removed_functions:
        func_blocks.append(_section("Functions removed", (f"- {_fmt_function(f)}" for f in diff.removed_functions)))
    if diff.changed_functions:
        func_blocks.append(
            _section(
                "Functions with changed visibility/modifiers",
                (f"~ {_fmt_function(o)}  →  {_fmt_function(n)}" for o, n in diff.changed_functions),
            )
        )
    if func_blocks:
        lines.append("")
        lines.extend(func_blocks)

    if diff.namespaced_storage:
        lines.append("")
        lines.append("Storage layout: uses EIP-7201 namespaced storage; positional layout check skipped.")
    elif not diff.storage_layout_safe:
        lines.append("")
        lines.append("⚠ Storage layout NOT upgrade-safe:")
        lines.extend(f"  {c}" for c in diff.layout_changes)
    elif diff.added_state_vars:
        lines.append("")
        lines.append("Storage layout safe (append-only). New state vars at end:")
        for v in diff.added_state_vars:
            lines.append(f"  + {v.type_str} {v.visibility} {v.name}")
    else:
        lines.append("")
        lines.append("Storage layout: unchanged.")

    return "\n".join(lines)

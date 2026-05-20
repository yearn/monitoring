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

from utils.logging import get_logger
from utils.source_context import _fetch_source

logger = get_logger("utils.impl_diff")

# function <name>(<args>) <modifiers/returns until { or ;>
# Args don't typically contain nested parens in Solidity, so `[^)]*` works.
_FUNCTION_DEF_RE = re.compile(
    r"\bfunction\s+(\w+)\s*\(([^)]*)\)([^{;]*)(?:\{|;)",
    re.MULTILINE,
)

# State variable declaration: <type> <visibility> [modifiers] <name> [= value];
# Type can be a simple ident, mapping(...), or an array type. We avoid matching
# function-local declarations by requiring a visibility modifier.
_STATE_VAR_RE = re.compile(
    r"(?:^|\n)\s*"
    r"((?:mapping\s*\([^)]+(?:\([^)]*\)[^)]*)*\))|(?:[A-Za-z_]\w*(?:\[[^\]]*\])?))"
    r"\s+"
    r"(public|private|internal|external)"
    r"((?:\s+(?:immutable|constant|override(?:\s*\([^)]*\))?|virtual))*)"
    r"\s+"
    r"([A-Za-z_]\w*)"
    r"\s*(?:=|;)",
    re.MULTILINE,
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


def _extract_state_vars(source: str) -> list[StateVarDecl]:
    """Find every state-var declaration in source order. Excludes function-local vars."""
    vars_out: list[StateVarDecl] = []
    seen: set[tuple[str, str]] = set()  # (name, type) — dedup repeated decls across inheritance
    for m in _STATE_VAR_RE.finditer(source):
        type_str = " ".join(m.group(1).split())  # collapse whitespace
        visibility = m.group(2)
        modifier_block = m.group(3) or ""
        name = m.group(4)
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


def _storage_layout(
    old_vars: list[StateVarDecl], new_vars: list[StateVarDecl]
) -> tuple[bool, list[str], list[StateVarDecl], list[StateVarDecl]]:
    """Return (safe, layout_changes, net_added, net_removed).

    Safe ⇔ the new layout starts with the old layout in the same order (append-only).
    Comparison key is (name, type) since rename or type change both shift bytecode-level layout.
    """
    # Filter out immutable/constant — they don't occupy a storage slot
    old_slots = [v for v in old_vars if not v.immutable]
    new_slots = [v for v in new_vars if not v.immutable]

    changes: list[str] = []
    n_common = min(len(old_slots), len(new_slots))
    for i in range(n_common):
        o, n = old_slots[i], new_slots[i]
        if (o.name, o.type_str) != (n.name, n.type_str):
            changes.append(f"slot {i}: {o.type_str} {o.name} → {n.type_str} {n.name}")

    if len(new_slots) < len(old_slots):
        for i in range(n_common, len(old_slots)):
            v = old_slots[i]
            changes.append(f"slot {i}: removed {v.type_str} {v.name}")

    added_at_end = [v for v in new_slots[n_common:]] if len(new_slots) > len(old_slots) else []
    removed_off_end = [v for v in old_slots[n_common:]] if len(old_slots) > len(new_slots) else []

    safe = not changes
    return safe, changes, added_at_end, removed_off_end


def diff_implementations(old_addr: str, new_addr: str, chain_id: int) -> ImplDiff | None:
    """Fetch both verified impls and produce a structural diff. None on any failure."""
    old = _fetch_source(chain_id, old_addr)
    new = _fetch_source(chain_id, new_addr)
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

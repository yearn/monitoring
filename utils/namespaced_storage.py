"""Check the storage a proxy upgrade touches outside the positional layout.

The compiler's ``storageLayout`` describes positional state variables only. A
namespaced struct at a hashed root, a hand-rolled ``s.slot := ROOT`` accessor, a
raw ``sstore`` or a ``delegatecall`` never appear in it — so a clean positional
comparison says nothing about them. This module reads the code that can run
against the proxy's storage (``utils.storage_scope``) and sorts what it finds
into three buckets:

- **conflicts** — proven incompatibilities: a storage root that moved between
  versions, or two different structs aimed at the same root;
- **gaps** (``unvalidated``) — storage that exists but can't be validated here;
- **unchanged** — ERC-7201 namespaces proven identical.

A namespace is proven unchanged only when, on both sides, it has the same id,
an identical struct definition over elementary member types (a user-defined
member type could change beneath identical text), and every accessor for it
resolves to the root its annotation defines. ERC-7201 leaves enforcing the
declared namespace to the developer, so the annotation alone proves nothing.

What this can't do without a compiler — compare namespace struct layouts that
changed, or follow storage references through helpers — stays a gap.
"""

import re
from dataclasses import dataclass, field

from utils.solidity_text import namespaced_structs
from utils.storage_access import StorageAccess, erc7201_root, find_storage_access
from utils.storage_scope import ScopedFunction, scope_owners, storage_scope
from utils.verified_contract import VerifiedContract

_ELEMENTARY_TYPE_RE = re.compile(r"^(?:u?int\d*|bytes\d*|bool|address|string)$")
_TYPE_KEYWORDS = frozenset({"mapping", "payable"})
_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_STRUCT_NAME_RE = re.compile(r"^struct\s+(\w+)")


@dataclass(frozen=True)
class Namespace:
    """One ERC-7201 namespace as a contract version declares it."""

    namespace_id: str
    owners: tuple[str, ...]  # contract(s)/library(ies) declaring it — more than one is ambiguous
    definitions: tuple[str, ...]  # normalized struct text per declaration
    roots: tuple[int | None, ...] = ()  # root of every accessor returning this struct; None = unresolved


@dataclass(frozen=True)
class NamespaceComparison:
    """Non-positional storage, sorted into proven, unvalidated and unchanged."""

    unchanged: list[str] = field(default_factory=list)
    unvalidated: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def is_validated(self) -> bool:
        return not self.unvalidated and not self.conflicts


def compare_non_positional(old: VerifiedContract, new: VerifiedContract) -> NamespaceComparison:
    """Everything outside the positional layout, for one upgrade."""
    old_scope, new_scope = storage_scope(old), storage_scope(new)
    if old_scope is None or new_scope is None:
        return NamespaceComparison(
            unvalidated=["non-positional storage: could not resolve the deployed contract to find the code in scope"]
        )
    old_access = find_storage_access(old, old_scope)
    new_access = find_storage_access(new, new_scope)
    old_ns = _collect(old, old_scope, old_access)
    new_ns = _collect(new, new_scope, new_access)

    namespaces = compare_namespaces(old_ns, new_ns)
    namespaced_structs_ = {_struct_name(d) for ns in (*old_ns.values(), *new_ns.values()) for d in ns.definitions}
    accessors = _compare_accessors(old_access, new_access, namespaced_structs_)
    return NamespaceComparison(
        unchanged=namespaces.unchanged,
        unvalidated=namespaces.unvalidated + accessors.unvalidated + _raw_gaps(old_access, new_access),
        conflicts=namespaces.conflicts + accessors.conflicts + _shared_roots(new_access),
    )


def collect_namespaces(contract: VerifiedContract) -> dict[str, Namespace] | None:
    """ERC-7201 namespaces declared by code in the storage scope, keyed by id.

    None when the deployed contract's own file can't be resolved.
    """
    scope = storage_scope(contract)
    if scope is None:
        return None
    return _collect(contract, scope, find_storage_access(contract, scope))


def compare_namespaces(old: dict[str, Namespace] | None, new: dict[str, Namespace] | None) -> NamespaceComparison:
    """Prove each namespace unchanged, prove it moved, or say why neither."""
    if old is None or new is None:
        return NamespaceComparison(
            unvalidated=["namespaced storage: could not resolve the deployed contract to walk its bases"]
        )

    unchanged: list[str] = []
    unvalidated: list[str] = []
    conflicts: list[str] = []
    for namespace_id in sorted(set(old) | set(new)):
        before, after = old.get(namespace_id), new.get(namespace_id)
        moved = _moved_root(before, after)
        if moved:
            conflicts.append(f"namespace {namespace_id}: {moved}")
            continue
        reason = _unvalidated_reason(namespace_id, before, after)
        if reason is None:
            unchanged.append(namespace_id)
        else:
            unvalidated.append(f"namespace {namespace_id}: {reason}")
    return NamespaceComparison(unchanged=unchanged, unvalidated=unvalidated, conflicts=conflicts)


def _collect(contract: VerifiedContract, scope: list[ScopedFunction], access: StorageAccess) -> dict[str, Namespace]:
    found: dict[str, list[tuple[str, str]]] = {}
    for owner, path in scope_owners(contract, scope):
        for namespace_id, definition in namespaced_structs(contract.sources[path], owner).items():
            found.setdefault(namespace_id, []).append((owner, definition))

    namespaces: dict[str, Namespace] = {}
    for namespace_id, entries in found.items():
        structs = {_struct_name(definition) for _, definition in entries}
        namespaces[namespace_id] = Namespace(
            namespace_id=namespace_id,
            owners=tuple(owner for owner, _ in entries),
            definitions=tuple(definition for _, definition in entries),
            roots=tuple(a.root for a in access.assignments if a.struct in structs),
        )
    return namespaces


def _moved_root(old: Namespace | None, new: Namespace | None) -> str | None:
    """A proven root change: both sides resolved, and they differ."""
    if old is None or new is None:
        return None
    before, after = _resolved(old.roots), _resolved(new.roots)
    if before is None or after is None or before == after:
        return None
    return f"storage root changed from {_hex(before)} to {_hex(after)} — existing data is no longer read"


def _unvalidated_reason(namespace_id: str, old: Namespace | None, new: Namespace | None) -> str | None:
    """Why a namespace can't be proven unchanged, or None if it can."""
    if old is None:
        return "added in the new implementation; its layout was not compared"
    if new is None:
        return "no longer declared by the new implementation"
    if len(set(old.definitions)) > 1 or len(set(new.definitions)) > 1:
        return "declared differently in more than one place; cannot tell which applies"
    root_problem = _root_problem(namespace_id, old) or _root_problem(namespace_id, new)
    if root_problem:
        return root_problem
    if old.definitions[0] != new.definitions[0]:
        return f"struct definition changed (declared by {', '.join(sorted(set(new.owners)))})"
    if not _uses_only_elementary_types(new.definitions[0]):
        return "definition unchanged, but it references user-defined types whose layout was not checked"
    return None


def _root_problem(namespace_id: str, namespace: Namespace) -> str | None:
    """Whether this side's accessors verifiably use the annotated root."""
    if not namespace.roots:
        return "no accessor for its struct was found, so the root it actually uses is unknown"
    roots = _resolved(namespace.roots)
    if roots is None:
        return "an accessor's root is written in a form that could not be resolved"
    formula, _, name = namespace_id.partition(":")
    if formula != "erc7201":
        return f"uses the {formula!r} formula, whose root is not verified here"
    expected = erc7201_root(name)
    if roots != {expected}:
        return f"accessor root {_hex(roots)} does not match its annotation ({_hex({expected})})"
    return None


def _resolved(roots: tuple[int | None, ...]) -> set[int] | None:
    """The distinct roots, or None if there are none or any is unresolved."""
    if not roots or any(root is None for root in roots):
        return None
    return {root for root in roots if root is not None}


def _compare_accessors(old: StorageAccess, new: StorageAccess, namespaced: set[str]) -> NamespaceComparison:
    """Accessors not tied to an ERC-7201 namespace: pair by owner and function."""
    old_by = {a.where: a for a in old.assignments if a.struct not in namespaced}
    new_by = {a.where: a for a in new.assignments if a.struct not in namespaced}
    unvalidated: list[str] = []
    conflicts: list[str] = []
    for where in sorted(set(old_by) | set(new_by)):
        before, after = old_by.get(where), new_by.get(where)
        if before and after and before.root is not None and after.root is not None and before.root != after.root:
            conflicts.append(
                f"storage accessor {where}: root changed from {_hex({before.root})} to {_hex({after.root})} — "
                "existing data is no longer read"
            )
            continue
        present = after if after is not None else before
        subject = present.struct if present is not None else None
        what = f"struct {subject}" if subject else "a storage pointer"
        unvalidated.append(f"storage accessor {where} aims {what} at a custom root; its layout is not validated")
    return NamespaceComparison(unvalidated=unvalidated, conflicts=conflicts)


def _raw_gaps(old: StorageAccess, new: StorageAccess) -> list[str]:
    """Raw storage access on either side — none of it is describable by a layout."""
    seen: dict[tuple[str, str, str], None] = {}
    for access in (*new.raw, *old.raw):
        seen.setdefault((access.owner, access.function, access.kind), None)
    explain = {
        "sload/sstore": "reads/writes storage at a computed slot",
        "delegatecall": "delegatecalls other code, which then runs against this contract's storage",
    }
    return [f"{owner}.{function} {explain[kind]} ({kind})" for owner, function, kind in seen]


def _shared_roots(access: StorageAccess) -> list[str]:
    """Two different structs aimed at the same resolved root corrupt each other."""
    by_root: dict[int, set[str]] = {}
    for a in access.assignments:
        if a.root is not None and a.struct:
            by_root.setdefault(a.root, set()).add(a.struct)
    return [
        f"storage root {_hex({root})} is shared by different structs ({', '.join(sorted(structs))})"
        for root, structs in sorted(by_root.items())
        if len(structs) > 1
    ]


def _struct_name(definition: str) -> str:
    match = _STRUCT_NAME_RE.match(definition)
    return match.group(1) if match else ""


def _hex(roots: set[int]) -> str:
    return ", ".join(f"0x{root:064x}" for root in sorted(roots))


def _uses_only_elementary_types(definition: str) -> bool:
    """True if every member type in a struct definition is elementary.

    Mappings and arrays of elementary types qualify. A contract, enum, struct or
    user-defined value type does not — its own definition could change while the
    text here stays identical.
    """
    body = definition[definition.find("{") + 1 : definition.rfind("}")]
    for member in body.split(";"):
        tokens = _IDENTIFIER_RE.findall(member)
        # The last identifier is the member's name; everything before it is type.
        for token in tokens[:-1]:
            if token not in _TYPE_KEYWORDS and not _ELEMENTARY_TYPE_RE.match(token):
                return False
    return True

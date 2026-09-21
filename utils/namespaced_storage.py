"""Check ERC-7201 namespaced storage across a proxy upgrade.

Namespaced structs live at a hashed slot and never appear in the compiler's
positional ``storageLayout``, so a clean positional comparison says nothing about
them. A namespace changing its struct under the same id garbles live data just as
surely as a reordered state variable.

Where namespaces come from: ERC-7201 annotates the *struct* inside the contract
(``/// @custom:storage-location erc7201:<id>``), and the struct is frequently
declared by an inherited base rather than by the deployed contract itself —
OpenZeppelin v5's ``Initializable`` is inherited by nearly every upgradeable
contract. So the deployed contract and its full inheritance chain are scanned.
Libraries and interfaces that are merely imported are not: an imported helper's
namespace is what used to suppress the positional check entirely (see
yearn/monitoring#367).

What counts as validated: a namespace is *provably unchanged* when both sides
declare the same id with an identical struct definition whose members use only
elementary types. Identical text over elementary types means identical layout;
a user-defined member type could change underneath identical text, so it is not
accepted. Everything else — an added, removed, edited or ambiguous namespace —
is reported unvalidated, which keeps the storage verdict at UNKNOWN. Nothing
here ever declares a namespace *incompatible*: that would need a real layout.
"""

import re
from dataclasses import dataclass, field

from utils.solidity_text import declared_names, namespaced_structs, parent_names
from utils.verified_contract import VerifiedContract

_ELEMENTARY_TYPE_RE = re.compile(r"^(?:u?int\d*|bytes\d*|bool|address|string)$")
_TYPE_KEYWORDS = frozenset({"mapping", "payable"})
_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")


@dataclass(frozen=True)
class Namespace:
    """One ERC-7201 namespace as a contract version declares it."""

    namespace_id: str
    owners: tuple[str, ...]  # contract(s) declaring it — more than one is ambiguous
    definitions: tuple[str, ...]  # normalized struct text per declaration


@dataclass(frozen=True)
class NamespaceComparison:
    """Which namespaces were proven unchanged, and which could not be."""

    unchanged: list[str] = field(default_factory=list)
    unvalidated: list[str] = field(default_factory=list)

    @property
    def is_validated(self) -> bool:
        return not self.unvalidated


def collect_namespaces(contract: VerifiedContract) -> dict[str, Namespace] | None:
    """Namespaces the deployed contract and its bases declare, keyed by id.

    None when the deployed contract's own file can't be resolved — without the
    target there is no inheritance chain to walk.
    """
    if not contract.contract_file:
        return None

    found: dict[str, list[tuple[str, str]]] = {}
    for name, path in _inheritance_chain(contract, contract.contract_file):
        for namespace_id, definition in namespaced_structs(contract.sources[path], name).items():
            found.setdefault(namespace_id, []).append((name, definition))

    return {
        namespace_id: Namespace(
            namespace_id=namespace_id,
            owners=tuple(owner for owner, _ in entries),
            definitions=tuple(definition for _, definition in entries),
        )
        for namespace_id, entries in found.items()
    }


def compare_namespaces(old: dict[str, Namespace] | None, new: dict[str, Namespace] | None) -> NamespaceComparison:
    """Prove each namespace unchanged, or say why it couldn't be."""
    if old is None or new is None:
        return NamespaceComparison(
            unvalidated=["namespaced storage: could not resolve the deployed contract to walk its bases"]
        )

    unchanged: list[str] = []
    unvalidated: list[str] = []
    for namespace_id in sorted(set(old) | set(new)):
        reason = _unvalidated_reason(old.get(namespace_id), new.get(namespace_id))
        if reason is None:
            unchanged.append(namespace_id)
        else:
            unvalidated.append(f"namespace {namespace_id}: {reason}")
    return NamespaceComparison(unchanged=unchanged, unvalidated=unvalidated)


def _unvalidated_reason(old: Namespace | None, new: Namespace | None) -> str | None:
    """Why a namespace can't be proven unchanged, or None if it can."""
    if old is None:
        return "added in the new implementation; its layout was not compared"
    if new is None:
        return "no longer declared by the new implementation"
    if len(set(old.definitions)) > 1 or len(set(new.definitions)) > 1:
        return "declared differently in more than one base; cannot tell which applies"
    if old.definitions[0] != new.definitions[0]:
        return f"struct definition changed (declared by {', '.join(sorted(set(new.owners)))})"
    if not _uses_only_elementary_types(new.definitions[0]):
        return "definition unchanged, but it references user-defined types whose layout was not checked"
    return None


def _inheritance_chain(contract: VerifiedContract, target_file: str) -> list[tuple[str, str]]:
    """(contract name, file) for the deployed contract and every transitive base.

    The deployed contract comes from its resolved ``target_file``. A base name
    declared in several files is taken from all of them — over-inclusion can
    only add namespaces to check, which fails toward UNKNOWN, never toward a
    false pass.
    """
    declared_in: dict[str, list[str]] = {}
    for path, source in contract.sources.items():
        for name in declared_names(source):
            declared_in.setdefault(name, []).append(path)

    chain: list[tuple[str, str]] = []
    seen: set[str] = set()
    pending: list[tuple[str, list[str]]] = [(contract.contract_name, [target_file])]
    while pending:
        name, paths = pending.pop(0)
        if name in seen:
            continue
        seen.add(name)
        for path in paths:
            chain.append((name, path))
            for base in parent_names(contract.sources[path], name):
                pending.append((base, declared_in.get(base, [])))
    return chain


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

"""Find storage the positional layout can't see, and resolve where it lives.

Two kinds of access are collected from the storage scope (``utils.storage_scope``):

- **Slot assignments** — ``$.slot := ROOT`` in assembly, pointing a storage
  struct at a chosen root. This is how ERC-7201 namespaces and older
  "diamond storage" patterns work. The root is resolved when it is written in a
  supported form; the struct is known when the pointer is the function's
  ``returns (S storage $)`` value.
- **Raw access** — ``sload``/``sstore`` on a computed slot, or ``delegatecall``
  into other code that then runs against the proxy's storage. Neither can be
  checked here; each is reported as a coverage gap.

Root resolution is deliberately bounded. Supported: a numeric literal; a
``bytes32`` constant (unique across the bundle); a local ``bytes32`` in the
accessor that is never reassigned; a zero-argument getter whose body is
``return <expr>;`` (every override must agree); and three standard hash forms —
``keccak256("id")``, ``bytes32(uint256(keccak256("id")) - 1)`` (EIP-1967) and
the ERC-7201 formula. Anything else is unresolved, never guessed.

Structs are identified by where they are declared, not by bare name:
``Vault.Main`` and ``Lib.Main`` are different storage, and matching on ``Main``
alone let one borrow the other's validation.
"""

import re
from dataclasses import dataclass

from eth_utils import keccak

from utils.solidity_text import declarations, import_aliases, strip_comments, struct_definitions, struct_member_types
from utils.storage_scope import ScopedFunction, inheritance_chain
from utils.verified_contract import VerifiedContract

_SLOT_ASSIGN_RE = re.compile(
    r"([A-Za-z_$][\w$]*)\.slot\s*:=\s*([A-Za-z_]\w*\s*\(\s*\)|0x[0-9a-fA-F]+|\d+|[A-Za-z_]\w*)"
)
_RETURNS_STORAGE_RE = re.compile(r"returns\s*\(\s*([\w.]+)\s+storage\s+([A-Za-z_$][\w$]*)\s*\)")
_RAW_ACCESS = (
    ("sload/sstore", re.compile(r"\bs(?:load|store)\s*\(")),
    ("delegatecall", re.compile(r"\bdelegatecall\b")),
)

_CONSTANT_RE = re.compile(
    r"\bbytes32\s+(?:(?:private|internal|public)\s+)?constant\s+(?:(?:private|internal|public)\s+)?"
    r"([A-Za-z_]\w*)\s*=\s*([^;]+);"
)
_LOCAL_RE_TEMPLATE = r"\bbytes32\s+{name}\s*=\s*([^;]+);"

_STRING = r'"([^"\\]*)"'
_KECCAK_STRING = rf"keccak256\s*\(\s*(?:bytes\s*\(\s*)?{_STRING}\s*\)?\s*\)"
_EIP1967_RE = re.compile(rf"^bytes32\s*\(\s*uint256\s*\(\s*{_KECCAK_STRING}\s*\)\s*-\s*1\s*\)$")
_ERC7201_RE = re.compile(
    rf"^keccak256\s*\(\s*abi\.encode\s*\(\s*uint256\s*\(\s*{_KECCAK_STRING}\s*\)\s*-\s*1\s*\)\s*\)"
    r"\s*&\s*~\s*bytes32\s*\(\s*uint256\s*\(\s*0xff\s*\)\s*\)$"
)
_KECCAK_RE = re.compile(rf"^{_KECCAK_STRING}$")
_GETTER_CALL_RE = re.compile(r"^([A-Za-z_]\w*)\s*\(\s*\)$")
_RETURN_RE = re.compile(r"^return\s+([^;]+);$")

_MAX_DEPTH = 6
_WORD = 1 << 256


@dataclass(frozen=True)
class SlotAssignment:
    """A storage pointer aimed at a chosen root."""

    owner: str
    function: str
    struct: str | None  # storage struct type as written, when the pointer is the function's return value
    root: int | None  # resolved root, None when written in an unsupported form
    struct_owner: str | None = None  # contract/library (or file) declaring the struct, None if unresolved
    struct_shape: tuple[str, ...] | None = None  # the struct's member types, names dropped

    @property
    def where(self) -> str:
        return f"{self.owner}.{self.function}"

    @property
    def struct_key(self) -> str | None:
        """``Owner.Struct`` — the declaration this pointer uses, or None if unresolved."""
        if self.struct is None or self.struct_owner is None:
            return None
        return f"{self.struct_owner}.{self.struct.split('.')[-1]}"


@dataclass(frozen=True)
class RawAccess:
    """Storage access no layout describes: a computed-slot sload/sstore or a delegatecall."""

    owner: str
    function: str
    kind: str  # "sload/sstore" or "delegatecall"


@dataclass(frozen=True)
class StorageAccess:
    assignments: list[SlotAssignment]
    raw: list[RawAccess]


def erc7201_root(namespace_id: str) -> int:
    """The ERC-7201 root for a namespace id (the part after ``erc7201:``).

    ``keccak256(abi.encode(uint256(keccak256(id)) - 1)) & ~bytes32(uint256(0xff))``
    """
    inner = (int.from_bytes(keccak(text=namespace_id), "big") - 1) % _WORD
    return int.from_bytes(keccak(inner.to_bytes(32, "big")), "big") & ~0xFF


def find_storage_access(contract: VerifiedContract, scope: list[ScopedFunction]) -> StorageAccess:
    """Collect slot assignments (with resolved roots) and raw access in ``scope``."""
    constants = _bundle_constants(contract)
    getters = _getters(scope)
    structs = _StructResolver(contract)
    assignments: list[SlotAssignment] = []
    raw: list[RawAccess] = []
    for scoped in scope:
        body = scoped.fn.body
        returned = _RETURNS_STORAGE_RE.search(scoped.fn.header)
        for pointer, expr in _SLOT_ASSIGN_RE.findall(body):
            written = returned.group(1) if returned and returned.group(2) == pointer else None
            origin, definition = structs.resolve(scoped, written) if written else (None, None)
            assignments.append(
                SlotAssignment(
                    owner=scoped.owner,
                    function=scoped.fn.name,
                    struct=written,
                    root=_resolve(expr, body, constants, getters, 0),
                    struct_owner=origin,
                    struct_shape=struct_member_types(definition) if definition else None,
                )
            )
        for kind, pattern in _RAW_ACCESS:
            if pattern.search(body):
                raw.append(RawAccess(scoped.owner, scoped.fn.name, kind))
    return StorageAccess(assignments=assignments, raw=raw)


class _StructResolver:
    """Find which declaration a struct type name refers to, from where it is used.

    Solidity's lookup, bounded: a qualified ``Q.Main`` names ``Q``'s struct (read
    through the file's import aliases); a bare ``Main`` in a library is the
    library's own; in a contract it is the one struct of that name anywhere in
    the inheritance chain (Solidity forbids redeclaring one along a chain);
    otherwise a file-level struct in the same file. Anything else is unresolved.
    """

    def __init__(self, contract: VerifiedContract) -> None:
        self._contract = contract
        self._chain = inheritance_chain(contract, contract.contract_file) if contract.contract_file else []
        self._declared_in: dict[str, list[str]] = {}
        for path, source in contract.sources.items():
            for name, _ in declarations(source):
                self._declared_in.setdefault(name, []).append(path)

    def resolve(self, scoped: ScopedFunction, written: str) -> tuple[str | None, str | None]:
        """(declaring owner, normalized definition) for ``written`` used in ``scoped``."""
        qualifier, _, name = written.rpartition(".")
        if qualifier:
            owner = import_aliases(self._contract.sources[scoped.path]).get(qualifier, qualifier)
            return self._unique(owner, name)
        if scoped.owner_kind == "library":
            found = self._in(scoped.path, scoped.owner, name)
            if found:
                return scoped.owner, found
        elif scoped.owner_kind == "contract":
            hits = [(owner, d) for owner, path in self._chain if (d := self._in(path, owner, name))]
            if len(hits) == 1:
                return hits[0]
            if hits:
                return None, None
        file_level = self._in(scoped.path, None, name)
        return (scoped.path, file_level) if file_level else (None, None)

    def _unique(self, owner: str, name: str) -> tuple[str | None, str | None]:
        paths = self._declared_in.get(owner, [])
        if len(paths) != 1:
            return None, None
        found = self._in(paths[0], owner, name)
        return (owner, found) if found else (None, None)

    def _in(self, path: str, owner: str | None, name: str) -> str | None:
        return struct_definitions(self._contract.sources[path], owner).get(name)


def _bundle_constants(contract: VerifiedContract) -> dict[str, str | None]:
    """``bytes32`` constant name → expression; None when declared inconsistently."""
    out: dict[str, str | None] = {}
    for source in contract.sources.values():
        for name, expr in _CONSTANT_RE.findall(strip_comments(source)):
            expr = " ".join(expr.split())
            out[name] = expr if out.get(name, expr) == expr else None
    return out


def _getters(scope: list[ScopedFunction]) -> dict[str, list[str]]:
    """Zero-argument function name → the returned expression of each definition.

    A definition that isn't a single ``return <expr>;`` contributes an empty
    entry, which makes the getter unresolvable — an override doing anything
    cleverer must not be assumed to return the base's constant.
    """
    out: dict[str, list[str]] = {}
    for scoped in scope:
        if scoped.fn.params or not scoped.fn.has_body:
            continue
        match = _RETURN_RE.match(scoped.fn.body.strip())
        out.setdefault(scoped.fn.name, []).append(match.group(1).strip() if match else "")
    return out


def _resolve(
    expr: str, body: str, constants: dict[str, str | None], getters: dict[str, list[str]], depth: int
) -> int | None:
    """Evaluate a root expression in one of the supported forms, else None."""
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        expr = expr[1:-1].strip()
    if depth > _MAX_DEPTH or not expr:
        return None
    if re.fullmatch(r"0x[0-9a-fA-F]+", expr):
        return int(expr, 16)
    if expr.isdigit():
        return int(expr)
    for pattern, derive in (
        (_ERC7201_RE, erc7201_root),
        (_EIP1967_RE, lambda s: (_keccak_int(s) - 1) % _WORD),
        (_KECCAK_RE, _keccak_int),
    ):
        match = pattern.match(expr)
        if match:
            return derive(match.group(1))
    getter = _GETTER_CALL_RE.match(expr)
    if getter:
        values = {_resolve(e, "", constants, getters, depth + 1) for e in getters.get(getter.group(1), [""])}
        return values.pop() if len(values) == 1 else None
    if re.fullmatch(r"[A-Za-z_]\w*", expr):
        local = re.search(_LOCAL_RE_TEMPLATE.format(name=re.escape(expr)), body)
        if local:
            # The initializer only holds if nothing overwrites the local before
            # the slot assignment; without flow analysis, any write disqualifies it.
            if _is_reassigned(expr, body):
                return None
            return _resolve(local.group(1), body, constants, getters, depth + 1)
        constant = constants.get(expr)
        # A constant is never assigned, so any write to this name in the body is
        # a local (`let ROOT := …`, `uint256 ROOT = …`) shadowing the constant.
        if constant and not re.search(rf"(?<![\w.$]){re.escape(expr)}\s*(?::=|=(?!=))", body):
            return _resolve(constant, "", constants, getters, depth + 1)
    return None


def _is_reassigned(name: str, body: str) -> bool:
    """True if ``name`` is written anywhere besides its declaration.

    Covers Solidity assignment and compound assignment (``root = …``,
    ``root += …``), Yul assignment (``root := …``), tuple destructuring
    (``(root, x) = …``) and ``delete root`` (which zeroes it). The declaration
    itself accounts for one assignment match.
    """
    n = re.escape(name)
    writes = re.findall(rf"(?<![\w.$]){n}\s*(?::=|(?:<<|>>|[-+*/%&|^])?=(?!=))", body)
    destructured = re.search(rf"\([^()]*(?<![\w.$]){n}\b[^()]*\)\s*=(?!=)", body)
    deleted = re.search(rf"\bdelete\s+{n}\b", body)
    return len(writes) > 1 or destructured is not None or deleted is not None


def _keccak_int(text: str) -> int:
    return int.from_bytes(keccak(text=text), "big")

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
accessor; a zero-argument getter whose body is ``return <expr>;`` (every
override must agree); and three standard hash forms —
``keccak256("id")``, ``bytes32(uint256(keccak256("id")) - 1)`` (EIP-1967) and
the ERC-7201 formula. Anything else is unresolved, never guessed.
"""

import re
from dataclasses import dataclass

from eth_utils import keccak

from utils.solidity_text import strip_comments
from utils.storage_scope import ScopedFunction
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
    struct: str | None  # storage struct type, when the pointer is the function's return value
    root: int | None  # resolved root, None when written in an unsupported form

    @property
    def where(self) -> str:
        return f"{self.owner}.{self.function}"


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
    assignments: list[SlotAssignment] = []
    raw: list[RawAccess] = []
    for scoped in scope:
        body = scoped.fn.body
        returned = _RETURNS_STORAGE_RE.search(scoped.fn.header)
        for pointer, expr in _SLOT_ASSIGN_RE.findall(body):
            struct = returned.group(1).split(".")[-1] if returned and returned.group(2) == pointer else None
            root = _resolve(expr, body, constants, getters, 0)
            assignments.append(SlotAssignment(scoped.owner, scoped.fn.name, struct, root))
        for kind, pattern in _RAW_ACCESS:
            if pattern.search(body):
                raw.append(RawAccess(scoped.owner, scoped.fn.name, kind))
    return StorageAccess(assignments=assignments, raw=raw)


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
            return _resolve(local.group(1), body, constants, getters, depth + 1)
        constant = constants.get(expr)
        if constant:
            return _resolve(constant, "", constants, getters, depth + 1)
    return None


def _keccak_int(text: str) -> int:
    return int.from_bytes(keccak(text=text), "big")

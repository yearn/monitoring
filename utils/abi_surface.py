"""Diff the external surface of two contracts from their verified ABIs.

The ABI is the authoritative statement of what a contract exposes: it carries
generated public getters, canonical tuple/array types and nothing else — no
interface declarations, no commented-out code, no internal or private
functions. Deriving the surface diff from it is what keeps an imported
interface's declaration from being reported as a new entry point on the
deployed contract.

Signatures are keyed canonically (``name(type,type)``), so overloads are
distinct entries and a renamed parameter is not a change.
"""

from dataclasses import dataclass

from eth_utils.abi import collapse_if_tuple


@dataclass(frozen=True)
class AbiFunction:
    """One externally reachable entry point."""

    signature: str  # canonical "name(type,type)", or "fallback()" / "receive()"
    state_mutability: str  # "pure" / "view" / "nonpayable" / "payable"

    def __str__(self) -> str:
        return f"{self.signature} [{self.state_mutability}]" if self.state_mutability else self.signature


@dataclass(frozen=True)
class AbiSurfaceDiff:
    """Additions, removals and mutability changes between two ABIs."""

    added: list[AbiFunction]
    removed: list[AbiFunction]
    mutability_changed: list[tuple[AbiFunction, AbiFunction]]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.mutability_changed)


def abi_functions(abi: list[dict]) -> dict[str, AbiFunction]:
    """Canonical signature -> entry point, for every callable ABI entry."""
    out: dict[str, AbiFunction] = {}
    for entry in abi:
        if not isinstance(entry, dict):
            continue
        signature = _canonical_signature(entry)
        if signature:
            out[signature] = AbiFunction(signature, entry.get("stateMutability") or "")
    return out


def diff_abi_surface(old_abi: list[dict], new_abi: list[dict]) -> AbiSurfaceDiff:
    """Compare two ABIs and return the external-surface delta."""
    old = abi_functions(old_abi)
    new = abi_functions(new_abi)

    added = [new[sig] for sig in sorted(set(new) - set(old))]
    removed = [old[sig] for sig in sorted(set(old) - set(new))]
    changed = [
        (old[sig], new[sig])
        for sig in sorted(set(old) & set(new))
        if old[sig].state_mutability != new[sig].state_mutability
    ]
    return AbiSurfaceDiff(added=added, removed=removed, mutability_changed=changed)


def _canonical_signature(entry: dict) -> str | None:
    """Canonical signature for an ABI entry, or None if it isn't callable."""
    kind = entry.get("type")
    if kind in ("fallback", "receive"):
        return f"{kind}()"
    if kind != "function":
        return None
    name = entry.get("name")
    if not name:
        return None
    inputs = entry.get("inputs") or []
    try:
        types = ",".join(collapse_if_tuple(inp) for inp in inputs)
    except (KeyError, TypeError):  # malformed ABI entry — skip rather than guess
        return None
    return f"{name}({types})"

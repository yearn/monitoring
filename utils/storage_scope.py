"""Which code can read or write a proxy's storage.

Positional storage is fully described by the compiler layout, but anything else
— an ERC-7201 namespace, a hand-rolled ``s.slot := ROOT`` accessor, a raw
``sstore``, a ``delegatecall`` — is only visible by reading the code that runs
against the proxy. That code is:

- the deployed contract and every contract it inherits from, whole — any of
  their functions can be an entry point;
- library functions that are actually reachable from it. Internal library
  functions are inlined and public ones are delegatecalled, so both operate on
  the proxy's storage;
- free (file-level) functions reachable from it, which also run in the
  caller's storage context.

A library that is merely imported contributes nothing (the original
yearn/monitoring#367 failure was an imported helper suppressing the storage
check). Reachability is by name: a library function counts once its library is
referenced (``Lib.f(…)``, ``using Lib for …`` or ``using {Lib.f} for …``) and
something reachable calls a function of that name; a free function counts once
something reachable calls it. A function bound by a ``using {…}`` list counts
as called, since through an operator (``using {add as +}``) it has no call text.
Two files declaring the same library are both kept: which one a call binds to
isn't resolved, so neither may mask the other. Names are read through each file's import aliases
(``import {Lib as State}``), so a renamed import is still followed. Receivers
aren't resolved, so this over-approximates — it can only pull in more code to
check, which fails toward UNKNOWN.

Constructors are left out: they run once against the implementation's own
storage at deployment, never against the proxy's.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from utils.solidity_text import (
    FunctionDef,
    contract_functions,
    declarations,
    free_functions,
    import_aliases,
    parent_names,
    strip_noise,
)
from utils.verified_contract import VerifiedContract

_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_QUALIFIER_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\.")
_USING_RE = re.compile(r"\busing\s+([A-Za-z_][\w.]*)\s+for\b")
# `using {Lib.f, g as +} for T` — binds individual functions, possibly to operators.
_USING_LIST_RE = re.compile(r"\busing\s*\{([^}]*)\}\s*for\b")


@dataclass(frozen=True)
class ScopedFunction:
    """A function that can run against the proxy's storage, with where it lives."""

    owner: str  # declaring contract or library; the file path for a free function
    owner_kind: str  # "contract", "library" or "free"
    path: str
    fn: FunctionDef


def inheritance_chain(contract: VerifiedContract, target_file: str) -> list[tuple[str, str]]:
    """(contract name, file) for the deployed contract and every transitive base.

    Base names are read through the declaring file's import aliases, so
    ``import {Base as B}; contract C is B`` still reaches ``Base``. A base name
    declared in several files is taken from all of them — over-inclusion can
    only add code to check, never hide any.
    """
    declared_in = _declared_in(contract, kinds=("contract", "interface"))
    aliases = _AliasCache(contract)
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
                original = aliases.resolve(path, base)
                pending.append((original, declared_in.get(original, [])))
    return chain


def storage_scope(contract: VerifiedContract) -> list[ScopedFunction] | None:
    """Every function that can run against the proxy's storage.

    None when the deployed contract's own file can't be resolved: without it
    there is no chain to start from, and an empty scope would read as "nothing
    to check".
    """
    if not contract.contract_file:
        return None

    scope: list[ScopedFunction] = []
    for name, path in inheritance_chain(contract, contract.contract_file):
        scope.extend(_members(contract, name, "contract", path, lambda fn: fn.kind != "constructor"))

    libraries = _declared_in(contract, kinds=("library",))
    free = {path: free_functions(source) for path, source in contract.sources.items()}
    aliases = _AliasCache(contract)
    # (owner, file, signature): two files may each declare a `Lib` with the same
    # function. Which one a call binds to isn't resolved, so both stay in scope —
    # keying on the name alone let a harmless one mask the one writing storage.
    included: set[tuple[str, str, str]] = set()

    while True:
        called, referenced = _calls_and_references(contract, scope, aliases)
        additions: list[ScopedFunction] = []
        for library in referenced & set(libraries):
            for path in libraries[library]:
                additions.extend(_members(contract, library, "library", path, lambda fn: fn.name in called))
        for path, functions in free.items():
            additions.extend(ScopedFunction(path, "free", path, fn) for fn in functions if fn.name in called)

        added = False
        for scoped in additions:
            key = (scoped.owner, scoped.path, scoped.fn.signature)
            if key not in included:
                included.add(key)
                scope.append(scoped)
                added = True
        if not added:
            return scope


def scope_owners(contract: VerifiedContract, scope: list[ScopedFunction]) -> list[tuple[str, str]]:
    """Distinct (owner, file) pairs: the whole inheritance chain, then used libraries.

    Chain members come from the chain itself, not from their functions — a base
    that only declares a storage struct, or only forwards inheritance, still
    owns what it declares. Free functions have no owning declaration and are
    not listed.
    """
    seen: dict[tuple[str, str], None] = {}
    if contract.contract_file:
        for member in inheritance_chain(contract, contract.contract_file):
            seen.setdefault(member, None)
    for scoped in scope:
        if scoped.owner_kind != "free":
            seen.setdefault((scoped.owner, scoped.path), None)
    return list(seen)


class _AliasCache:
    """Per-file ``import {X as Y}`` maps, parsed once per file."""

    def __init__(self, contract: VerifiedContract) -> None:
        self._contract = contract
        self._by_path: dict[str, dict[str, str]] = {}

    def resolve(self, path: str, name: str) -> str:
        """The original name ``name`` refers to in ``path`` (itself if not an alias)."""
        if path not in self._by_path:
            self._by_path[path] = import_aliases(self._contract.sources.get(path, ""))
        return self._by_path[path].get(name, name)


def _declared_in(contract: VerifiedContract, kinds: tuple[str, ...]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path, source in contract.sources.items():
        for name, kind in declarations(source):
            if kind in kinds:
                out.setdefault(name, []).append(path)
    return out


def _members(
    contract: VerifiedContract, owner: str, kind: str, path: str, keep: Callable[[FunctionDef], bool]
) -> list[ScopedFunction]:
    functions = contract_functions(contract.sources[path], owner) or []
    return [ScopedFunction(owner, kind, path, fn) for fn in functions if keep(fn)]


def _calls_and_references(
    contract: VerifiedContract, scope: list[ScopedFunction], aliases: _AliasCache
) -> tuple[set[str], set[str]]:
    """Function names called, and library names referenced, by the scope so far.

    Every name is read through the import aliases of the file it appears in.
    ``using Lib for T`` sits at contract level rather than in a function, so the
    files of everything in scope are scanned for it.
    """
    called: set[str] = set()
    referenced: set[str] = set()
    for scoped in scope:
        text = scoped.fn.header + " " + scoped.fn.body
        called.update(aliases.resolve(scoped.path, name) for name in _CALL_RE.findall(text))
        referenced.update(aliases.resolve(scoped.path, name) for name in _QUALIFIER_RE.findall(text))
    paths = {path for _, path in scope_owners(contract, scope)} | {s.path for s in scope}
    for path in paths:
        text = strip_noise(contract.sources[path])
        for name in _USING_RE.findall(text):
            referenced.add(aliases.resolve(path, name.split(".")[-1]))
        # A listed function is bound, not necessarily called by name — through an
        # operator (`using {add as +}`) it has no call text at all — so it counts
        # as called outright, and a `Lib.` qualifier as a reference to `Lib`.
        for listed in _USING_LIST_RE.findall(text):
            for item in listed.split(","):
                qualifier, _, name = item.split(" as ")[0].strip().rpartition(".")
                if name:
                    called.add(aliases.resolve(path, name))
                if qualifier:
                    referenced.add(aliases.resolve(path, qualifier.split(".")[-1]))
    return called, referenced

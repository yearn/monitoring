"""Compare two proxy implementations and surface what actually changed.

When a governance tx upgrades a proxy, the LLM otherwise sees just the new impl
address and a diff URL it can't follow. This module produces the deterministic
evidence that grounds the alert, in four separated categories:

- **External ABI changes** — added/removed entry points and ``stateMutability``
  changes, taken from each implementation's own verified ABI.
- **Changed function bodies** — same signature, different code, scoped to the
  contract actually deployed at that address, with a small unified diff.
- **Storage compatibility** — COMPATIBLE / INCOMPATIBLE / UNKNOWN, from the
  compiler storage layouts Sourcify serves for both implementations.
- **Unvalidated items** — everything the above cannot see, stated explicitly so
  silence is never read as safety.

Every fact carries provenance (which contract, in which file). Nothing is
derived from the concatenated source bundle, because the bundle contains bases,
libraries and imported interfaces that are not part of the deployed contract —
attributing those to the proxy produced both false positives (an ``IMorpho``
declaration reported as a new unpermissioned function) and false negatives (a
real addition masked by a same-signature declaration elsewhere in the bundle).

Before anything is rendered, :func:`_consistency_violations` re-checks the
result against the ABIs it came from and against the other diffs produced in
this process. A section that fails its own check is dropped rather than shown.
"""

import difflib
import threading
from dataclasses import dataclass, field

from utils.abi_surface import AbiSurfaceDiff, abi_functions, diff_abi_surface
from utils.logger import get_logger
from utils.solidity_text import FunctionDef, contract_functions, uses_namespaced_storage
from utils.source_context import fetch_verified_contract
from utils.sourcify_layout import fetch_storage_layout
from utils.storage_layout import LayoutComparison, StorageCompatibility, compare_storage_layouts
from utils.verified_contract import VerifiedContract

logger = get_logger("utils.impl_diff")

STORAGE_UNKNOWN_NOTE = "not validated automatically; inspect compiler layouts manually"

# Prompt budget for the body section: enough to see what changed, not enough to
# drown the rest of the alert. Functions past the cap are still listed by name.
MAX_DIFFED_FUNCTIONS = 5
MAX_DIFF_LINES = 30
MAX_LISTED_CONFLICTS = 10

_UNVALIDATED_INHERITED = "function and modifier bodies inherited from base contracts were not compared"
_UNVALIDATED_INDIRECT = (
    "linked or inlined libraries, imported constants and free functions can change behavior "
    "even when the target's own function bodies are unchanged"
)
_UNVALIDATED_MODIFIERS = "source-level modifiers are shown as written; they are not an authorization proof"
_UNVALIDATED_NAMESPACED = "ERC-7201 namespaced storage layouts are not compared; only positional storage is"

# Added-surface sets seen in this process, so the same "additions" can't be
# reported for two different contracts (the failure that started this).
_seen_surfaces: dict[frozenset[str], tuple[str, str]] = {}
_seen_surfaces_lock = threading.Lock()


@dataclass(frozen=True)
class BodyChange:
    """A function whose signature is unchanged but whose code is not."""

    signature: str
    visibility: str
    modifiers: tuple[str, ...]
    diff: str = ""  # target-scoped unified diff, possibly truncated or empty

    def __str__(self) -> str:
        parts = [self.signature]
        if self.visibility:
            parts.append(self.visibility)
        parts.extend(self.modifiers)
        return " ".join(parts)


@dataclass(frozen=True)
class ImplTarget:
    """Which contract, in which file, was deployed at an address."""

    address: str
    contract_name: str
    contract_file: str | None

    def __str__(self) -> str:
        if self.contract_name and self.contract_file:
            return f"{self.contract_name} ({self.contract_file})"
        return self.contract_name or self.address


@dataclass(frozen=True)
class ImplDiff:
    """Deterministic evidence about one implementation swap."""

    old: ImplTarget
    new: ImplTarget
    surface: AbiSurfaceDiff | None  # None when unavailable or withheld by the gate
    changed_bodies: list[BodyChange]
    body_scope: str | None  # contract whose bodies were compared, None if unavailable
    storage: LayoutComparison
    unvalidated: list[str] = field(default_factory=list)
    surface_note: str = ""  # cross-diff provenance note, empty when nothing to flag

    @property
    def storage_status(self) -> StorageCompatibility:
        return self.storage.status


def diff_implementations(old_addr: str, new_addr: str, chain_id: int) -> ImplDiff | None:
    """Fetch both verified implementations and diff them. None on any failure."""
    old = fetch_verified_contract(chain_id, old_addr)
    new = fetch_verified_contract(chain_id, new_addr)
    if not old or not new:
        return None

    unvalidated: list[str] = []
    surface = diff_abi_surface(old.abi, new.abi) if old.abi and new.abi else None
    if surface is None:
        unvalidated.append("external ABI surface: no ABI available for one of the implementations")

    changed_bodies, body_scope = _diff_bodies(old, new)
    if body_scope is None:
        unvalidated.append("function bodies: could not resolve the deployed contract's own source unambiguously")
    else:
        unvalidated.append(_UNVALIDATED_INHERITED)
        unvalidated.append(_UNVALIDATED_INDIRECT)
    if changed_bodies:
        unvalidated.append(_UNVALIDATED_MODIFIERS)

    storage = _compare_storage(old, new, old_addr, new_addr, chain_id)
    if storage.status is not StorageCompatibility.UNKNOWN:
        unvalidated.append(_UNVALIDATED_NAMESPACED)

    surface_note = _provenance_note(new, surface)
    violations = _consistency_violations(old, new, surface, changed_bodies)
    if violations:
        # Deterministic evidence that contradicts its own source is worse than
        # no evidence: withhold the section rather than hand it to the model.
        logger.error("impl diff consistency gate failed for %s → %s: %s", old_addr, new_addr, violations)
        surface = None
        surface_note = ""
        changed_bodies = []
        body_scope = None
        unvalidated.extend(violations)

    return ImplDiff(
        old=_target(old_addr, old),
        new=_target(new_addr, new),
        surface=surface,
        changed_bodies=changed_bodies,
        body_scope=body_scope,
        storage=storage,
        unvalidated=unvalidated,
        surface_note=surface_note,
    )


def reset_provenance_registry() -> None:
    """Clear the cross-diff provenance registry (test isolation)."""
    with _seen_surfaces_lock:
        _seen_surfaces.clear()


def _target(address: str, contract: VerifiedContract) -> ImplTarget:
    return ImplTarget(address=address, contract_name=contract.contract_name, contract_file=contract.contract_file)


def _compare_storage(
    old: VerifiedContract,
    new: VerifiedContract,
    old_addr: str,
    new_addr: str,
    chain_id: int,
) -> LayoutComparison:
    """Compare compiler layouts, deferring to UNKNOWN for namespaced storage.

    A positional layout that matches says nothing about ERC-7201 namespaces, so
    a namespaced target cannot be called COMPATIBLE. A positional *conflict* is
    still a real conflict, and is reported as such.
    """
    comparison = compare_storage_layouts(
        fetch_storage_layout(chain_id, old_addr),
        fetch_storage_layout(chain_id, new_addr),
    )
    if comparison.status is not StorageCompatibility.COMPATIBLE:
        return comparison
    if _target_is_namespaced(old) or _target_is_namespaced(new):
        return LayoutComparison(
            status=StorageCompatibility.UNKNOWN,
            reason="namespaced layout not validated (target declares ERC-7201 storage)",
        )
    return comparison


def _target_is_namespaced(contract: VerifiedContract) -> bool:
    """ERC-7201 annotation on the deployed contract itself — not on any import."""
    if not contract.contract_file:
        return False
    return uses_namespaced_storage(contract.target_source, contract.contract_name)


def _diff_bodies(old: VerifiedContract, new: VerifiedContract) -> tuple[list[BodyChange], str | None]:
    """Compare function bodies within each side's own deployed contract.

    Returns (changes, scope) where ``scope`` names the contract the comparison
    covered, or None when either side's target is unresolved or its overloads
    are ambiguous — in which case no body claim is made at all.
    """
    old_fns = _target_functions(old)
    new_fns = _target_functions(new)
    if old_fns is None or new_fns is None:
        return [], None

    changes: list[BodyChange] = []
    for sig in sorted(set(old_fns) & set(new_fns)):
        old_fn, new_fn = old_fns[sig], new_fns[sig]
        if old_fn.fingerprint == new_fn.fingerprint:
            continue
        diff = ""
        if len(changes) < MAX_DIFFED_FUNCTIONS:
            diff = _unified_diff(
                _raw_definition(old.target_source, old_fn),
                _raw_definition(new.target_source, new_fn),
                sig,
            )
        changes.append(BodyChange(signature=sig, visibility=new_fn.visibility, modifiers=new_fn.modifiers, diff=diff))
    return changes, f"{new.contract_name} @ {new.contract_file}"


def _target_functions(contract: VerifiedContract) -> dict[str, FunctionDef] | None:
    """The deployed contract's own functions, keyed by signature.

    None when the compilation target can't be resolved, the named contract isn't
    in the file it resolved to, or two definitions collapse to the same
    signature — an ambiguous match would compare unrelated functions, so body
    analysis is reported unavailable instead of guessed.
    """
    if not contract.contract_file:
        return None
    functions = contract_functions(contract.target_source, contract.contract_name)
    if functions is None:
        return None

    by_signature = {fn.signature: fn for fn in functions}
    if len(by_signature) != len(functions):
        logger.info("ambiguous overloads in %s; skipping body diff", contract.contract_file)
        return None
    return by_signature


def _raw_definition(source: str, fn: FunctionDef) -> str:
    """The function's original text, comments included, for a readable diff."""
    start, end = fn.span
    return source[start:end]


def _unified_diff(old_text: str, new_text: str, signature: str) -> str:
    """A compact unified diff of one function, within the prompt budget.

    A diff too large to show is summarized by line counts rather than cut off
    mid-hunk: half a rewrite reads like a deletion, which is worse than a count.
    """
    old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
    lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"old {signature}",
            tofile=f"new {signature}",
            lineterm="",
            n=2,
        )
    )
    if len(lines) <= MAX_DIFF_LINES:
        return "\n".join(lines)

    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return (
        f"(rewritten: +{added}/-{removed} lines across {len(old_lines)} → {len(new_lines)} lines; "
        "diff omitted — read the source)"
    )


def _consistency_violations(
    old: VerifiedContract,
    new: VerifiedContract,
    surface: AbiSurfaceDiff | None,
    changed_bodies: list[BodyChange],
) -> list[str]:
    """Re-derive the claims from their own sources; return any that don't hold.

    Cheap insurance against exactly the class of bug this module was rewritten
    for: a surface claim that isn't in the target's ABI, or a body claim about a
    function the target doesn't define.
    """
    violations: list[str] = []
    if surface is not None:
        violations.extend(_surface_violations(old, new, surface))
    if changed_bodies:
        violations.extend(_body_violations(new, changed_bodies))
    return violations


def _surface_violations(old: VerifiedContract, new: VerifiedContract, surface: AbiSurfaceDiff) -> list[str]:
    """Every addition/removal must agree with the two target ABIs."""
    old_sigs = set(abi_functions(old.abi))
    new_sigs = set(abi_functions(new.abi))
    out: list[str] = []
    for fn in surface.added:
        if fn.signature not in new_sigs or fn.signature in old_sigs:
            out.append(f"withheld ABI section: '{fn.signature}' reported as added but the ABIs disagree")
    for fn in surface.removed:
        if fn.signature not in old_sigs or fn.signature in new_sigs:
            out.append(f"withheld ABI section: '{fn.signature}' reported as removed but the ABIs disagree")
    return out


def _provenance_note(new: VerifiedContract, surface: AbiSurfaceDiff | None) -> str:
    """Note when a second contract is handed the same set of additions.

    Reporting one contract's functions as another's is what started this, so the
    coincidence is worth surfacing — but it is not grounds for withholding. Each
    set is derived from, and re-checked against, that contract's own ABI, so two
    siblings genuinely gaining the same function is a real result the reviewer
    should still see. The note says which other contract it matched.
    """
    if surface is None:
        return ""
    added = frozenset(fn.signature for fn in surface.added)
    if not added:
        return ""

    label = new.contract_name or new.contract_file or ""
    with _seen_surfaces_lock:
        seen = _seen_surfaces.setdefault(added, (label, new.contract_file or ""))
    if seen[0] == label:
        return ""

    logger.warning("identical ABI additions reported for %s and %s: %s", seen[0], label, sorted(added))
    return (
        f"note: the same {len(added)} addition(s) were also reported for {seen[0]}; "
        "each set was verified against its own contract's ABI"
    )


def _body_violations(new: VerifiedContract, changed_bodies: list[BodyChange]) -> list[str]:
    """Every changed body must belong to a function the target itself defines."""
    defined = _target_functions(new)
    if defined is None:
        return ["withheld body section: the deployed contract's functions could not be re-resolved"]
    return [
        f"withheld body section: '{change.signature}' is not defined by {new.contract_name}"
        for change in changed_bodies
        if change.signature not in defined
    ]


def _fmt_surface(diff: ImplDiff) -> list[str]:
    """Render the ABI section — the only source of external-surface claims."""
    label = diff.new.contract_name or diff.new.address
    if diff.surface is None:
        return [f"External ABI changes ({label}): NOT AVAILABLE — see Unvalidated items."]
    if diff.surface.is_empty:
        return [f"External ABI changes ({label}): none — the external surface is identical."]

    lines = [f"External ABI changes ({label}):"]
    lines.extend(f"  + {fn}" for fn in diff.surface.added)
    lines.extend(f"  - {fn}" for fn in diff.surface.removed)
    lines.extend(f"  ~ {old} → {new}" for old, new in diff.surface.mutability_changed)
    if diff.surface_note:
        lines.append(f"  {diff.surface_note}")
    return lines


def _fmt_bodies(diff: ImplDiff) -> list[str]:
    """Render the body-change section. ABI equality is not behavioral equality."""
    if diff.body_scope is None:
        return ["Changed function bodies: NOT COMPARED — see Unvalidated items."]
    if not diff.changed_bodies:
        return [f"Changed function bodies ({diff.body_scope}): none."]

    lines = [f"Changed function bodies ({diff.body_scope}):"]
    for change in diff.changed_bodies:
        lines.append(f"  ~ {change}")
        lines.extend(f"      {line}" for line in change.diff.splitlines())
    return lines


def _fmt_storage(diff: ImplDiff) -> list[str]:
    """Render the storage section, including why a verdict is unavailable."""
    storage = diff.storage
    if storage.status is StorageCompatibility.UNKNOWN:
        reason = storage.reason or STORAGE_UNKNOWN_NOTE
        return [
            f"Storage compatibility: UNKNOWN — {reason}.",
            f"  {STORAGE_UNKNOWN_NOTE.capitalize()}.",
        ]

    lines = [f"Storage compatibility: {storage.status.value} (compiler layouts, both implementations verified)."]
    if storage.conflicts:
        lines.append("  Conflicting slots:")
        for conflict in storage.conflicts[:MAX_LISTED_CONFLICTS]:
            lines.append(f"    {conflict}")
        if len(storage.conflicts) > MAX_LISTED_CONFLICTS:
            lines.append(f"    … and {len(storage.conflicts) - MAX_LISTED_CONFLICTS} more")
    for gap in storage.consumed_gaps:
        lines.append(f"  Reserved space consumed: {gap}")
    for entry in storage.added:
        lines.append(f"  + {entry}")
    for before, after in storage.renamed:
        lines.append(f"  renamed (same slot, same type): {before.label} → {after.label}")
    return lines


def format_impl_diff(diff: ImplDiff) -> str:
    """Render an :class:`ImplDiff` into a prompt-ready text block."""
    lines: list[str] = [
        f"Old: {diff.old.address} — {diff.old}",
        f"New: {diff.new.address} — {diff.new}",
    ]
    if diff.old.contract_name and diff.new.contract_name and diff.old.contract_name != diff.new.contract_name:
        lines.append(f"Contract name changed: {diff.old.contract_name} → {diff.new.contract_name}")

    for section in (_fmt_surface(diff), _fmt_bodies(diff), _fmt_storage(diff)):
        lines.append("")
        lines.extend(section)

    if diff.unvalidated:
        lines.append("")
        lines.append("Unvalidated items (absence of a finding here is NOT evidence of safety):")
        lines.extend(f"  - {item}" for item in diff.unvalidated)
    return "\n".join(lines)

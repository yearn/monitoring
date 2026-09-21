"""Compare two proxy implementations and surface what actually changed.

When a governance tx upgrades a proxy, the LLM otherwise sees just the new impl
address and a diff URL it can't follow. This module produces the deterministic
evidence that grounds the alert, in four separated categories:

- **External ABI changes** — added/removed entry points and ``stateMutability``
  changes, taken from each implementation's own verified ABI.
- **Target-defined function changes** — scoped to the contract actually deployed
  at that address: bodies that changed under an unchanged signature, plus
  internal/private members added or removed. Comparing only the functions both
  sides share would miss behavior *moved* into a new helper, or a deleted hook —
  neither of which has an ABI footprint.
- **Storage compatibility** — COMPATIBLE / INCOMPATIBLE / UNKNOWN, combining
  the compiler storage layouts Sourcify serves for both implementations with
  the storage those layouts can't describe (namespaces, custom-root accessors,
  raw ``sstore``, ``delegatecall``). A proven conflict anywhere wins; anything
  unseen makes it UNKNOWN; the positional result is shown on its own as well.
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
from utils.namespaced_storage import NamespaceComparison, compare_non_positional
from utils.solidity_text import FunctionDef, contract_functions, value_type_declarations
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
# Added internal helpers get their source, not just a name: an upgrade that moves
# behavior into one is exactly the case a signature alone fails to explain.
MAX_ADDED_BODIES = 3

_UNVALIDATED_INHERITED = "function and modifier bodies inherited from base contracts were not compared"
_UNVALIDATED_INDIRECT = (
    "linked or inlined libraries, imported constants and free functions can change behavior "
    "even when the target's own function bodies are unchanged"
)
_UNVALIDATED_MODIFIERS = "source-level modifiers are shown as written; they are not an authorization proof"

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
class BodyDiff:
    """What changed among the functions the deployed contract itself defines.

    ``added``/``removed`` cover only members the ABI cannot show (internal,
    private, modifiers); external ones live in the ABI surface diff instead, so
    the two sections never report the same function twice.
    """

    changed: list[BodyChange] = field(default_factory=list)
    added: list[BodyChange] = field(default_factory=list)
    removed: list[BodyChange] = field(default_factory=list)
    scope: str | None = None  # contract whose members were compared, None if unavailable

    @property
    def is_empty(self) -> bool:
        return not (self.changed or self.added or self.removed)


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
    bodies: BodyDiff
    storage: LayoutComparison
    unvalidated: list[str] = field(default_factory=list)
    surface_note: str = ""  # cross-diff provenance note, empty when nothing to flag
    namespaces: NamespaceComparison = field(default_factory=NamespaceComparison)
    # Positional and non-positional storage combined; ``storage`` alone is positional.
    storage_verdict: StorageCompatibility = StorageCompatibility.UNKNOWN

    @property
    def storage_status(self) -> StorageCompatibility:
        return self.storage_verdict


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

    bodies = _diff_bodies(old, new)
    if bodies.scope is None:
        unvalidated.append("function bodies: could not resolve the deployed contract's own source unambiguously")
    else:
        unvalidated.append(_UNVALIDATED_INHERITED)
        unvalidated.append(_UNVALIDATED_INDIRECT)
    if not bodies.is_empty:
        unvalidated.append(_UNVALIDATED_MODIFIERS)

    # Storage coverage gaps go in the storage section, beside the verdict they
    # hold back — not in Unvalidated items, which never affect a verdict.
    storage, namespaces, storage_verdict = _compare_storage(old, new, old_addr, new_addr, chain_id)

    surface_note = _provenance_note(new, surface)
    violations = _consistency_violations(old, new, surface, bodies)
    if violations:
        # Deterministic evidence that contradicts its own source is worse than
        # no evidence: withhold the section rather than hand it to the model.
        logger.error("impl diff consistency gate failed for %s → %s: %s", old_addr, new_addr, violations)
        surface = None
        surface_note = ""
        bodies = BodyDiff(scope=None)
        unvalidated.extend(violations)

    return ImplDiff(
        old=_target(old_addr, old),
        new=_target(new_addr, new),
        surface=surface,
        bodies=bodies,
        storage=storage,
        unvalidated=unvalidated,
        surface_note=surface_note,
        namespaces=namespaces,
        storage_verdict=storage_verdict,
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
) -> tuple[LayoutComparison, NamespaceComparison, StorageCompatibility]:
    """Compare positional and non-positional storage, and combine the verdict.

    Precedence: any proven conflict, in either part, is INCOMPATIBLE; otherwise
    anything the check could not see — an unresolved type, a namespace it
    couldn't prove, a raw ``sstore``, a ``delegatecall`` — is UNKNOWN; only
    full coverage is COMPATIBLE. The positional result is returned as-is so it
    stays visible on its own even when the combined verdict is UNKNOWN.
    """
    positional = compare_storage_layouts(
        fetch_storage_layout(chain_id, old_addr),
        fetch_storage_layout(chain_id, new_addr),
        _value_types(old),
        _value_types(new),
    )
    non_positional = compare_non_positional(old, new)
    if positional.conflicts or non_positional.conflicts:
        verdict = StorageCompatibility.INCOMPATIBLE
    elif positional.status is not StorageCompatibility.COMPATIBLE or not non_positional.is_validated:
        verdict = StorageCompatibility.UNKNOWN
    else:
        verdict = StorageCompatibility.COMPATIBLE
    return positional, non_positional, verdict


def _value_types(contract: VerifiedContract) -> dict[str, str | None]:
    """Custom value type → underlying type, declared across this side's sources.

    A name declared once — or several times over the same type — resolves. A
    name declared over *different* types is ambiguous (None): without lexical
    scope resolution there is no telling which declaration a slot uses.
    """
    underlying: dict[str, set[str]] = {}
    for source in contract.sources.values():
        for name, base in value_type_declarations(source):
            underlying.setdefault(name, set()).add(base)
    return {name: next(iter(bases)) if len(bases) == 1 else None for name, bases in underlying.items()}


def _diff_bodies(old: VerifiedContract, new: VerifiedContract) -> BodyDiff:
    """Diff the union of functions each side's own deployed contract defines.

    Comparing only the intersection loses whole functions: an upgrade that moves
    logic out of one function into a new internal helper, or deletes a transfer
    hook, would show as a shrinking body and nothing else. Additions and removals
    are reported for members the ABI cannot show — internal, private and
    modifiers — since external ones are already the ABI section's job.
    """
    old_fns = _target_functions(old)
    new_fns = _target_functions(new)
    if old_fns is None or new_fns is None:
        return BodyDiff(scope=None)

    changed: list[BodyChange] = []
    for sig in sorted(set(old_fns) & set(new_fns)):
        old_fn, new_fn = old_fns[sig], new_fns[sig]
        if old_fn.fingerprint == new_fn.fingerprint:
            continue
        diff = ""
        if len(changed) < MAX_DIFFED_FUNCTIONS:
            diff = _unified_diff(
                _raw_definition(old.target_source, old_fn),
                _raw_definition(new.target_source, new_fn),
                sig,
            )
        changed.append(BodyChange(signature=sig, visibility=new_fn.visibility, modifiers=new_fn.modifiers, diff=diff))

    added = [new_fns[sig] for sig in sorted(set(new_fns) - set(old_fns)) if _is_hidden_from_abi(new_fns[sig])]
    removed = [old_fns[sig] for sig in sorted(set(old_fns) - set(new_fns)) if _is_hidden_from_abi(old_fns[sig])]
    return BodyDiff(
        changed=changed,
        added=[_added_change(fn, new.target_source, i) for i, fn in enumerate(added)],
        removed=[
            BodyChange(signature=fn.signature, visibility=fn.visibility, modifiers=fn.modifiers) for fn in removed
        ],
        scope=f"{new.contract_name} @ {new.contract_file}",
    )


def _is_hidden_from_abi(fn: FunctionDef) -> bool:
    """True for members no ABI can carry: internal, private, and modifiers.

    Visibility is read from the target's own source rather than matched against
    ABI signatures, because source types don't always spell the ABI's canonical
    ones (``initialize(address,Id,…)`` vs ``initialize(address,bytes32,…)``).
    """
    return fn.visibility not in ("external", "public")


def _added_change(fn: FunctionDef, source: str, index: int) -> BodyChange:
    """An added function, with its body when it fits the budget.

    A signature alone rarely explains a new internal helper — the behavior an
    upgrade moved into one is exactly what a reviewer needs to see — so the
    first few get their source, subject to the same truncation as a diff.
    """
    diff = _unified_diff("", _raw_definition(source, fn), fn.signature) if index < MAX_ADDED_BODIES else ""
    return BodyChange(signature=fn.signature, visibility=fn.visibility, modifiers=fn.modifiers, diff=diff)


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

    if not old_lines:
        # An addition has nothing to diff against; "rewritten" would misdescribe it.
        return f"(new function, {len(new_lines)} lines; body omitted — read the source)"

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
    bodies: BodyDiff,
) -> list[str]:
    """Re-derive the claims from their own sources; return any that don't hold.

    Cheap insurance against exactly the class of bug this module was rewritten
    for: a surface claim that isn't in the target's ABI, or a body claim about a
    function the target doesn't define.
    """
    violations: list[str] = []
    if surface is not None:
        violations.extend(_surface_violations(old, new, surface))
    if bodies.changed or bodies.added:
        violations.extend(_body_violations(new, bodies.changed + bodies.added))
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
    """Render the target-defined function section.

    ABI equality is not behavioral equality, and neither is an unchanged
    function list: logic moved into a new internal helper, or a deleted hook,
    only shows up here.
    """
    bodies = diff.bodies
    if bodies.scope is None:
        return ["Target-defined function changes: NOT COMPARED — see Unvalidated items."]
    if bodies.is_empty:
        return [f"Target-defined function changes ({bodies.scope}): none."]

    lines = [f"Target-defined function changes ({bodies.scope}):"]
    if bodies.added:
        lines.append("  Added (internal/private — not on the external surface):")
        lines.extend(_fmt_body_change("+", change) for change in bodies.added)
    if bodies.removed:
        lines.append("  No longer defined here (internal/private; may have moved to a base contract):")
        lines.extend(_fmt_body_change("-", change) for change in bodies.removed)
    if bodies.changed:
        lines.append("  Changed bodies (same signature, different code):")
        lines.extend(_fmt_body_change("~", change) for change in bodies.changed)
    return [line for block in lines for line in block.splitlines()]


def _fmt_body_change(marker: str, change: BodyChange) -> str:
    """One entry plus its indented source/diff, when one fits the budget."""
    head = f"    {marker} {change}"
    if not change.diff:
        return head
    return head + "\n" + "\n".join(f"        {line}" for line in change.diff.splitlines())


_VERDICT_REASONS = {
    StorageCompatibility.INCOMPATIBLE: "a conflict is proven (listed below)",
    StorageCompatibility.UNKNOWN: "storage coverage is incomplete (gaps listed below)",
    StorageCompatibility.COMPATIBLE: "positional layout and all detected non-positional storage validated",
}


def _fmt_storage(diff: ImplDiff) -> list[str]:
    """Render the combined verdict, then the positional and non-positional parts.

    The positional result gets its own line even when the combined verdict is
    UNKNOWN: "every declared variable kept its slot" is still worth knowing when
    some other storage couldn't be checked.
    """
    verdict = diff.storage_verdict
    lines = [f"Storage compatibility: {verdict.value} — {_VERDICT_REASONS[verdict]}."]
    if verdict is StorageCompatibility.UNKNOWN:
        lines.append(f"  {STORAGE_UNKNOWN_NOTE.capitalize()}.")
    lines.extend(_fmt_positional(diff.storage))
    lines.extend(_fmt_non_positional(diff.namespaces))
    return lines


def _fmt_positional(storage: LayoutComparison) -> list[str]:
    """The compiler-layout comparison, with its own status."""
    status = storage.status.value
    if storage.status is StorageCompatibility.UNKNOWN and storage.reason:
        status += f" — {storage.reason}"
    lines = [f"  Positional layout (compiler): {status}"]
    if storage.conflicts:
        lines.append("    Conflicting slots:")
        lines.extend(f"      {conflict}" for conflict in storage.conflicts[:MAX_LISTED_CONFLICTS])
        if len(storage.conflicts) > MAX_LISTED_CONFLICTS:
            lines.append(f"      … and {len(storage.conflicts) - MAX_LISTED_CONFLICTS} more")
    lines.extend(f"    Unresolved: {gap}" for gap in storage.gaps)
    lines.extend(f"    Reserved space consumed: {gap}" for gap in storage.consumed_gaps)
    lines.extend(f"    + {entry}" for entry in storage.added)
    for before, after in storage.renamed:
        lines.append(f"    renamed (same slot, same type): {before.label} → {after.label}")
    for before, after in storage.retyped:
        lines.append(
            f"    retyped (same slot, same representation): {after.label} {before.type_label} → {after.type_label}"
        )
    return lines


def _fmt_non_positional(namespaces: NamespaceComparison) -> list[str]:
    """Storage outside the compiler layout: what was proven, and what couldn't be."""
    lines = [f"  Namespaced storage unchanged (ERC-7201, root verified): {n}" for n in namespaces.unchanged]
    if namespaces.conflicts:
        lines.append("  Non-positional conflicts:")
        lines.extend(f"    {conflict}" for conflict in namespaces.conflicts)
    if namespaces.unvalidated:
        lines.append("  Coverage gaps (storage this check cannot see):")
        lines.extend(f"    - {gap}" for gap in namespaces.unvalidated)
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

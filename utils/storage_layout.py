"""Compare two compiler storage layouts for proxy-upgrade compatibility.

A proxy keeps its storage across an upgrade, so the question is narrow and
mechanical: does every variable the old implementation wrote still live at the
same slot and byte offset, with the same shape, under the new one?

What that means in practice:

- Identity is ``(slot, offset)`` plus the shape of the type — recursively:
  encoding, byte width, array base/length, mapping key/value, struct members'
  own slot/offset/shape, and at every level a normalized *kind*. Width alone is
  not enough: ``uint256`` and ``bytes32`` fill the same 32 bytes, but reading one
  as the other changes what the stored data means.
- Compiler-internal noise is ignored: type ids (``t_contract(IToken)6874`` vs
  ``…6876`` for the same type), AST ids, and the declaring-contract label.
  Contract types and ``address`` are one kind — that retype is intentional.
- A rename is not an incompatibility. The slot doesn't move because the variable
  got a new name, so renames are reported for the reviewer and nothing more —
  e.g. retiring variables as ``__deprecated_*``.
- Reserved gaps (``uint256[N] __gap``) exist to be consumed. An old gap need not
  survive; the variables around it must.

This module makes no claim about ERC-7201 namespaced storage: namespaced structs
do not appear in the positional layout at all, so a compatible positional result
says nothing about them. ``utils.namespaced_storage`` checks those, and
``utils.impl_diff`` combines the two into one verdict.
"""

import re
from dataclasses import dataclass, field
from enum import Enum

from utils.sourcify_layout import StorageLayout

# `uint256[40] __gap` / `_gap` / `gap` — reserved space, by convention.
_GAP_NAME_RE = re.compile(r"(?:^|_)gap$", re.IGNORECASE)

# Depth guard for self-referential types (a struct holding a mapping to itself).
_MAX_TYPE_DEPTH = 12

# `t_enum(Status)123` / `t_userDefinedValueType(Id)6519` — named, with an AST id.
# A contract-scoped type may be qualified (`C.Id`); lookups use the last segment.
_NAMED_TYPE_RE = re.compile(r"t_(enum|userDefinedValueType)\(([\w.]+)\)")

# Marks a custom value type whose underlying type couldn't be resolved from source.
# Two such shapes can match textually without proving anything, so a slot carrying
# one is a coverage gap rather than a pass.
_UNRESOLVED_MARKER = "unresolved-udvt"

# Custom value type name → underlying type, resolved from one side's verified source.
# ``None`` means the name was declared ambiguously; a missing name was not declared.
ValueTypes = dict[str, str | None]
# A trailing AST id after a closing paren, for exotic ids such as function types.
_AST_ID_RE = re.compile(r"(?<=\))\d+")


class StorageCompatibility(str, Enum):
    """Storage-compatibility result — deliberately not an overall safety verdict.

    ``UNKNOWN`` is not a soft ``COMPATIBLE``: it means no layout comparison was
    performed (no Sourcify coverage on one or both sides, a malformed payload,
    or namespaced storage), and a reviewer still has to look.
    """

    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SlotEntry:
    """One variable in a layout, normalized for comparison."""

    slot: int
    offset: int
    label: str
    type_label: str  # human-readable type, for display only
    shape: str  # canonical shape including type kinds — the thing actually compared
    width_shape: str = ""  # byte placement only, to explain *how* two shapes differ

    @property
    def position(self) -> tuple[int, int]:
        return (self.slot, self.offset)

    def __str__(self) -> str:
        return f"slot {self.slot}+{self.offset} {self.type_label} {self.label}"


@dataclass(frozen=True)
class LayoutComparison:
    """Outcome of comparing an old and a new storage layout."""

    status: StorageCompatibility
    conflicts: list[str] = field(default_factory=list)  # proven incompatibilities
    gaps: list[str] = field(default_factory=list)  # slots whose compatibility couldn't be established
    added: list[SlotEntry] = field(default_factory=list)  # new variables (appended or into a gap)
    renamed: list[tuple[SlotEntry, SlotEntry]] = field(default_factory=list)  # same slot/shape, new name
    retyped: list[tuple[SlotEntry, SlotEntry]] = field(default_factory=list)  # same representation, new type
    consumed_gaps: list[str] = field(default_factory=list)  # reserved space that was claimed
    reason: str = ""  # populated when status is UNKNOWN


def compare_storage_layouts(
    old: StorageLayout | None,
    new: StorageLayout | None,
    old_value_types: ValueTypes | None = None,
    new_value_types: ValueTypes | None = None,
) -> LayoutComparison:
    """Compare two layouts: a proven conflict is INCOMPATIBLE, a gap is UNKNOWN.

    ``*_value_types`` resolve custom value types to their underlying types for
    each side (see :func:`_type_kind`). Without them every custom value type is
    unresolved, which can only make a slot a gap — never a pass.
    """
    if old is None and new is None:
        return _unknown("no Sourcify-verified compiler layout for either implementation")
    if old is None:
        return _unknown("no Sourcify-verified compiler layout for the old implementation")
    if new is None:
        return _unknown("no Sourcify-verified compiler layout for the new implementation")

    old_entries = _normalize(old, old_value_types)
    new_entries = _normalize(new, new_value_types)
    if not old_entries or not new_entries:
        return _unknown("compiler layout was empty for one of the implementations")

    new_by_position = {e.position: e for e in new_entries}

    conflicts: list[str] = []
    gaps: list[str] = []
    renamed: list[tuple[SlotEntry, SlotEntry]] = []
    retyped: list[tuple[SlotEntry, SlotEntry]] = []
    consumed_gaps: list[str] = []

    for entry in old_entries:
        if _is_gap(entry):
            consumed_gaps.extend(_describe_gap_use(entry, new_by_position))
            continue
        moved = new_by_position.get(entry.position)
        if moved is None:
            conflicts.append(f"{entry} is gone in the new layout — that slot is no longer written the same way")
            continue
        # Width is compiler-reported fact, so a resize is proven whether or not
        # every type resolved. Only a same-width comparison depends on resolution.
        if moved.width_shape != entry.width_shape:
            conflicts.append(_describe_type_change(entry, moved))
            continue
        if _UNRESOLVED_MARKER in entry.shape or _UNRESOLVED_MARKER in moved.shape:
            gaps.append(
                f"slot {entry.slot}+{entry.offset}: {entry.type_label} {entry.label} → {moved.type_label} — "
                "a custom value type's underlying type could not be resolved from source"
            )
            continue
        if moved.shape != entry.shape:
            conflicts.append(_describe_type_change(entry, moved))
            continue
        if moved.type_label != entry.type_label:
            retyped.append((entry, moved))
        if moved.label != entry.label:
            renamed.append((entry, moved))

    old_by_position = {e.position: e for e in old_entries}
    added = [e for e in new_entries if _is_new_variable(e, old_by_position.get(e.position))]

    if conflicts:
        status, reason = StorageCompatibility.INCOMPATIBLE, ""
    elif gaps:
        status, reason = StorageCompatibility.UNKNOWN, "some slot types could not be resolved"
    else:
        status, reason = StorageCompatibility.COMPATIBLE, ""
    return LayoutComparison(
        status=status,
        conflicts=conflicts,
        gaps=gaps,
        added=added,
        renamed=renamed,
        retyped=retyped,
        consumed_gaps=consumed_gaps,
        reason=reason,
    )


def _unknown(reason: str) -> LayoutComparison:
    return LayoutComparison(status=StorageCompatibility.UNKNOWN, reason=reason)


def _describe_type_change(old: SlotEntry, new: SlotEntry) -> str:
    """Say whether the bytes moved or were merely reinterpreted.

    Both are incompatible, but they fail differently: a resized or restructured
    slot garbles neighbouring data, while a same-width retype (``uint256`` →
    ``bytes32``) leaves every byte in place and reads it as something else.
    """
    where = f"slot {old.slot}+{old.offset}"
    if old.width_shape == new.width_shape:
        return (
            f"{where}: {old.type_label} {old.label} reinterpreted as {new.type_label} {new.label} "
            "(same slot and width, different type)"
        )
    return f"{where}: {old.type_label} {old.label} → {new}"


def _normalize(layout: StorageLayout, value_types: ValueTypes | None) -> list[SlotEntry]:
    """Turn raw layout entries into comparable ones, ordered by position."""
    entries: list[SlotEntry] = []
    for raw in layout.storage:
        slot = _as_int(raw.get("slot"))
        offset = _as_int(raw.get("offset"))
        type_id = raw.get("type")
        if slot is None or offset is None or not isinstance(type_id, str):
            continue
        entries.append(
            SlotEntry(
                slot=slot,
                offset=offset,
                label=str(raw.get("label") or ""),
                type_label=_type_label(layout.types, type_id),
                shape=_type_shape(layout.types, type_id, value_types=value_types),
                width_shape=_type_shape(layout.types, type_id, with_kind=False),
            )
        )
    return sorted(entries, key=lambda e: e.position)


def _as_int(value: object) -> int | None:
    """Slots arrive as strings, offsets as ints."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _type_label(types: dict[str, dict], type_id: str) -> str:
    """Display label for a type (``mapping(address => bool)``), falling back to its id."""
    entry = types.get(type_id)
    label = entry.get("label") if isinstance(entry, dict) else None
    return str(label) if label else type_id


def _type_shape(
    types: dict[str, dict],
    type_id: str,
    depth: int = 0,
    *,
    with_kind: bool = True,
    value_types: ValueTypes | None = None,
) -> str:
    """Canonical description of a type: where its bytes live and how they're read.

    Byte placement alone is not enough: ``uint256`` and ``bytes32`` occupy the
    same 32 bytes, but a new implementation reading a counter as an identifier
    has changed what the stored data *means*. So each level also carries a
    normalized kind (:func:`_type_kind`).

    Everything the compiler regenerates between builds is still excluded — type
    ids, AST ids, the declaring contract — and so are struct names: a struct is
    compared member by member instead. ``with_kind=False`` gives the width-only
    shape, used to tell a same-width reinterpretation apart from a moved or
    resized slot when describing a conflict.
    """
    entry = types.get(type_id)
    if not isinstance(entry, dict) or depth > _MAX_TYPE_DEPTH:
        # Unknown id: fall back to the id itself so two unknowns still compare.
        return f"?{type_id}"

    parts = [f"enc={entry.get('encoding', '')}", f"bytes={entry.get('numberOfBytes', '')}"]
    if with_kind:
        parts.insert(0, f"kind={_type_kind(type_id, value_types)}")
    base = entry.get("base")
    if isinstance(base, str):
        parts.append(f"base=({_type_shape(types, base, depth + 1, with_kind=with_kind, value_types=value_types)})")
    key, value = entry.get("key"), entry.get("value")
    if isinstance(key, str):
        parts.append(f"key=({_type_shape(types, key, depth + 1, with_kind=with_kind, value_types=value_types)})")
    if isinstance(value, str):
        parts.append(f"value=({_type_shape(types, value, depth + 1, with_kind=with_kind, value_types=value_types)})")
    members = entry.get("members")
    if isinstance(members, list):
        rendered = [_member_shape(types, m, depth, with_kind, value_types) for m in members if isinstance(m, dict)]
        parts.append("members=[" + ",".join(rendered) + "]")
    return ";".join(parts)


def _member_shape(
    types: dict[str, dict], member: dict, depth: int, with_kind: bool, value_types: ValueTypes | None
) -> str:
    """A struct member's position and shape — its name is display-only."""
    member_type = member.get("type")
    shape = (
        _type_shape(types, member_type, depth + 1, with_kind=with_kind, value_types=value_types)
        if isinstance(member_type, str)
        else "?"
    )
    return f"{member.get('slot', '')}+{member.get('offset', '')}:({shape})"


def _type_kind(type_id: str, value_types: ValueTypes | None = None) -> str:
    """How a slot's bytes are interpreted, with compiler-generated ids stripped.

    - ``address``, ``address payable`` and every contract type are one kind: a
      contract-typed variable *is* an address in storage, so retyping
      ``IERC20 token`` as ``address token`` (or as another interface) is an
      intentional, safe equivalence.
    - A user-defined value type is its *underlying* type, resolved from the
      source (``type Id is bytes32`` → ``bytes32``): unwrapping ``Id`` to
      ``bytes32`` keeps the representation, while ``Id`` redeclared over
      ``uint256`` does not. The layout only names the type, so without a
      resolved declaration it is marked unresolved and never matched by name.
    - Enums keep their name (``enum(Status)``) minus the AST id.
    - Mappings, arrays and structs are compared through their components, so
      their kind is just the category — a renamed struct type is not a change.
    - Elementary types are compared exactly: ``uint256`` ≠ ``bytes32`` ≠ ``int256``.
    """
    if type_id.startswith(("t_address", "t_contract(")):
        return "address"
    named = _NAMED_TYPE_RE.match(type_id)
    if named:
        kind, name = named.group(1), named.group(2).split(".")[-1]
        if kind == "enum":
            return f"enum({name})"
        underlying = (value_types or {}).get(name)
        return underlying if underlying else f"{_UNRESOLVED_MARKER}({name})"
    for prefix, category in (("t_mapping(", "mapping"), ("t_array(", "array"), ("t_struct(", "struct")):
        if type_id.startswith(prefix):
            return category
    # Elementary: `t_uint256`, `t_bool`, `t_string_storage`, `t_bytes_storage`.
    return _AST_ID_RE.sub("", type_id.removeprefix("t_").removesuffix("_storage"))


def _is_new_variable(entry: SlotEntry, previous: SlotEntry | None) -> bool:
    """True if ``entry`` claims a slot the old layout did not use for a variable.

    That covers both appending past the end and claiming reserved space; a gap
    that simply stayed where it was is not an addition.
    """
    if previous is None:
        return True
    if not _is_gap(previous):
        return False  # a real variable was there — the conflict/rename checks own this slot
    return (entry.label, entry.shape) != (previous.label, previous.shape)


def _is_gap(entry: SlotEntry) -> bool:
    """True for an OpenZeppelin-style reserved gap (`uint256[40] __gap`)."""
    return bool(_GAP_NAME_RE.search(entry.label)) and entry.type_label.endswith("]")


def _describe_gap_use(gap: SlotEntry, new_by_position: dict[tuple[int, int], SlotEntry]) -> list[str]:
    """Report what the new layout put where a reserved gap used to start.

    Purely informational: a gap is reserved space, so consuming it (or not) is
    compatible either way. What matters is that the *other* variables kept their
    slots, which the caller checks entry by entry.
    """
    occupant = new_by_position.get(gap.position)
    if occupant is None or _is_gap(occupant):
        return []
    return [f"{gap.type_label} {gap.label} at slot {gap.slot} is now {occupant.type_label} {occupant.label}"]

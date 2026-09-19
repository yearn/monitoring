"""Compare two compiler storage layouts for proxy-upgrade compatibility.

A proxy keeps its storage across an upgrade, so the question is narrow and
mechanical: does every variable the old implementation wrote still live at the
same slot and byte offset, with the same shape, under the new one?

What that means in practice:

- Identity is ``(slot, offset)`` plus the *structural* shape of the type —
  recursively: encoding, byte width, array base/length, mapping key/value, and
  struct members' own slot/offset/shape.
- Compiler-internal noise is ignored: type ids (``t_contract(IMorpho)6874`` vs
  ``…6876`` for the same type), AST ids, and the declaring-contract label.
- A rename is not an incompatibility. The slot doesn't move because the variable
  got a new name, so renames are reported for the reviewer and nothing more.
  The real 3Jane upgrade renamed four variables to ``__deprecated_*``.
- Reserved gaps (``uint256[N] __gap``) exist to be consumed. An old gap need not
  survive; the variables around it must.

This module makes no claim about ERC-7201 namespaced storage: namespaced structs
do not appear in the positional layout at all, so a compatible positional result
says nothing about them (see ``utils.impl_diff`` for how that's surfaced).
"""

import re
from dataclasses import dataclass, field
from enum import Enum

from utils.sourcify_layout import StorageLayout

# `uint256[40] __gap` / `_gap` / `gap` — reserved space, by convention.
_GAP_NAME_RE = re.compile(r"(?:^|_)gap$", re.IGNORECASE)

# Depth guard for self-referential types (a struct holding a mapping to itself).
_MAX_TYPE_DEPTH = 12


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
    shape: str  # canonical structural shape — the thing actually compared

    @property
    def position(self) -> tuple[int, int]:
        return (self.slot, self.offset)

    def __str__(self) -> str:
        return f"slot {self.slot}+{self.offset} {self.type_label} {self.label}"


@dataclass(frozen=True)
class LayoutComparison:
    """Outcome of comparing an old and a new storage layout."""

    status: StorageCompatibility
    conflicts: list[str] = field(default_factory=list)  # why it's incompatible
    added: list[SlotEntry] = field(default_factory=list)  # new variables (appended or into a gap)
    renamed: list[tuple[SlotEntry, SlotEntry]] = field(default_factory=list)  # same slot/shape, new name
    consumed_gaps: list[str] = field(default_factory=list)  # reserved space that was claimed
    reason: str = ""  # populated when status is UNKNOWN


def compare_storage_layouts(old: StorageLayout | None, new: StorageLayout | None) -> LayoutComparison:
    """Compare two layouts. Anything less than full coverage is UNKNOWN."""
    if old is None and new is None:
        return _unknown("no Sourcify-verified compiler layout for either implementation")
    if old is None:
        return _unknown("no Sourcify-verified compiler layout for the old implementation")
    if new is None:
        return _unknown("no Sourcify-verified compiler layout for the new implementation")

    old_entries = _normalize(old)
    new_entries = _normalize(new)
    if not old_entries or not new_entries:
        return _unknown("compiler layout was empty for one of the implementations")

    new_by_position = {e.position: e for e in new_entries}

    conflicts: list[str] = []
    renamed: list[tuple[SlotEntry, SlotEntry]] = []
    consumed_gaps: list[str] = []

    for entry in old_entries:
        if _is_gap(entry):
            consumed_gaps.extend(_describe_gap_use(entry, new_by_position))
            continue
        moved = new_by_position.get(entry.position)
        if moved is None:
            conflicts.append(f"{entry} is gone in the new layout — that slot is no longer written the same way")
        elif moved.shape != entry.shape:
            conflicts.append(f"slot {entry.slot}+{entry.offset}: {entry.type_label} {entry.label} → {moved}")
        elif moved.label != entry.label:
            renamed.append((entry, moved))

    old_by_position = {e.position: e for e in old_entries}
    added = [e for e in new_entries if _is_new_variable(e, old_by_position.get(e.position))]

    return LayoutComparison(
        status=StorageCompatibility.INCOMPATIBLE if conflicts else StorageCompatibility.COMPATIBLE,
        conflicts=conflicts,
        added=added,
        renamed=renamed,
        consumed_gaps=consumed_gaps,
    )


def _unknown(reason: str) -> LayoutComparison:
    return LayoutComparison(status=StorageCompatibility.UNKNOWN, reason=reason)


def _normalize(layout: StorageLayout) -> list[SlotEntry]:
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
                shape=_type_shape(layout.types, type_id),
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


def _type_shape(types: dict[str, dict], type_id: str, depth: int = 0) -> str:
    """Canonical structural description of a type.

    Deliberately excludes every identifier the compiler regenerates between
    builds — type ids, AST ids, declaring contract — and the type's own label,
    so that renaming a struct or swapping one contract type for another of the
    same width is not reported as a layout conflict. What's compared is what
    determines where bytes live.
    """
    entry = types.get(type_id)
    if not isinstance(entry, dict) or depth > _MAX_TYPE_DEPTH:
        # Unknown id: fall back to the id itself so two unknowns still compare.
        return f"?{type_id}"

    parts = [f"enc={entry.get('encoding', '')}", f"bytes={entry.get('numberOfBytes', '')}"]
    base = entry.get("base")
    if isinstance(base, str):
        parts.append(f"base=({_type_shape(types, base, depth + 1)})")
    key, value = entry.get("key"), entry.get("value")
    if isinstance(key, str):
        parts.append(f"key=({_type_shape(types, key, depth + 1)})")
    if isinstance(value, str):
        parts.append(f"value=({_type_shape(types, value, depth + 1)})")
    members = entry.get("members")
    if isinstance(members, list):
        rendered = [_member_shape(types, m, depth) for m in members if isinstance(m, dict)]
        parts.append("members=[" + ",".join(rendered) + "]")
    return ";".join(parts)


def _member_shape(types: dict[str, dict], member: dict, depth: int) -> str:
    """A struct member's position and shape — its name is display-only."""
    member_type = member.get("type")
    shape = _type_shape(types, member_type, depth + 1) if isinstance(member_type, str) else "?"
    return f"{member.get('slot', '')}+{member.get('offset', '')}:({shape})"


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

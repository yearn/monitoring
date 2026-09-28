"""Whole-bundle source and compiler-settings diff between two verified contracts.

An implementation's verified bundle is every file it was compiled from — its own
file, inherited bases, internal libraries, interfaces. An upgrade whose change
sits in a base contract (a new modifier on an inherited ``mint``, a cap added to
an inherited setter) leaves the deployed contract's own file untouched, so a
diff scoped to that file reports "unchanged" for a behavior change. This module
diffs every file, keyed by path, plus the compiler settings that change bytecode
without touching source: compiler version, EVM version, optimizer, ``viaIR`` and
linked (externally deployed) library addresses.

Import remappings are deliberately ignored: they only matter through the files
they resolve to, and those files are already diffed.
"""

import difflib
import json
import posixpath
from dataclasses import dataclass, field

from utils.verified_contract import VerifiedContract

# Prompt budget: enough to show what changed, not enough to drown the alert.
# Files past the cap are still listed with their line counts.
MAX_DIFFED_FILES = 6
MAX_FILE_DIFF_LINES = 60

NOT_LINKED = "(not linked)"

# Vendored dependencies sort after the project's own files, so the budget goes
# to the protocol's code first.
_DEPENDENCY_PREFIXES = ("node_modules/", "lib/", "@")


@dataclass(frozen=True)
class FileChange:
    """One source file that differs between the two bundles."""

    path: str
    added_lines: int
    removed_lines: int
    old_path: str | None = None  # set when the file moved to a new path
    diff: str = ""  # unified diff, empty when over the budget

    def __str__(self) -> str:
        where = f"{self.old_path} → {self.path}" if self.old_path else self.path
        return f"{where} (+{self.added_lines}/-{self.removed_lines})"


@dataclass(frozen=True)
class SourceFilesDiff:
    """Every file-level difference between two verified bundles."""

    changed: list[FileChange] = field(default_factory=list)
    added: list[FileChange] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)  # (old, new) path, identical content

    @property
    def is_empty(self) -> bool:
        return not (self.changed or self.added or self.removed or self.moved)


@dataclass(frozen=True)
class SettingChange:
    """One compiler setting that differs; values are rendered strings."""

    name: str
    old: str
    new: str

    def __str__(self) -> str:
        return f"{self.name}: {self.old} → {self.new}"


@dataclass(frozen=True)
class SettingsDiff:
    """Compiler-setting changes, and whether every setting could be compared.

    ``complete`` is False when either side has no standard-json settings: only
    the compiler version was compared, so an empty ``changes`` then means
    "same compiler", not "same settings".
    """

    changes: list[SettingChange] = field(default_factory=list)
    complete: bool = True


@dataclass(frozen=True)
class LinkedLibrary:
    """An externally deployed library the contract was linked against."""

    file: str
    name: str
    address: str


def diff_source_files(old_sources: dict[str, str], new_sources: dict[str, str]) -> SourceFilesDiff:
    """Diff two bundles file by file.

    Files are matched by path. A file missing on one side is paired with a file
    on the other side by identical content (a move) or, failing that, by a
    unique basename (a move plus an edit, e.g. after a remapping change), so a
    relocated dependency doesn't show up as a whole-file deletion and addition.
    """
    changed: list[tuple[str, str | None, str, str]] = []  # (path, old_path, old_text, new_text)
    for path in sorted(set(old_sources) & set(new_sources)):
        if old_sources[path] != new_sources[path]:
            changed.append((path, None, old_sources[path], new_sources[path]))

    only_old = {p: old_sources[p] for p in old_sources if p not in new_sources}
    only_new = {p: new_sources[p] for p in new_sources if p not in old_sources}
    moved: list[tuple[str, str]] = []
    for old_path, new_path in _pair_unmatched(only_old, only_new):
        old_text, new_text = only_old.pop(old_path), only_new.pop(new_path)
        if old_text == new_text:
            moved.append((old_path, new_path))
        else:
            changed.append((new_path, old_path, old_text, new_text))

    changed.sort(key=lambda item: _budget_order(item[0]))
    budget = MAX_DIFFED_FILES
    file_changes: list[FileChange] = []
    for path, from_path, before, after in changed:
        file_changes.append(_file_change(path, from_path, before, after, with_diff=budget > 0))
        budget -= 1

    added: list[FileChange] = []
    for path in sorted(only_new, key=_budget_order):
        added.append(_file_change(path, None, "", only_new[path], with_diff=budget > 0))
        budget -= 1

    return SourceFilesDiff(changed=file_changes, added=added, removed=sorted(only_old), moved=sorted(moved))


def diff_compiler_settings(old: VerifiedContract, new: VerifiedContract) -> SettingsDiff:
    """Compiler settings that differ between two verified contracts.

    The compiler version is a top-level Etherscan field present for every
    verification, so it is always compared. Everything else (EVM version,
    optimizer, ``viaIR``, linked libraries) lives in standard-json settings,
    which a single-file verification doesn't carry; when either side lacks
    them the result is marked incomplete rather than claiming "no difference"
    from nothing.
    """
    changes: list[SettingChange] = []
    if old.compiler_version != new.compiler_version:
        changes.append(SettingChange("compiler", old.compiler_version or "(unset)", new.compiler_version or "(unset)"))
    if not old.settings or not new.settings:
        return SettingsDiff(changes=changes, complete=False)

    for key in ("evmVersion", "viaIR", "optimizer"):
        before, after = _setting(old.settings, key), _setting(new.settings, key)
        if before != after:
            changes.append(SettingChange(key, before, after))
    for name, old_addr, new_addr in relinked_libraries(old, new):
        changes.append(SettingChange(f"linked library {name}", old_addr, new_addr))
    return SettingsDiff(changes=changes, complete=True)


def relinked_libraries(old: VerifiedContract, new: VerifiedContract) -> list[tuple[str, str, str]]:
    """(name, old address, new address) for each linked library whose address differs.

    A library linked on one side only gets :data:`NOT_LINKED` for the other.
    """
    old_libs = {(lib.file, lib.name): lib.address for lib in linked_libraries(old)}
    new_libs = {(lib.file, lib.name): lib.address for lib in linked_libraries(new)}
    out: list[tuple[str, str, str]] = []
    for key in sorted(set(old_libs) | set(new_libs)):
        old_addr, new_addr = old_libs.get(key, NOT_LINKED), new_libs.get(key, NOT_LINKED)
        if old_addr.lower() != new_addr.lower():
            out.append((key[1], old_addr, new_addr))
    return out


def linked_libraries(contract: VerifiedContract) -> list[LinkedLibrary]:
    """Libraries linked into the bytecode, from standard-json ``settings.libraries``."""
    libraries = contract.settings.get("libraries")
    if not isinstance(libraries, dict):
        return []
    out: list[LinkedLibrary] = []
    for file, entries in libraries.items():
        if not isinstance(entries, dict):
            continue
        for name, address in entries.items():
            if isinstance(address, str) and address:
                out.append(LinkedLibrary(file=str(file), name=str(name), address=address))
    return out


def format_source_files(diff: SourceFilesDiff, label: str) -> list[str]:
    """Render a bundle diff as prompt lines, headed by ``label``."""
    if diff.is_empty:
        return [f"{label}: none — every file in the verified bundle is identical."]

    lines = [f"{label}:"]
    for change in diff.changed:
        lines.extend(_fmt_file("~", change))
    for change in diff.added:
        lines.extend(_fmt_file("+", change))
    lines.extend(f"  - {path} (removed)" for path in diff.removed)
    lines.extend(f"  = {old} → {new} (moved, content identical)" for old, new in diff.moved)
    return lines


def _pair_unmatched(only_old: dict[str, str], only_new: dict[str, str]) -> list[tuple[str, str]]:
    """Pair files present on one side only: identical content first, then a unique basename."""
    pairs: list[tuple[str, str]] = []
    taken_new: set[str] = set()
    # Several files can share content (identical interfaces, empty markers), so
    # each content maps to every new path holding it; each is used at most once.
    by_content: dict[str, list[str]] = {}
    for path in sorted(only_new):
        by_content.setdefault(only_new[path], []).append(path)
    for old_path, text in sorted(only_old.items()):
        candidates = [p for p in by_content.get(text, []) if p not in taken_new]
        if not candidates:
            continue
        # Among identical files, keep a file's own name when one is available.
        same_name = [p for p in candidates if posixpath.basename(p) == posixpath.basename(old_path)]
        new_path = (same_name or candidates)[0]
        pairs.append((old_path, new_path))
        taken_new.add(new_path)

    paired_old = {old for old, _ in pairs}
    old_by_name = _unique_by_basename([p for p in only_old if p not in paired_old])
    new_by_name = _unique_by_basename([p for p in only_new if p not in taken_new])
    for name in sorted(set(old_by_name) & set(new_by_name)):
        pairs.append((old_by_name[name], new_by_name[name]))
    return pairs


def _unique_by_basename(paths: list[str]) -> dict[str, str]:
    """basename → path, for basenames that occur exactly once."""
    counts: dict[str, list[str]] = {}
    for path in paths:
        counts.setdefault(posixpath.basename(path), []).append(path)
    return {name: group[0] for name, group in counts.items() if len(group) == 1}


def _budget_order(path: str) -> tuple[bool, str]:
    return (path.startswith(_DEPENDENCY_PREFIXES), path)


def _file_change(path: str, old_path: str | None, old_text: str, new_text: str, *, with_diff: bool) -> FileChange:
    lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"old {old_path or path}",
            tofile=f"new {path}",
            lineterm="",
            n=2,
        )
    )
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    diff = _budgeted_diff(lines) if with_diff else ""
    return FileChange(path=path, added_lines=added, removed_lines=removed, old_path=old_path, diff=diff)


def _budgeted_diff(lines: list[str]) -> str:
    """Whole hunks up to the line budget, then a count of what was left out.

    Cutting at hunk boundaries keeps every shown change intact — half a hunk
    reads like a deletion — while one oversized hunk no longer hides the small
    ones before it. Empty when not even the first hunk fits.
    """
    if len(lines) <= MAX_FILE_DIFF_LINES:
        return "\n".join(lines)

    hunk_starts = [i for i, line in enumerate(lines) if line.startswith("@@")]
    ends = hunk_starts[1:] + [len(lines)]
    kept_until = 0
    for end in ends:
        if end > MAX_FILE_DIFF_LINES:
            break
        kept_until = end
    if not kept_until:
        return ""

    omitted = len(hunk_starts) - sum(1 for start in hunk_starts if start < kept_until)
    return "\n".join(lines[:kept_until]) + f"\n… {omitted} more hunk(s) omitted — read the source"


def _fmt_file(marker: str, change: FileChange) -> list[str]:
    head = f"  {marker} {change}"
    if not change.diff:
        return [f"{head} — diff omitted (over budget); read the source"]
    return [head, *(f"      {line}" for line in change.diff.splitlines())]


def _setting(settings: dict, key: str) -> str:
    """One setting rendered for comparison; nested values as canonical JSON."""
    value = settings.get(key)
    if value is None:
        return "(unset)"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)

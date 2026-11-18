"""Publish a complete verified-source diff for a proxy upgrade alert."""

import difflib
import io
import os
import re
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

from utils.chains import EXPLORER_URLS
from utils.disk_cache import DiskCache
from utils.file_lock import LockTimeout, file_lock
from utils.logger import get_logger
from utils.source_context import fetch_verified_contract
from utils.source_diff import diff_compiler_settings, pair_moved_sources
from utils.verified_contract import VerifiedContract
from utils.wavey_gist import upload_to_gist

logger = get_logger("utils.upgrade_diff")

# Wavey Gist's total file content limit (https://gist.wavey.info/llms.txt).
MAX_GIST_BYTES = 1024 * 1024
DIFF_TIMEOUT_SECONDS = 4
MAX_DIFF_WORKERS = 4
_diff_cache = DiskCache("wavey-upgrade-diffs-v1", max_entries=2048)


@dataclass(frozen=True)
class UpgradeDiffLink:
    """One proxy and the implementation comparison shown for it in an alert."""

    proxy: str
    old_impl: str
    new_impl: str
    explorer_url: str


def _pair_key(old_impl: str, new_impl: str, chain_id: int) -> str:
    """Return the ordered, case-insensitive key for an implementation pair."""
    return f"{chain_id}:{old_impl.lower()}:{new_impl.lower()}"


def _approved_pairs() -> set[str]:
    """Read explicitly approved pairs for public disclosure."""
    if not os.getenv("WAVEY_GIST_API_KEY"):
        return set()
    entries = os.getenv("WAVEY_PUBLIC_UPGRADE_DIFFS", "").lower().split(",")
    return {key for item in entries if re.fullmatch(r"\d+:0x[0-9a-f]{40}:0x[0-9a-f]{40}", key := item.strip())}


def publish_upgrade_diff(old_impl: str, new_impl: str, chain_id: int) -> str:
    """Get or publish the Wavey Gist for an approved implementation pair.

    Args:
        old_impl: Current implementation address.
        new_impl: Proposed implementation address.
        chain_id: Chain on which both implementations are deployed.

    Returns:
        A public URL, or an empty string when publishing is disabled or unavailable.
    """
    key = _pair_key(old_impl, new_impl, chain_id)
    if key not in _approved_pairs():
        return ""

    # Both alert jobs can encounter the same pair before either writes the cache.
    cached = _diff_cache.get(key)
    if isinstance(cached, str):
        return cached
    try:
        with file_lock(f"wavey-upgrade-diff-{key}", timeout=0):
            cached = _diff_cache.get(key)
            if isinstance(cached, str):
                return cached
            url = _create_upgrade_diff(old_impl, new_impl, chain_id)
            if url:
                _diff_cache.set_positive(key, url)
            return url
    except LockTimeout:
        return ""


def resolve_upgrade_diff_links(upgrades: list[UpgradeDiffLink], chain_id: int) -> dict[str, str]:
    """Resolve approved comparisons in parallel within one alert's time budget.

    Args:
        upgrades: Proxy upgrades and their explorer fallback links in this alert.
        chain_id: Chain on which the upgrades were detected.

    Returns:
        A mapping from explorer URLs to Wavey URLs that were ready by the deadline.
    """
    if not upgrades:
        return {}
    approved = _approved_pairs()
    pending = {
        _pair_key(upgrade.old_impl, upgrade.new_impl, chain_id): upgrade
        for upgrade in upgrades
        if _pair_key(upgrade.old_impl, upgrade.new_impl, chain_id) in approved
    }
    if not pending:
        return {}

    ready: dict[str, str] = {}
    pool = ThreadPoolExecutor(max_workers=min(MAX_DIFF_WORKERS, len(pending)))
    try:
        jobs = {
            pool.submit(publish_upgrade_diff, upgrade.old_impl, upgrade.new_impl, chain_id): key
            for key, upgrade in pending.items()
        }
        finished, delayed = wait(jobs, timeout=DIFF_TIMEOUT_SECONDS)
        if delayed:
            logger.info("Wavey source diff deadline reached for %s comparison(s) on chain %s", len(delayed), chain_id)
        for job in finished:
            key = jobs[job]
            try:
                url = job.result()
            except Exception:
                upgrade = pending[key]
                logger.exception(
                    "Failed to publish source diff for chain=%s old=%s new=%s",
                    chain_id,
                    upgrade.old_impl,
                    upgrade.new_impl,
                )
                continue
            if url:
                ready[key] = url
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return {
        upgrade.explorer_url: ready[key]
        for upgrade in upgrades
        if (key := _pair_key(upgrade.old_impl, upgrade.new_impl, chain_id)) in ready
    }


def apply_upgrade_diff_links(lines: list[str], urls: dict[str, str]) -> list[str]:
    """Replace explorer diff URLs on Telegram diff lines with ready Wavey URLs.

    Args:
        lines: Telegram alert lines with their explorer fallback links.
        urls: Ready Wavey URLs keyed by their corresponding explorer URLs.

    Returns:
        Alert lines in their original order, with available Wavey links.
    """
    replacements = {f"📊 [Diff]({explorer})": f"📊 [Diff]({gist})" for explorer, gist in urls.items()}
    return [replacements.get(line, line) for line in lines]


def format_upgrade_report_links(upgrades: list[UpgradeDiffLink], urls: dict[str, str], chain_id: int) -> list[str]:
    """List each proxy's diff in the full alert report.

    Args:
        upgrades: Proxy upgrades detected for the alert.
        urls: Ready Wavey URLs keyed by their corresponding explorer URLs.
        chain_id: Chain on which the proxies are deployed.

    Returns:
        Markdown list items using the same URLs as the Telegram alert.
    """
    return list(
        dict.fromkeys(
            f"- Proxy {_address_link(upgrade.proxy, chain_id)} "
            f"[source diff]({urls.get(upgrade.explorer_url, upgrade.explorer_url)})"
            for upgrade in upgrades
        )
    )


def _address_link(address: str, chain_id: int, *, code: bool = False) -> str:
    """Render a full address with an explorer link where available."""
    explorer = EXPLORER_URLS.get(chain_id)
    anchor = "#code" if code else ""
    return f"[`{address}`]({explorer}/address/{address}{anchor})" if explorer else f"`{address}`"


def _create_upgrade_diff(old_impl: str, new_impl: str, chain_id: int) -> str:
    """Render and upload the verified source and deployment metadata."""
    old = fetch_verified_contract(chain_id, old_impl, timeout=DIFF_TIMEOUT_SECONDS, retries=0)
    if not old or not old.sources:
        return ""
    new = fetch_verified_contract(chain_id, new_impl, timeout=DIFF_TIMEOUT_SECONDS, retries=0)
    if not new or not new.sources:
        return ""

    contract_name = new.contract_name if new.contract_name.isidentifier() else new_impl
    title = f"{contract_name} upgrade source diff"
    old_link = _address_link(old_impl, chain_id, code=True)
    new_link = _address_link(new_impl, chain_id, code=True)
    body = f"Chain ID {chain_id}\n\n- Old implementation {old_link}\n- New implementation {new_link}\n\n"
    body += _format_deployment_metadata(old, new) + "\n\n" + _format_source_diff(old.sources, new.sources)
    if len(f"# {title}\n\n{body}".encode("utf-8")) > MAX_GIST_BYTES:
        logger.info("Source diff for chain=%s old=%s new=%s exceeds Wavey Gist limit", chain_id, old_impl, new_impl)
        return ""
    return upload_to_gist(body, title=title, timeout=DIFF_TIMEOUT_SECONDS, retries=0)


def _format_deployment_metadata(old: VerifiedContract, new: VerifiedContract) -> str:
    """Show verified deployment differences and the coverage of the comparison."""
    settings = diff_compiler_settings(old, new)
    lines = ["## Deployment metadata", ""]
    if settings.changes:
        changes = "\n".join(str(change) for change in settings.changes)
        fence = _backtick_fence(changes, minimum=3)
        lines.extend((f"{fence}text\n{changes}\n{fence}", ""))
    if not settings.complete:
        lines.extend(
            ("Full compiler settings and linked libraries are unavailable for at least one implementation.", "")
        )
    elif not settings.changes:
        lines.extend(("The compared compiler settings and linked library addresses match.", ""))
    if old.constructor_arguments is None or new.constructor_arguments is None:
        args = "Constructor arguments are unavailable for at least one implementation."
    elif old.constructor_arguments != new.constructor_arguments:
        args = "Constructor arguments differ. See the linked deployments for the encoded values."
    else:
        args = "The reported constructor arguments match."
    lines.extend((args, "", "This comparison covers verified source and reported deployment metadata only."))
    return "\n".join(lines)


def _backtick_fence(text: str, minimum: int = 1) -> str:
    """Choose a Markdown delimiter longer than any backtick run in the content."""
    return "`" * max(minimum, 1 + max(map(len, re.findall(r"`+", text)), default=0))


def _format_source_diff(old_sources: dict[str, str], new_sources: dict[str, str]) -> str:
    """Render all changed paths as separate, untruncated unified diffs.

    Args:
        old_sources: Verified source files of the current implementation.
        new_sources: Verified source files of the proposed implementation.

    Returns:
        Markdown with complete source hunks and file moves.
    """
    moved = dict(
        pair_moved_sources(
            {path: old_sources[path] for path in old_sources.keys() - new_sources.keys()},
            {path: new_sources[path] for path in new_sources.keys() - old_sources.keys()},
        )
    )
    paths = [(path, moved.get(path, path)) for path in old_sources]
    paths.extend((path, path) for path in new_sources.keys() - old_sources.keys() - set(moved.values()))
    sections: list[str] = []
    for old_path, new_path in sorted(paths, key=lambda pair: pair[1]):
        old = old_sources.get(old_path)
        new = new_sources.get(new_path)
        if old == new and old_path == new_path:
            continue

        # Verified filenames and source can contain Markdown delimiters.
        old_label = " ".join(old_path.splitlines())
        new_label = " ".join(new_path.splitlines())
        label = new_label if old_path == new_path else f"{old_label} → {new_label}"
        label_fence = _backtick_fence(label)
        heading = f"### {label_fence} {label} {label_fence}"
        if old == new:
            sections.append(f"{heading}\n\nFile moved. Its contents match.")
            continue

        diff = difflib.unified_diff(
            io.StringIO(old or "", newline="\n").readlines(),
            io.StringIO(new or "", newline="\n").readlines(),
            fromfile=f"a/{old_label}" if old is not None else "/dev/null",
            tofile=f"b/{new_label}" if new is not None else "/dev/null",
        )
        patch = "".join(line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n" for line in diff)
        if not patch:
            patch = "+ Empty file added\n" if old is None else "- Empty file removed\n"
        fence = _backtick_fence(patch, minimum=3)
        sections.append(f"{heading}\n\n{fence}diff\n{patch}{fence}")

    content = "\n\n".join(sections) if sections else "No source file changes. See deployment metadata above."
    return f"## Verified source\n\n{content}"

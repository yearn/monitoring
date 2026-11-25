"""Publish a complete verified-source diff for a proxy upgrade alert."""

import difflib
import io
import os
import re
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from time import monotonic

from utils.chains import EXPLORER_URLS
from utils.disk_cache import DiskCache
from utils.file_lock import LockTimeout, file_lock
from utils.logger import get_logger
from utils.source_context import fetch_contract_from_provider, fetch_verified_contract
from utils.source_diff import diff_compiler_settings, pair_moved_sources
from utils.verified_contract import VerifiedContract
from utils.wavey_gist import upload_to_gist

logger = get_logger("utils.upgrade_diff")

# Wavey Gist's total file content limit (https://gist.wavey.info/llms.txt).
MAX_GIST_BYTES = 1024 * 1024
# Longest an alert waits for its gists. Every request is bounded by what is
# left of this deadline, and nothing is uploaded after it passes, so a late
# comparison cannot publish a gist that no alert links to.
DIFF_DEADLINE_SECONDS = 60
# Per Etherscan or Wavey request, capped further by the remaining deadline.
REQUEST_TIMEOUT_SECONDS = 20
# Skip a request when less than this is left: it would almost certainly time out.
MIN_REQUEST_SECONDS = 1
# Extra wait after the deadline for a job finishing its last bounded request.
DEADLINE_GRACE_SECONDS = 2
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


def _request_timeout(deadline: float) -> float | None:
    """Return the timeout for the next request, or None once the deadline is too close."""
    remaining = deadline - monotonic()
    return None if remaining < MIN_REQUEST_SECONDS else min(REQUEST_TIMEOUT_SECONDS, remaining)


def publish_upgrade_diff(old_impl: str, new_impl: str, chain_id: int, *, deadline: float | None = None) -> str:
    """Get or publish the Wavey Gist for an implementation pair.

    Args:
        old_impl: Current implementation address.
        new_impl: Proposed implementation address.
        chain_id: Chain on which both implementations are deployed.
        deadline: ``time.monotonic()`` value after which no request or upload starts.
            Defaults to ``DIFF_DEADLINE_SECONDS`` from now.

    Returns:
        A public URL, or an empty string when publishing is disabled or unavailable.
    """
    if not os.getenv("WAVEY_GIST_API_KEY"):
        return ""
    key = _pair_key(old_impl, new_impl, chain_id)
    if deadline is None:
        deadline = monotonic() + DIFF_DEADLINE_SECONDS

    # Both alert jobs can encounter the same pair before either writes the cache.
    cached = _diff_cache.get(key)
    if isinstance(cached, str):
        return cached
    try:
        # Another alert job publishing the same pair writes the cache before it
        # releases the lock, so waiting for it reuses that gist.
        with file_lock(f"wavey-upgrade-diff-{key}", timeout=max(0.0, deadline - monotonic())):
            cached = _diff_cache.get(key)
            if isinstance(cached, str):
                return cached
            url = _create_upgrade_diff(old_impl, new_impl, chain_id, deadline)
            if url:
                _diff_cache.set_positive(key, url)
            return url
    except LockTimeout:
        return ""


def resolve_upgrade_diff_links(upgrades: list[UpgradeDiffLink], chain_id: int) -> dict[str, str]:
    """Resolve the alert's comparisons in parallel within one time budget.

    Args:
        upgrades: Proxy upgrades and their explorer fallback links in this alert.
        chain_id: Chain on which the upgrades were detected.

    Returns:
        A mapping from explorer URLs to Wavey URLs that were ready by the deadline.
    """
    if not upgrades or not os.getenv("WAVEY_GIST_API_KEY"):
        return {}
    # Proxies upgraded to the same implementation pair share one gist.
    pending = {_pair_key(upgrade.old_impl, upgrade.new_impl, chain_id): upgrade for upgrade in upgrades}

    ready: dict[str, str] = {}
    deadline = monotonic() + DIFF_DEADLINE_SECONDS
    pool = ThreadPoolExecutor(max_workers=min(MAX_DIFF_WORKERS, len(pending)))
    try:
        jobs = {
            pool.submit(publish_upgrade_diff, upgrade.old_impl, upgrade.new_impl, chain_id, deadline=deadline): key
            for key, upgrade in pending.items()
        }
        finished, delayed = wait(jobs, timeout=DIFF_DEADLINE_SECONDS + DEADLINE_GRACE_SECONDS)
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
        # Unfinished jobs stop at the deadline: they start no further request or upload.
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


def _fetch_for_diff(chain_id: int, address: str, deadline: float) -> VerifiedContract | None:
    """Fetch one implementation's verified record within the alert deadline."""
    timeout = _request_timeout(deadline)
    if timeout is None:
        return None
    return fetch_verified_contract(
        chain_id, address, timeout=timeout, retries=0, require_constructor_arguments=True, deadline=deadline
    )


def _create_upgrade_diff(old_impl: str, new_impl: str, chain_id: int, deadline: float) -> str:
    """Render and upload the verified source and deployment metadata before the deadline."""
    old = _fetch_for_diff(chain_id, old_impl, deadline)
    if not old or not old.sources:
        return ""
    new = _fetch_for_diff(chain_id, new_impl, deadline)
    if not new or not new.sources:
        return ""
    old, new = _same_provider_pair(chain_id, (old_impl, old), (new_impl, new), deadline)

    contract_name = new.contract_name if new.contract_name.isidentifier() else new_impl
    title = f"{contract_name} upgrade source diff"
    old_link = _address_link(old_impl, chain_id, code=True)
    new_link = _address_link(new_impl, chain_id, code=True)
    body = (
        f"Chain ID {chain_id}\n\n"
        f"- Old implementation {old_link} ({_source_label(old)})\n"
        f"- New implementation {new_link} ({_source_label(new)})\n\n"
    )
    if old.provider != new.provider:
        body += (
            "> The implementations come from different source providers. File paths, flattening and "
            "compiler version strings can differ even where the deployed code does not.\n\n"
        )
    body += _format_source_diff(old.sources, new.sources)
    if deployment := _format_deployment_changes(old, new):
        body += "\n\n" + deployment
    if len(f"# {title}\n\n{body}".encode("utf-8")) > MAX_GIST_BYTES:
        logger.info("Source diff for chain=%s old=%s new=%s exceeds Wavey Gist limit", chain_id, old_impl, new_impl)
        return ""
    timeout = _request_timeout(deadline)
    if timeout is None:
        logger.info(
            "Source diff deadline passed before upload for chain=%s old=%s new=%s", chain_id, old_impl, new_impl
        )
        return ""
    return upload_to_gist(body, title=title, timeout=timeout, retries=0)


def _same_provider_pair(
    chain_id: int,
    old: tuple[str, VerifiedContract],
    new: tuple[str, VerifiedContract],
    deadline: float,
) -> tuple[VerifiedContract, VerifiedContract]:
    """Read both implementations from one source provider when they came from different ones.

    Cached records are usually from Etherscan and new lookups from Sourcify. The
    providers name files and compiler versions differently, so a mixed pair would
    report changes that are only formatting. The new implementation is refetched
    from the old one's provider first, then the old from the new one's provider.

    Args:
        chain_id: Chain on which both implementations are deployed.
        old: Current implementation address and its verified record.
        new: Proposed implementation address and its verified record.
        deadline: Monotonic deadline for the alert's gists.

    Returns:
        The old and new records, from one provider when either refetch succeeded.
    """
    (old_impl, old_record), (new_impl, new_record) = old, new
    if old_record.provider == new_record.provider:
        return old_record, new_record
    for address, provider, keep_old in ((new_impl, old_record.provider, True), (old_impl, new_record.provider, False)):
        timeout = _request_timeout(deadline)
        if timeout is None:
            break
        aligned = fetch_contract_from_provider(chain_id, address, provider, timeout=timeout, deadline=deadline)
        if aligned and aligned.sources:
            original = new_record if keep_old else old_record
            if original.constructor_arguments is not None:
                aligned = replace(aligned, constructor_arguments=original.constructor_arguments)
            return (old_record, aligned) if keep_old else (aligned, new_record)
    logger.info("Source diff for chain=%s old=%s new=%s mixes source providers", chain_id, old_impl, new_impl)
    return old_record, new_record


def _source_label(contract: VerifiedContract) -> str:
    """Show the provenance and any missing constructor data for one side."""
    labels = [contract.provider]
    if contract.match_quality:
        labels.append(contract.match_quality.replace("_", " "))
    if contract.constructor_arguments is None:
        labels.append("constructor arguments unavailable")
    return ", ".join(labels)


def _format_deployment_changes(old: VerifiedContract, new: VerifiedContract) -> str:
    """List compiler, linked-library and constructor-argument changes, or "" when none differ.

    Identical source can still deploy different behavior: constructor arguments
    set immutables, and linked libraries decide which external code runs.

    Args:
        old: Verified record of the current implementation.
        new: Verified record of the proposed implementation.

    Returns:
        A Markdown section listing each change, or an empty string.
    """
    changes = [str(change) for change in diff_compiler_settings(old, new).changes]
    args = (old.constructor_arguments, new.constructor_arguments)
    if None not in args and args[0] != args[1]:
        changes.append("constructor arguments: differ (see the linked deployments for the encoded values)")
    if not changes:
        return ""
    text = "\n".join(changes)
    fence = _backtick_fence(text, minimum=3)
    return f"## Deployment changes\n\n{fence}text\n{text}\n{fence}"


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

    content = "\n\n".join(sections) if sections else "No source file changes."
    return f"## Verified source\n\n{content}"

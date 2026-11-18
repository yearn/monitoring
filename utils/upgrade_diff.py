"""Publish a complete verified-source diff for a proxy upgrade alert."""

import difflib
import os
import re

from utils.chains import EXPLORER_URLS
from utils.logger import get_logger
from utils.source_context import fetch_verified_contract
from utils.wavey_gist import upload_to_gist

logger = get_logger("utils.upgrade_diff")

# Wavey Gist's total file content limit (https://gist.wavey.info/llms.txt).
MAX_GIST_BYTES = 1024 * 1024


def publish_upgrade_diff(old_impl: str, new_impl: str, chain_id: int) -> str:
    """Return a public Wavey Gist URL for two verified implementations, or an empty string."""
    if not os.getenv("WAVEY_GIST_API_KEY"):
        return ""

    old = fetch_verified_contract(chain_id, old_impl)
    new = fetch_verified_contract(chain_id, new_impl)
    if not old or not old.sources or not new or not new.sources:
        return ""

    contract_name = new.contract_name if new.contract_name.isidentifier() else "Proxy"
    title = f"{contract_name} upgrade source diff"
    explorer = EXPLORER_URLS.get(chain_id)
    old_link = f"[`{old_impl}`]({explorer}/address/{old_impl}#code)" if explorer else f"`{old_impl}`"
    new_link = f"[`{new_impl}`]({explorer}/address/{new_impl}#code)" if explorer else f"`{new_impl}`"
    body = f"Chain ID {chain_id}\n\n- Old implementation {old_link}\n- New implementation {new_link}\n\n"
    body += _format_source_diff(old.sources, new.sources)
    if len(f"# {title}\n\n{body}".encode("utf-8")) > MAX_GIST_BYTES:
        logger.info("Source diff for chain=%s old=%s new=%s exceeds Wavey Gist limit", chain_id, old_impl, new_impl)
        return ""
    return upload_to_gist(body, title=title)


def _format_source_diff(old_sources: dict[str, str], new_sources: dict[str, str]) -> str:
    """Render all changed paths as separate, untruncated unified diffs."""
    sections: list[str] = []
    for path in sorted(old_sources.keys() | new_sources.keys()):
        old = old_sources.get(path)
        new = new_sources.get(path)
        if old == new:
            continue

        # Verified filenames and source can contain Markdown delimiters.
        label = " ".join(path.splitlines())
        diff = difflib.unified_diff(
            (old or "").splitlines(keepends=True),
            (new or "").splitlines(keepends=True),
            fromfile=f"a/{label}" if old is not None else "/dev/null",
            tofile=f"b/{label}" if new is not None else "/dev/null",
        )
        patch = "".join(line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n" for line in diff)
        if not patch:
            patch = "+ Empty file added\n" if old is None else "- Empty file removed\n"
        fence = "`" * max(3, 1 + max(map(len, re.findall(r"`+", patch)), default=0))
        label_fence = "`" * (1 + max(map(len, re.findall(r"`+", label)), default=0))
        sections.append(f"## {label_fence} {label} {label_fence}\n\n{fence}diff\n{patch}{fence}")

    return "\n\n".join(sections) if sections else "The verified source files are identical."

"""Render the full transaction report published to Wavey Gist.

The Telegram alert only carries the short AI summary; the linked gist is the
full artifact a reviewer opens. It pairs the LLM's analysis with a
deterministic, code-built **call flow** and **reference table** — the exact
function each call hits, its arguments, and every address rendered as a
block-explorer hyperlink.

The call flow is built here rather than asked of the LLM on purpose: it is
ground truth straight from the decoded calldata, so it can't be hallucinated,
mis-ordered, or summarized away.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

from eth_utils import to_checksum_address

from utils.calldata.decoder import (
    MAX_BYTES_RECURSION_DEPTH,
    DecodedCall,
    split_top_level_types,
    try_decode_inner_calldata,
)
from utils.calldata.role_names import normalize_role_hash
from utils.chains import EXPLORER_URLS, Chain
from utils.related_tokens import RelatedToken

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# The report already opens the section with "## Analysis", so a detail that
# starts with its own "Detailed Analysis" heading would double up.
_REDUNDANT_ANALYSIS_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?\**\s*(?:detailed|full|in-depth)?\s*analysis\s*\**\s*:?\s*\n+",
    re.IGNORECASE,
)


# Standard ERC20 movement functions: signature -> (label, recipient_param_index,
# amount_param_index, is_flow). Shared with the explainer so report amount hints
# and Token Flows classify the same parameters. `is_flow` marks calls that
# actually move a balance (summed into the per-token total); approve only sets
# an allowance, so it's listed but not summed.
TOKEN_MOVE_SIGS: dict[str, tuple[str, int | None, int, bool]] = {
    "transfer(address,uint256)": ("transfer", 0, 1, True),
    "transferFrom(address,address,uint256)": ("transferFrom", 1, 2, True),
    "mint(address,uint256)": ("mint", 0, 1, True),
    "burn(address,uint256)": ("burn", 0, 1, True),
    "approve(address,uint256)": ("approve", 0, 1, False),
}

# ABI names that are never token amounts, even when a related token is known.
_NON_AMOUNT_PARAM_NAMES = frozenset(
    {
        "deadline",
        "timestamp",
        "time",
        "expiry",
        "expiration",
        "epoch",
        "id",
        "marketid",
        "index",
        "nonce",
        "rate",
        "fee",
        "bps",
        "duration",
        "delay",
        "slippage",
        "ltv",
        "shares",
        "share",
    }
)

# Names that can support amount classification only when the target itself is
# the token (related-token getter ``self``). They never pick a token on their own.
_AMOUNT_PARAM_NAMES = frozenset({"amount", "amounts", "assets", "value"})


def token_amount_index(signature: str) -> int | None:
    """Amount-parameter index for a known ERC20 movement signature, else None."""
    spec = TOKEN_MOVE_SIGS.get(signature)
    return spec[2] if spec else None


def is_token_amount_param(
    signature: str,
    param_index: int,
    param_name: str | None,
    token: RelatedToken | None = None,
) -> bool:
    """True when existing evidence says this parameter is a token amount.

    Prefer the exact ERC20 movement signature → amount-index mapping. ABI names
    can exclude (timestamp, marketId, shares) or, when the target *is* the token,
    support ``amount``/``assets``. Names alone never choose a token or scale, and
    share quantities must not inherit an underlying asset's decimals.
    """
    amount_index = token_amount_index(signature)
    if amount_index is not None and param_index == amount_index:
        return True
    if not param_name:
        return False
    normalized = param_name.lstrip("_").lower()
    if normalized in _NON_AMOUNT_PARAM_NAMES:
        return False
    if token is None or token.getter != "self":
        return False
    return normalized in _AMOUNT_PARAM_NAMES


@dataclass(frozen=True)
class CallEntry:
    """One call in the transaction, with the context needed to render it.

    ``call`` is None when the payload could not be decoded. Those entries still
    belong in the call flow: dropping them hides an action and renumbers later
    calls. ``original_index`` is the 1-based input order and must be preserved
    even when neighbors fail to decode.
    """

    target: str
    call: DecodedCall | None = None
    value: int = 0
    param_names: list[str] | None = None
    # Role hash → role name for this call's bytes32 role arguments, so the call
    # flow shows `GOVERNOR` next to the digest instead of the digest alone.
    role_names: dict[str, str] = field(default_factory=dict)
    # The single ERC20 this call's target is denominated in, when exactly one
    # resolved. Applied only to parameters that ``is_token_amount_param`` accepts.
    amount_token: RelatedToken | None = None
    raw_calldata: str = ""
    original_index: int | None = None
    # ``decoded``, ``unknown_selector``, or ``empty_calldata``.
    decode_status: str = "decoded"
    # Deterministic simulation note for this call. Failed sims may appear here
    # as diagnostics; they must not be copied into the LLM risk prompt.
    simulation_note: str = ""


@dataclass(frozen=True)
class ReportContext:
    """Everything the gist report needs beyond the LLM's summary and detail."""

    entries: list[CallEntry] = field(default_factory=list)
    chain_id: int = 0
    labels: dict[str, str] = field(default_factory=dict)
    protocol: str = ""
    label: str = ""
    from_address: str = ""
    # Address the ``label`` names — usually the executing timelock/Safe, but a
    # Safe multisend batch labels the utility contract instead. Linked from the
    # report's Contract header line.
    label_address: str = ""
    # Deterministic protocol-specific facts that belong in the full gist but
    # are not part of the raw calldata flow (for example an Infinifi farm and
    # the non-accounting ERC20 targets configured in its escrow).
    protocol_context: str = ""
    # Addresses introduced by protocol-specific context. These may not occur in
    # calldata but still belong in the report's deterministic reference table.
    related_addresses: list[str] = field(default_factory=list)
    # Deterministic before-state notes (unavailable keys, omitted-key cap).
    state_read_notes: str = ""


@dataclass
class _ReferenceEntry:
    """One address and its accumulated deterministic report usages."""

    address: str
    label: str
    usages: list[tuple[str, str]] = field(default_factory=list)


def checksum_or_none(addr: object) -> str | None:
    """Return the checksummed address, or None if ``addr`` isn't a hex address."""
    if not isinstance(addr, str) or not addr.startswith("0x"):
        return None
    try:
        return str(to_checksum_address(addr))
    except ValueError:
        return None


def explorer_address_url(chain_id: int, address: str) -> str:
    """Block-explorer address URL, or "" when the chain has no configured explorer."""
    explorer = EXPLORER_URLS.get(chain_id)
    checksum = checksum_or_none(address)
    if not explorer or checksum is None:
        return ""
    return f"{explorer}/address/{checksum}"


def address_link(address: str, chain_id: int, labels: dict[str, str] | None = None) -> str:
    """Render an address as a markdown explorer link, suffixed with its label.

    Full addresses are always shown (never truncated) so the reader can copy
    and verify them. Falls back to plain text on chains with no explorer, and
    returns the input unchanged when it isn't a parseable address.
    """
    checksum = checksum_or_none(address)
    if checksum is None:
        return str(address)
    label = (labels or {}).get(checksum)
    url = explorer_address_url(chain_id, checksum)
    rendered = f"[`{checksum}`]({url})" if url else f"`{checksum}`"
    return f"{rendered} ({label})" if label else rendered


def format_address_links_block(addresses: list[str], chain_id: int, labels: dict[str, str] | None = None) -> str:
    """Prompt section listing the exact markdown link to use for each address.

    Handing the LLM ready-made links is what makes the "always hyperlink
    addresses" rule reliable — it copies a line instead of assembling an
    explorer URL from memory (and picking the wrong chain's explorer).
    Returns "" when there is nothing to link.
    """
    lines: list[str] = []
    for addr in addresses:
        rendered = address_link(addr, chain_id, labels)
        if rendered.startswith("["):  # only useful when an explorer link was produced
            lines.append(f"- {rendered}")
    return "\n".join(lines)


def _amount_hint(type_str: str, value: object, token: RelatedToken | None) -> str:
    """Human-readable suffix for a raw token amount, or "" when it doesn't apply.

    Whole-token values are truncated to an integer. Values from 0.1 to under 1
    token retain one truncated decimal place; smaller values are left unannotated
    so they never render as a misleading ``0.0 TOKEN`` hint.
    """
    if token is None or not type_str.startswith("uint"):
        return ""
    if not isinstance(value, int) or isinstance(value, bool):
        return ""
    token_scale = 10**token.decimals
    whole_tokens = value // token_scale
    if whole_tokens >= 1:
        amount = f"{whole_tokens:,}"
    else:
        tenths = (value * 10) // token_scale
        if tenths < 1:
            return ""
        amount = f"0.{tenths}"
    return f" (≈ {amount} {token.symbol})"


def _format_param_value(
    type_str: str,
    value: object,
    chain_id: int,
    labels: dict[str, str],
    token: RelatedToken | None = None,
) -> str:
    """Render a single scalar parameter value for the markdown call flow."""
    if type_str == "address" and isinstance(value, str):
        return address_link(value, chain_id, labels)
    if isinstance(value, bytes):
        return f"`0x{value.hex()}`"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"`{value:,}`{_amount_hint(type_str, value, token)}"
    return f"`{value}`"


def _param_label(type_str: str, name: str | None) -> str:
    """Render a Solidity-style ``type name`` declaration, falling back to bare type."""
    return f"`{type_str} {name}`" if name else f"`{type_str}`"


def array_element_type(type_str: str) -> str | None:
    """``T[]`` / ``T[3]`` → ``T``; None when the type isn't an array."""
    if not type_str.endswith("]"):
        return None
    open_idx = type_str.rfind("[")
    return type_str[:open_idx] if open_idx > 0 else None


def tuple_component_types(type_str: str) -> list[str] | None:
    """``(address,uint256)`` → ``["address", "uint256"]``; None when not a tuple."""
    if not (type_str.startswith("(") and type_str.endswith(")")):
        return None
    inner = type_str[1:-1].strip()
    return split_top_level_types(inner) if inner else []


def _is_composite(type_str: str) -> bool:
    """True for array and tuple types, which render as nested bullets."""
    return array_element_type(type_str) is not None or tuple_component_types(type_str) is not None


def iter_address_values(type_str: str, value: object) -> Iterator[str]:
    """Yield every ``address`` leaf inside a decoded parameter value.

    Walks arrays and tuples (and their nesting) so a struct argument like
    ``(address,address,uint256)`` contributes its addresses to label lookup
    and to the prompt's Address Links section — without this they'd only ever
    be stringified into the report as raw, unlinked text.
    """
    element = array_element_type(type_str)
    if element is not None and isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_address_values(element, item)
        return
    components = tuple_component_types(type_str)
    if components is not None and isinstance(value, (list, tuple)) and len(components) == len(value):
        for component, item in zip(components, value):
            yield from iter_address_values(component, item)
        return
    if type_str == "address" and isinstance(value, str):
        yield value


def _render_param(
    label: str,
    type_str: str,
    value: object,
    chain_id: int,
    labels: dict[str, str],
    indent: str,
    depth: int = 0,
    token: "RelatedToken | None" = None,
) -> list[str]:
    """Render one parameter, expanding arrays and tuples into nested bullets.

    Composites recurse so addresses nested in a struct or an array of structs
    still come out as explorer links rather than a stringified Python tuple.
    Recursion terminates on the type string, which is finite. ``token`` is the
    call target's sole ERC20 and is only passed for parameters already identified
    as token amounts.
    """
    element = array_element_type(type_str)
    if element is not None and isinstance(value, (list, tuple)):
        if not value:
            return [f"{indent}- {label}: _(empty)_"]
        lines = [f"{indent}- {label}:"]
        for i, item in enumerate(value):
            if _is_composite(element):
                # Nested tuples/structs must not inherit a parent amount token.
                lines.extend(_render_param(f"`[{i}]`", element, item, chain_id, labels, indent + "  ", depth, None))
            else:
                lines.append(f"{indent}  - {_format_param_value(element, item, chain_id, labels, token)}")
        return lines

    components = tuple_component_types(type_str)
    if components is not None and isinstance(value, (list, tuple)) and len(components) == len(value):
        if not components:
            return [f"{indent}- {label}: _(empty)_"]
        lines = [f"{indent}- {label}:"]
        for component, item in zip(components, value):
            lines.extend(_render_param(f"`{component}`", component, item, chain_id, labels, indent + "  ", depth, None))
        return lines

    if type_str == "bytes" and depth < MAX_BYTES_RECURSION_DEPTH:
        inner = try_decode_inner_calldata(value)
        if inner is not None:
            lines = [f"{indent}- {label}: ↳ `{inner.signature}`"]
            lines.extend(_format_params(inner, chain_id, labels, None, indent + "  ", depth + 1))
            return lines

    return [f"{indent}- {label}: {_format_param_value(type_str, value, chain_id, labels, token)}"]


def _format_params(
    call: DecodedCall,
    chain_id: int,
    labels: dict[str, str],
    param_names: list[str] | None,
    indent: str,
    depth: int = 0,
    token: "RelatedToken | None" = None,
    role_names: dict[str, str] | None = None,
) -> list[str]:
    """Render a call's parameters as an indented markdown bullet list.

    A ``bytes32`` that ``role_names`` resolves is annotated with its role name.
    Scalars render to exactly one line, so the annotation is appended to the
    line just produced; composites are left alone.
    """
    lines: list[str] = []
    for i, (type_str, value) in enumerate(call.params):
        name = param_names[i] if param_names is not None and i < len(param_names) else None
        hint_token = token if is_token_amount_param(call.signature, i, name, token) else None
        rendered = _render_param(
            _param_label(type_str, name), type_str, value, chain_id, labels, indent, depth, hint_token
        )
        if role_names and type_str == "bytes32" and len(rendered) == 1:
            role = role_names.get(normalize_role_hash(value))
            if role:
                rendered = [f"{rendered[0]} — role **{role}**"]
        lines.extend(rendered)
    return lines


def format_call_flow(ctx: ReportContext) -> str:
    """Render the decoded calls as a numbered markdown flow with explorer links.

    Returns "" when there is nothing to render.
    """
    if not ctx.entries:
        return ""

    lines: list[str] = []
    sender = checksum_or_none(ctx.from_address)
    if sender and sender != ZERO_ADDRESS:
        lines.append(f"**From:** {address_link(sender, ctx.chain_id, ctx.labels)}")
        lines.append("")

    for i, entry in enumerate(ctx.entries, start=1):
        number = entry.original_index if entry.original_index is not None else i
        target = address_link(entry.target, ctx.chain_id, ctx.labels) if entry.target else "_unknown target_"
        if entry.call is None:
            if entry.decode_status == "empty_calldata":
                lines.append(f"{number}. **Empty calldata** on {target} — no function selector")
            else:
                selector = entry.raw_calldata[:10] if len(entry.raw_calldata) >= 10 else entry.raw_calldata or "0x"
                lines.append(f"{number}. **Undecoded calldata** on {target} — unknown selector `{selector}`")
            if entry.value > 0:
                lines.append(f"   - **ETH value:** `{entry.value / 1e18:.6f}` ETH")
            elif entry.value == 0:
                lines.append("   - **ETH value:** `0`")
            if entry.raw_calldata:
                lines.append(f"   - **Calldata:** `{entry.raw_calldata}`")
            lines.append(f"   - **Decode status:** `{entry.decode_status}`")
        else:
            lines.append(f"{number}. **`{entry.call.signature}`** on {target}")
            if entry.value > 0:
                lines.append(f"   - **ETH value:** `{entry.value / 1e18:.6f}` ETH")
            param_lines = _format_params(
                entry.call,
                ctx.chain_id,
                ctx.labels,
                entry.param_names,
                indent="   ",
                token=entry.amount_token,
                role_names=entry.role_names,
            )
            lines.extend(param_lines or ["   - _no inputs_"])
        if entry.simulation_note:
            lines.append(f"   - {entry.simulation_note}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _reference_label(ctx: ReportContext, address: str) -> str:
    """Return the best deterministic label available for a reference row."""
    if checksum_or_none(ctx.label_address) == address and ctx.label:
        return ctx.label
    return ctx.labels.get(address, "")


def _add_reference(
    entries: dict[str, _ReferenceEntry],
    ctx: ReportContext,
    raw_address: str,
    role: str,
    description: str,
) -> None:
    """Add or enrich one checksummed reference entry."""
    address = checksum_or_none(raw_address)
    if address is None or address == ZERO_ADDRESS:
        return
    key = address.lower()
    entry = entries.setdefault(key, _ReferenceEntry(address, _reference_label(ctx, address)))
    usage = (role, description)
    if usage not in entry.usages:
        entry.usages.append(usage)


def _table_cell(value: str) -> str:
    """Escape dynamic text for one GitHub-flavored Markdown table cell."""
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _iter_inner_calldata(type_str: str, value: object) -> Iterator[DecodedCall]:
    """Yield decodable calldata from bytes leaves inside arrays and tuples."""
    element = array_element_type(type_str)
    if element is not None and isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_inner_calldata(element, item)
        return
    components = tuple_component_types(type_str)
    if components is not None and isinstance(value, (list, tuple)) and len(components) == len(value):
        for component, item in zip(components, value):
            yield from _iter_inner_calldata(component, item)
        return
    if type_str == "bytes":
        inner = try_decode_inner_calldata(value)
        if inner is not None:
            yield inner


def _iter_reference_arguments(
    call: DecodedCall,
    param_names: list[str] | None,
    depth: int = 0,
) -> Iterator[tuple[str, str]]:
    """Yield address arguments and factual descriptions, including nested calldata."""
    for index, (type_str, value) in enumerate(call.params):
        name = param_names[index] if param_names is not None and index < len(param_names) else ""
        parameter = f"`{name}`" if name else f"argument {index + 1}"
        description = f"Passed as {parameter} to `{call.signature}`"
        for address in iter_address_values(type_str, value):
            yield address, description
        if depth < MAX_BYTES_RECURSION_DEPTH:
            for inner in _iter_inner_calldata(type_str, value):
                yield from _iter_reference_arguments(inner, None, depth + 1)


def format_reference_table(ctx: ReportContext) -> str:
    """Render addresses used by the transaction as a deterministic table."""
    references: dict[str, _ReferenceEntry] = {}
    _add_reference(references, ctx, ctx.from_address, "Executor", "Execution authority for the governance transaction")

    label_address = checksum_or_none(ctx.label_address)
    sender = checksum_or_none(ctx.from_address)
    if label_address is not None and label_address != sender:
        _add_reference(references, ctx, label_address, "Alert contract", "Contract named in the report header")

    for entry in ctx.entries:
        if entry.call is None:
            received = (
                "undecoded calldata"
                if entry.decode_status != "empty_calldata"
                else "empty calldata (no function selector)"
            )
            _add_reference(references, ctx, entry.target, "Call target", f"Receives {received}")
            continue
        _add_reference(
            references,
            ctx,
            entry.target,
            "Call target",
            f"Receives `{entry.call.signature}`",
        )
        for address, description in _iter_reference_arguments(entry.call, entry.param_names):
            _add_reference(references, ctx, address, "Calldata argument", description)

    context_description = (
        f"Resolved by the {ctx.protocol} protocol adapter" if ctx.protocol else "Resolved by protocol context"
    )
    for address in ctx.related_addresses:
        _add_reference(references, ctx, address, "Protocol context", context_description)

    if not references:
        return ""

    lines = ["| Address | Label | Role | Description |", "|---|---|---|---|"]
    for reference in references.values():
        address = address_link(reference.address, ctx.chain_id)
        label = _table_cell(reference.label) or "—"
        roles = _table_cell("; ".join(dict.fromkeys(role for role, _description in reference.usages)))
        descriptions = "<br>".join(
            f"**{_table_cell(role)}:** {_table_cell(description)}" for role, description in reference.usages
        )
        lines.append(f"| {address} | {label} | {roles} | {descriptions} |")
    return "\n".join(lines)


def _chain_name(chain_id: int) -> str:
    try:
        return str(Chain.from_chain_id(chain_id).network_name).capitalize()
    except ValueError:
        return f"Chain {chain_id}"


def _format_metadata(ctx: ReportContext, risk_tag: str) -> str:
    """Header bullet list: protocol, contract label, chain, risk."""
    lines: list[str] = []
    if ctx.protocol:
        lines.append(f"- **Protocol:** {ctx.protocol}")
    if ctx.label:
        # Link the label to the contract it names, so the header itself is
        # clickable rather than a bare name the reader has to go look up.
        linked = address_link(ctx.label_address, ctx.chain_id) if ctx.label_address else ""
        lines.append(
            f"- **Contract:** {ctx.label} — {linked}" if linked.startswith("[") else f"- **Contract:** {ctx.label}"
        )
    if ctx.chain_id:
        lines.append(f"- **Chain:** {_chain_name(ctx.chain_id)} (chain id {ctx.chain_id})")
    if risk_tag:
        lines.append(f"- **Risk:** {risk_tag}")
    return "\n".join(lines)


def build_title(ctx: ReportContext, risk_tag: str = "", now: datetime | None = None, fallback: str = "") -> str:
    """Gist title: ``<contract> - <DD/MM/YYYY HH:MM> - <RISK>``.

    Naming the contract and the time makes a list of gists scannable — the
    previous constant title left every report looking identical. The timestamp
    is UTC (the runners' clock); ``now`` is injectable for tests.

    Args:
        ctx: Report context; its ``label`` (else ``protocol``) names the report.
        risk_tag: LOW / MEDIUM / HIGH / CRITICAL, appended when known.
        now: Timestamp to render. Defaults to the current UTC time.
        fallback: Name to use when the context has neither label nor protocol.

    Returns:
        The title string; never empty as long as ``fallback`` is set.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%d/%m/%Y %H:%M")
    parts = [ctx.label or ctx.protocol or fallback, stamp]
    if risk_tag:
        parts.append(risk_tag)
    return " - ".join(part for part in parts if part)


def build_report(summary: str, detail: str, ctx: ReportContext, risk_tag: str = "") -> str:
    """Assemble the full markdown gist body.

    Sections: metadata header, the Telegram-visible summary (so the gist is
    self-contained), the LLM's analysis, the deterministic call flow, optional
    protocol context, and a deterministic address reference.

    Args:
        summary: The authoritative TLDR, risk tag already stripped by the caller.
        detail: The LLM's detailed analysis.
        ctx: Decoded calls, labels, and alert metadata.
        risk_tag: LOW / MEDIUM / HIGH / CRITICAL, when known.

    Returns:
        Markdown body, or "" when there is nothing worth publishing.
    """
    call_flow = format_call_flow(ctx)
    if not detail and not summary and not call_flow:
        return ""

    sections: list[str] = []
    metadata = _format_metadata(ctx, risk_tag)
    if metadata:
        sections.append(metadata)
    if summary:
        sections.append(f"## Summary\n\n{summary}")
    if detail:
        sections.append(f"## Analysis\n\n{_REDUNDANT_ANALYSIS_HEADING_RE.sub('', detail)}")
    if call_flow:
        sections.append(f"## Call Flow\n\n{call_flow}")
    if ctx.state_read_notes:
        sections.append(f"## Current State\n\n{ctx.state_read_notes}")
    if ctx.protocol_context:
        sections.append(f"## Protocol Context\n\n{ctx.protocol_context}")
    reference = format_reference_table(ctx)
    if reference:
        sections.append(f"## Reference\n\n{reference}")
    return "\n\n".join(sections)

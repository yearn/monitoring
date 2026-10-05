"""Registry of protocol-specific LLM context adapters.

Some governance calls carry facts the generic resolvers cannot reach: an
Infinifi escrow hides the farm that owns it, a 3Jane ``setConfig`` identifies
its parameter only by ``keccak256`` hash. Each protocol adapter resolves those
facts deterministically — verified ABIs, on-chain reads, checked-in name
tables — and this module fans one call out to whichever adapters claim the
alert's protocol.

Adapters are responsible for their own guards: each returns an empty list for
protocols and chains it does not handle, so registration order carries no
meaning and adding a protocol is one row in ``_ADAPTERS``. Most guard on the
alert's protocol; the Yearn V3 adapter guards on call shape and an on-chain
``apiVersion()`` instead, because the same vault code is governed by Yearn and
third-party curators alike, and the control-transfer, permission-grant and
timelock-execution adapters guard on call shape alone, since handing over
management, opening an allowlist or releasing a timelock operation means the
same thing everywhere.

Adapters see the inner calls of an executed timelock batch as well as the
wrapper (see :func:`expand_executed_calls`).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from utils.calldata.decoder import DecodedCall, decode_calldata
from utils.calldata.wrappers import unwrap_executed_calls
from utils.llm.control_transfer_context import (
    format_control_transfer_prompt,
    format_control_transfer_report,
    resolve_control_transfer_context,
)
from utils.llm.infinifi_context import (
    format_infinifi_prompt,
    format_infinifi_report,
    resolve_infinifi_context,
)
from utils.llm.infinifi_outland_context import (
    format_outland_prompt,
    format_outland_report,
    resolve_outland_context,
)
from utils.llm.pendle_context import (
    format_pendle_prompt,
    format_pendle_report,
    resolve_pendle_context,
)
from utils.llm.permission_grant_context import (
    format_permission_grant_prompt,
    format_permission_grant_report,
    resolve_permission_grant_context,
)
from utils.llm.threejane_context import (
    format_threejane_prompt,
    format_threejane_report,
    resolve_threejane_context,
)
from utils.llm.timelock_execution_context import (
    format_timelock_execution_prompt,
    format_timelock_execution_report,
    resolve_timelock_execution_context,
)
from utils.llm.yearn_v3_context import (
    format_yearn_v3_prompt,
    format_yearn_v3_report,
    resolve_yearn_v3_context,
)
from utils.logger import get_logger
from utils.proxy import MAX_WRAPPER_DEPTH

logger = get_logger("utils.llm.protocol_context")


@dataclass(frozen=True)
class _Adapter:
    """One protocol's resolver plus its prompt and report renderers."""

    name: str
    resolve: Callable[[str, int, list[tuple[str, DecodedCall]]], list[Any]]
    format_prompt: Callable[[list[Any]], str]
    format_report: Callable[[list[Any], int, dict[str, str]], str]


_ADAPTERS: tuple[_Adapter, ...] = (
    _Adapter("infinifi", resolve_infinifi_context, format_infinifi_prompt, format_infinifi_report),
    _Adapter("infinifi-outland", resolve_outland_context, format_outland_prompt, format_outland_report),
    _Adapter("3jane", resolve_threejane_context, format_threejane_prompt, format_threejane_report),
    _Adapter("pendle", resolve_pendle_context, format_pendle_prompt, format_pendle_report),
    _Adapter("yearn-v3", resolve_yearn_v3_context, format_yearn_v3_prompt, format_yearn_v3_report),
    _Adapter(
        "control-transfer",
        resolve_control_transfer_context,
        format_control_transfer_prompt,
        format_control_transfer_report,
    ),
    _Adapter(
        "permission-grant",
        resolve_permission_grant_context,
        format_permission_grant_prompt,
        format_permission_grant_report,
    ),
    _Adapter(
        "timelock-execution",
        resolve_timelock_execution_context,
        format_timelock_execution_prompt,
        format_timelock_execution_report,
    ),
)


def expand_executed_calls(
    chain_id: int,
    calls: list[tuple[str, str, DecodedCall | None]],
) -> list[tuple[str, DecodedCall]]:
    """Decoded calls in execution order, with the inner calls of executing wrappers spliced in.

    A Safe that runs a timelock ``executeBatch`` sends one call whose payloads
    are the real actions. Adapters keyed on call shape saw only the wrapper: a
    batch whose nested ``add_strategy`` registers a strategy that a later
    top-level ``update_debt`` funds was reported as reverting on an inactive
    strategy. Each wrapper is kept, followed by its decoded inner calls, so
    adapters that explain the wrapper itself still see it.

    Args:
        chain_id: Chain the transaction executes on, for ABI-backed decoding.
        calls: (target, calldata, decoded call or None) in execution order.

    Returns:
        (target, decoded call) pairs; undecodable calls are dropped.
    """
    expanded: list[tuple[str, DecodedCall]] = []

    def visit(target: str, data: str, decoded: DecodedCall | None, depth: int) -> None:
        if decoded is not None:
            expanded.append((target, decoded))
        if depth >= MAX_WRAPPER_DEPTH:
            return
        for inner in unwrap_executed_calls(data):
            visit(
                inner.target, inner.data, decode_calldata(inner.data, chain_id=chain_id, target=inner.target), depth + 1
            )

    for target, data, decoded in calls:
        visit(target, data, decoded, 0)
    return expanded


@dataclass(frozen=True)
class ResolvedProtocolContext:
    """Rendered protocol context for one alert, empty when no adapter matched."""

    prompt: str = ""
    report: str = ""
    addresses: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)


def resolve_protocol_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
    labels: dict[str, str] | None = None,
) -> ResolvedProtocolContext:
    """Resolve and render protocol-specific context from every matching adapter.

    Args:
        protocol: Alert protocol name, matched case-insensitively by each adapter.
        chain_id: Chain the transaction executes on.
        targets_and_calls: Decoded calls paired with the address each one targets.
        labels: Address labels used when rendering the report section.

    Returns:
        Rendered prompt and report text plus the addresses and labels the
        adapters introduced. Adapter failures are logged and skipped — context
        is an enrichment and must never block a governance alert.
    """
    prompts: list[str] = []
    reports: list[str] = []
    addresses: list[str] = []
    resolved_labels: dict[str, str] = {}

    for adapter in _ADAPTERS:
        try:
            contexts = adapter.resolve(protocol, chain_id, targets_and_calls)
        except Exception as error:  # noqa: BLE001 - one adapter must not break the alert
            logger.info("Protocol context adapter %s failed: %s", adapter.name, error)
            continue
        if not contexts:
            continue
        for context in contexts:
            addresses.extend(context.addresses)
            for address, label in context.labels.items():
                resolved_labels.setdefault(address, label)
        prompts.append(adapter.format_prompt(contexts))
        reports.append(adapter.format_report(contexts, chain_id, {**(labels or {}), **resolved_labels}))

    return ResolvedProtocolContext(
        prompt="\n\n".join(part for part in prompts if part),
        report="\n\n".join(part for part in reports if part),
        addresses=list(dict.fromkeys(addresses)),
        labels=resolved_labels,
    )

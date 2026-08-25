"""Registry of protocol-specific LLM context adapters.

Some governance calls carry facts the generic resolvers cannot reach: an
Infinifi escrow hides the farm that owns it, a 3Jane ``setConfig`` identifies
its parameter only by ``keccak256`` hash. Each protocol adapter resolves those
facts deterministically — verified ABIs, on-chain reads, checked-in name
tables — and this module fans one call out to whichever adapters claim the
alert's protocol.

Adapters are responsible for their own guards: each returns an empty list for
protocols and chains it does not handle, so registration order carries no
meaning and adding a protocol is one row in ``_ADAPTERS``.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from utils.calldata.decoder import DecodedCall
from utils.llm.infinifi_context import (
    format_infinifi_prompt,
    format_infinifi_report,
    resolve_infinifi_context,
)
from utils.llm.threejane_context import (
    format_threejane_prompt,
    format_threejane_report,
    resolve_threejane_context,
)
from utils.logger import get_logger

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
    _Adapter("3jane", resolve_threejane_context, format_threejane_prompt, format_threejane_report),
)


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

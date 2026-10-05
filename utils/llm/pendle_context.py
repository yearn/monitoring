"""Enrich PendleSwap upgrades with integration scope and verified implementation code."""

import re
from dataclasses import dataclass

from eth_utils import to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.llm.report import address_link, checksum_or_none
from utils.logger import get_logger
from utils.proxy import get_current_implementation
from utils.solidity_text import contract_functions, declares_contract, strip_noise, struct_definitions
from utils.source_context import fetch_verified_contract
from utils.verified_contract import VerifiedContract
from utils.web3_wrapper import ChainManager

logger = get_logger("utils.llm.pendle_context")

# Pendle's published deployments/1-core.json and deployments/42161-core.json.
PENDLE_SWAP = "0xd4F480965D2347d421F1bEC7F545682E5Ec2151D"
SUPPORTED_CHAINS = {1, 42161}
_REFERENCE_BASE = (
    "https://github.com/pendle-finance/pendle-core-v2-public/blob/87685c89d05087535e9b9647eeda0e3d297d1f06"
)
_ROUTER_REFERENCE = f"{_REFERENCE_BASE}/contracts/router/base/ActionBase.sol"
_INTEGRATION_CONTEXT = (
    "PendleSwap is the external swap-aggregator adapter, separate from Pendle markets, PT/YT "
    "contracts and SY implementations. This call upgrades the adapter only.\n"
    "Integration reference (standard ActionBase router flow, not a live trace): the router "
    "pre-funds PendleSwap with ERC20 input, or sends native input with the swap call. "
    "It calls PendleSwap as an optional aggregator leg when minting/redeeming SY. "
    "NONE and ETH_WETH branches bypass this adapter. Output returned to msg.sender in this "
    "flow goes to the calling router for the next step, not necessarily to the end user. "
    "The redeem flow separately checks TokenOutput.minTokenOut, and the mint flow supplies "
    "minSyOut to SY.deposit. These are caller-supplied constraints, not proof every caller "
    "uses a nonzero minimum.\n"
    "Do not infer current balances, route usage, custody guarantees or the impact on all "
    "Pendle deposits from this architecture. Assess the affected routes using the old/new "
    "code below and the Proxy Upgrade diff; unchanged ABI does not prove unchanged payload "
    "semantics. Removing an ODOS scaling branch does not establish removal of all ODOS "
    "execution: inspect the needScale=false external-router dispatch separately. Describe "
    "a scaler removal as 'removes ODOS calldata scaling' unless the code proves all routes "
    "are disabled. Storage UNKNOWN is a validation gap, not a proven collision, and does "
    "not by itself establish a higher likelihood of storage collisions. An upgrade-only "
    "simulation does not test subsequent swaps. No fixed risk rating follows from this context."
)
_SOURCE_MEMBERS = {
    "swap",
    "_zeroExSwap",
    "_getScaledInputData",
    "_approveForExtRouter",
    "_safeApproveInfV2",
    "_authorizeUpgrade",
}
_OWNER_ABI = [
    {
        "type": "function",
        "name": "owner",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address", "name": ""}],
    }
]


@dataclass(frozen=True)
class PendleSwapContext:
    """Implementation evidence and current proxy owner for one adapter upgrade."""

    proxy: str
    implementation: str
    current_implementation: str | None
    owner: str | None
    current_source: str
    proposed_source: str
    is_upgrade_and_call: bool

    @property
    def addresses(self) -> list[str]:
        """Addresses introduced by this context."""
        return [
            value
            for value in (
                self.proxy,
                self.implementation,
                self.current_implementation,
                self.owner,
            )
            if value
        ]

    @property
    def labels(self) -> dict[str, str]:
        """Name the known proxy without assuming the replacement's identity."""
        return {self.proxy: "PendleSwap"}


def _source_evidence(record: VerifiedContract | None) -> str:
    """Extract complete members from the deployed target, plus its enum and ownership guard."""
    if record is None or record.contract_name != "PendleSwap" or not record.compilation_target:
        return "PendleSwap target source unavailable or replacement is a different contract."
    sections = [f"Verified target: {record.contract_name} in {record.contract_file}"]
    members = contract_functions(record.target_source, record.contract_name) or []
    for member in members:
        if member.name in _SOURCE_MEMBERS:
            sections.append(record.target_source[slice(*member.span)])
    sections.extend(_supporting_evidence(record))
    return "\n\n".join(sections)


def _supporting_evidence(record: VerifiedContract) -> list[str]:
    """Read uniquely declared payload and ownership definitions from the verified bundle."""
    sections: list[str] = []
    for name in ("IPSwapAggregator", "BoringOwnableUpgradeableV2"):
        matches = [(path, source) for path, source in record.sources.items() if declares_contract(source, name)]
        if len(matches) != 1:
            continue
        path, source = matches[0]
        if name == "IPSwapAggregator":
            sections.append(f"Verified swap payload/enum in {path}:\n{_payload_evidence(source)}")
        else:
            for member in contract_functions(source, name) or []:
                if member.name == "onlyOwner":
                    sections.append(f"Verified ownership guard in {path}:\n{source[slice(*member.span)]}")
    return sections


def _payload_evidence(source: str) -> str:
    """Extract only swap declarations, even when verification provides a flattened file."""
    sections = [struct_definitions(source, None).get("SwapData", "SwapData declaration unavailable.")]
    enums = list(re.finditer(r"\benum\s+SwapType\s*\{[^{}]*\}", strip_noise(source)))
    if len(enums) == 1:
        sections.append(source[enums[0].start() : enums[0].end()])
    else:
        sections.append("SwapType enum unavailable or ambiguous.")
    return "\n".join(sections)


def _read_owner(chain_id: int, proxy: str) -> str | None:
    """Read ownership at the proxy; unavailable state must not hide the source evidence."""
    try:
        client = ChainManager.get_client(Chain.from_chain_id(chain_id))
        contract = client.eth.contract(address=to_checksum_address(proxy), abi=_OWNER_ABI)
        return to_checksum_address(contract.functions.owner().call())
    except Exception as error:  # noqa: BLE001 - optional enrichment must not block the alert
        logger.info("PendleSwap owner read failed on chain %s for %s: %s", chain_id, proxy, error)
        return None


def resolve_pendle_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[PendleSwapContext]:
    """Resolve direct PendleSwap upgrades on Ethereum and Arbitrum, including Safe batches.

    Args:
        protocol: Alert protocol, matched case-insensitively.
        chain_id: Chain containing the published PendleSwap proxy.
        targets_and_calls: Decoded calls and their targets.

    Returns:
        Integration context, verified source excerpts and live owner for each
        distinct upgrade. Failed enrichments are logged without blocking alerts.
    """
    if protocol.lower() != "pendle" or chain_id not in SUPPORTED_CHAINS:
        return []
    contexts: list[PendleSwapContext] = []
    seen: set[tuple[str, bool]] = set()
    for target, call in targets_and_calls:
        implementation = _upgrade_implementation(target, call)
        if implementation is None:
            continue
        is_upgrade_and_call = call.signature == "upgradeToAndCall(address,bytes)"
        key = (implementation.lower(), is_upgrade_and_call)
        if key in seen:
            continue
        seen.add(key)
        try:
            current = get_current_implementation(target, chain_id)
            contexts.append(
                PendleSwapContext(
                    proxy=to_checksum_address(target),
                    implementation=implementation,
                    current_implementation=current,
                    owner=_read_owner(chain_id, target),
                    current_source=_source_evidence(fetch_verified_contract(chain_id, current) if current else None),
                    proposed_source=_source_evidence(fetch_verified_contract(chain_id, implementation)),
                    is_upgrade_and_call=is_upgrade_and_call,
                )
            )
        except Exception as error:  # noqa: BLE001 - retain other batch members on enrichment failure
            logger.info("PendleSwap context failed on chain %s for %s: %s", chain_id, implementation, error)
    return contexts


def _upgrade_implementation(target: str, call: DecodedCall) -> str | None:
    """Accept only well-formed upgrade arguments targeting the published swap adapter."""
    if target.lower() != PENDLE_SWAP.lower() or call.signature not in {
        "upgradeTo(address)",
        "upgradeToAndCall(address,bytes)",
    }:
        return None
    expected_types = ("address", "bytes") if call.signature == "upgradeToAndCall(address,bytes)" else ("address",)
    if tuple(param_type for param_type, _ in call.params) != expected_types:
        return None
    return checksum_or_none(call.params[0][1])


def format_pendle_prompt(contexts: list[PendleSwapContext]) -> str:
    """Render integration references separately from live state and verified code."""
    return "\n\n".join(_format_context(context, None, {}) for context in contexts)


def format_pendle_report(
    contexts: list[PendleSwapContext],
    chain_id: int,
    labels: dict[str, str],
) -> str:
    """Render the same evidence with full explorer links for the gist report."""
    return "\n\n".join(_format_context(context, chain_id, labels) for context in contexts)


def _format_context(context: PendleSwapContext, chain_id: int | None, labels: dict[str, str]) -> str:
    """Keep prompt and report facts identical, varying only address rendering."""

    def link(address: str | None) -> str:
        """Link known addresses and keep missing state explicit."""
        return address_link(address, chain_id, labels) if address and chain_id else address or "unavailable"

    execution = (
        "upgradeToAndCall includes a bytes payload; inspect its initialization/migration effects separately."
        if context.is_upgrade_and_call
        else "upgradeTo has no initialization/migration payload."
    )
    return (
        f"PendleSwap adapter upgrade on {link(context.proxy)}\n{_INTEGRATION_CONTEXT}\n"
        f"Router integration reference: {_ROUTER_REFERENCE}\n"
        f"Current implementation: {link(context.current_implementation)}\n"
        f"Proposed implementation: {link(context.implementation)}\n"
        f"Current proxy owner() (live read): {link(context.owner)}\n{execution}\n"
        "Authorization must be assessed from the hook AND ownership guard below; "
        "a modifier name alone is not proof.\n"
        f"Current implementation evidence:\n```solidity\n{context.current_source}\n```\n"
        f"Proposed implementation evidence:\n```solidity\n{context.proposed_source}\n```"
    )

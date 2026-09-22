"""Risk anchors — offline floors for known dangerous/safe function selectors.

The LLM picks LOW/MEDIUM/HIGH/CRITICAL from scratch every alert. That makes the
same call land at different levels on different runs ("setMaxSlippage" → LOW
one day, MEDIUM the next). Anchoring well-understood operations to a typical
risk level stabilizes verdicts and gives the LLM a reference frame.

These are *anchors*, not verdicts. The LLM is instructed to start from the
anchor and adjust based on parameters. A grantRole call for the PAUSER role
to a known multisig isn't HIGH; an upgrade of a vault to a fresh-bytecode
implementation isn't LOW.

Skipped intentionally:
- ERC20 transfer/approve — too context-dependent (rewards distribution vs
  exit liquidity look identical).
- Generic timelock schedule/execute — the inner call is the thing being
  judged, not the wrapper.
"""

from dataclasses import dataclass
from typing import Any

from eth_utils import function_signature_to_4byte_selector


@dataclass(frozen=True)
class RiskAnchor:
    """Typical risk level + one-line rationale for the LLM."""

    level: str  # LOW / MEDIUM / HIGH / CRITICAL
    rationale: str


# Signature → RiskAnchor. Selectors are derived below so the table can't drift
# from the canonical signature text.
_ANCHORS_BY_SIGNATURE: dict[str, RiskAnchor] = {
    # Pausing — reversible, defensive
    "pause()": RiskAnchor("LOW", "pause() is a defensive emergency stop; reversible"),
    "unpause()": RiskAnchor("LOW", "unpause() restores normal operation"),
    # Access control — depends on which role/who, but the operation itself is high-trust
    "grantRole(bytes32,address)": RiskAnchor("MEDIUM", "grantRole(): elevate to HIGH if role is owner/admin/upgrader"),
    "revokeRole(bytes32,address)": RiskAnchor("MEDIUM", "revokeRole(): elevate to HIGH if removing an emergency role"),
    "renounceRole(bytes32,address)": RiskAnchor(
        "LOW", "renounceRole() permanently drops a privilege; usually defensive"
    ),
    # Ownership — irreversible authority change
    "transferOwnership(address)": RiskAnchor("HIGH", "transferOwnership(): hands over full admin control"),
    "renounceOwnership()": RiskAnchor("HIGH", "renounceOwnership(): irrevocably abandons admin"),
    "acceptOwnership()": RiskAnchor("HIGH", "acceptOwnership(): completes an Ownable2Step handover"),
    # Token supply
    "mint(address,uint256)": RiskAnchor(
        "MEDIUM", "mint(address,uint256): new supply — elevate to HIGH if large or unbacked"
    ),
    # Proxy upgrades — replaces all code; impl-diff section should drive the verdict.
    # The *AndCall variants only run a payload when their bytes argument is non-empty;
    # `_refine_upgrade_anchor` rewrites the rationale from the actual argument.
    "upgradeTo(address)": RiskAnchor("HIGH", "upgradeTo(): replaces all implementation code"),
    "upgradeToAndCall(address,bytes)": RiskAnchor(
        "HIGH", "upgradeToAndCall(): replaces all implementation code; delegatecalls the data argument if non-empty"
    ),
    "upgradeAndCall(address,address,bytes)": RiskAnchor(
        "HIGH",
        "upgradeAndCall() via ProxyAdmin: replaces all implementation code; "
        "delegatecalls the data argument if non-empty",
    ),
    # Admin parameter changes
    "setDelay(uint256)": RiskAnchor("MEDIUM", "setDelay(): timelock window change — direction & magnitude matter"),
    "setPendingAdmin(address)": RiskAnchor(
        "MEDIUM", "setPendingAdmin(): new admin candidate — confirm + accept needed"
    ),
    "acceptAdmin()": RiskAnchor("HIGH", "acceptAdmin(): completes an admin handover"),
    # Diamond / facet operations
    "diamondCut((address,uint8,bytes4[])[],address,bytes)": RiskAnchor(
        "HIGH", "diamondCut(): replaces/adds/removes selectors — bytecode-level change"
    ),
    # Gnosis Safe self-administration — changes who/what can move the multisig's funds
    "addOwnerWithThreshold(address,uint256)": RiskAnchor("HIGH", "addOwnerWithThreshold(): adds a Safe signer"),
    "removeOwner(address,address,uint256)": RiskAnchor("HIGH", "removeOwner(): removes a Safe signer"),
    "swapOwner(address,address,address)": RiskAnchor("HIGH", "swapOwner(): replaces a Safe signer"),
    "changeThreshold(uint256)": RiskAnchor("HIGH", "changeThreshold(): changes signatures required to execute"),
    "enableModule(address)": RiskAnchor("CRITICAL", "enableModule(): a module can move funds with NO owner signatures"),
    "disableModule(address,address)": RiskAnchor("MEDIUM", "disableModule(): removes a module — usually defensive"),
    "setGuard(address)": RiskAnchor("HIGH", "setGuard(): a guard can permit or block every Safe transaction"),
    "setFallbackHandler(address)": RiskAnchor("MEDIUM", "setFallbackHandler(): changes the Safe's fallback behavior"),
}

# Selector → RiskAnchor. Lowercase, with 0x prefix to match the decoder's format.
_ANCHORS: dict[str, RiskAnchor] = {
    "0x" + function_signature_to_4byte_selector(signature).hex(): anchor
    for signature, anchor in _ANCHORS_BY_SIGNATURE.items()
}

# Upgrade variants whose trailing `bytes` argument decides whether a payload runs.
_UPGRADE_AND_CALL_SELECTORS = frozenset(
    "0x" + function_signature_to_4byte_selector(sig).hex()
    for sig in ("upgradeToAndCall(address,bytes)", "upgradeAndCall(address,address,bytes)")
)


def lookup(selector_hex: str, params: list[tuple[str, Any]] | None = None) -> RiskAnchor | None:
    """Return the risk anchor for a selector, or None if no anchor is registered.

    ``params`` are the decoded arguments. They're optional, but passing them
    lets an anchor whose meaning depends on an argument say what this specific
    call does instead of describing both cases (see :func:`_refine_upgrade_anchor`).
    """
    if not selector_hex or not selector_hex.startswith("0x"):
        return None
    anchor = _ANCHORS.get(selector_hex.lower())
    if anchor is None or params is None:
        return anchor
    if selector_hex.lower() in _UPGRADE_AND_CALL_SELECTORS:
        return _refine_upgrade_anchor(anchor, params)
    return anchor


def _refine_upgrade_anchor(anchor: RiskAnchor, params: list[tuple[str, Any]]) -> RiskAnchor:
    """Say whether this `upgrade*AndCall` actually delegatecalls a payload.

    An empty ``data`` argument means the call replaces the implementation and
    nothing else — OpenZeppelin's proxy skips the delegatecall entirely. Stating
    "runs an initializer" regardless invents a second effect that never happened,
    which is exactly the kind of detail the model then builds a verdict on.
    """
    payload = next((value for type_str, value in reversed(params) if type_str == "bytes"), None)
    if payload is None:
        return anchor
    if _is_empty_bytes(payload):
        return RiskAnchor(
            anchor.level,
            f"{anchor.rationale.split(':')[0]}: replaces the implementation only "
            "(data argument is empty — no delegatecall)",
        )
    return RiskAnchor(
        anchor.level,
        f"{anchor.rationale.split(':')[0]}: replaces the implementation AND delegatecalls "
        "the non-empty data payload — judge that payload too",
    )


def _is_empty_bytes(value: Any) -> bool:
    """True for the decoder's representations of empty `bytes`."""
    if isinstance(value, (bytes, bytearray)):
        return len(value) == 0
    if isinstance(value, str):
        return value.removeprefix("0x") == ""
    return False


def format_anchors_block(signatures_and_anchors: list[tuple[str, RiskAnchor]]) -> str:
    """Render a prompt-ready section listing the anchors for the current calls."""
    if not signatures_and_anchors:
        return ""
    lines = ["These calls have observed risk profiles. Start from the anchor and adjust based on parameters."]
    for sig, anchor in signatures_and_anchors:
        lines.append(f"- {sig} → typically {anchor.level} ({anchor.rationale})")
    return "\n".join(lines)

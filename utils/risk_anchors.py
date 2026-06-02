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


@dataclass(frozen=True)
class RiskAnchor:
    """Typical risk level + one-line rationale for the LLM."""

    level: str  # LOW / MEDIUM / HIGH / CRITICAL
    rationale: str


# Selector → RiskAnchor. Lowercase, with 0x prefix to match the decoder's format.
_ANCHORS: dict[str, RiskAnchor] = {
    # Pausing — reversible, defensive
    "0x8456cb59": RiskAnchor("LOW", "pause() is a defensive emergency stop; reversible"),
    "0x3f4ba83a": RiskAnchor("LOW", "unpause() restores normal operation"),
    # Access control — depends on which role/who, but the operation itself is high-trust
    "0x2f2ff15d": RiskAnchor("MEDIUM", "grantRole(): elevate to HIGH if role is owner/admin/upgrader"),
    "0xd547741f": RiskAnchor("MEDIUM", "revokeRole(): elevate to HIGH if removing an emergency role"),
    "0x36568abe": RiskAnchor("LOW", "renounceRole() permanently drops a privilege; usually defensive"),
    # Ownership — irreversible authority change
    "0xf2fde38b": RiskAnchor("HIGH", "transferOwnership(): hands over full admin control"),
    "0x715018a6": RiskAnchor("HIGH", "renounceOwnership(): irrevocably abandons admin"),
    # Proxy upgrades — replaces all code; impl-diff section should drive the verdict
    "0x3659cfe6": RiskAnchor("HIGH", "upgradeTo(): replaces all implementation code"),
    "0x4f1ef286": RiskAnchor("HIGH", "upgradeToAndCall(): replaces code AND runs initializer"),
    "0x9623609d": RiskAnchor("HIGH", "upgradeAndCall() via ProxyAdmin: same as above, routed via admin"),
    # Admin parameter changes
    "0xe177246e": RiskAnchor("MEDIUM", "setDelay(): timelock window change — direction & magnitude matter"),
    "0x4dd18bf5": RiskAnchor("MEDIUM", "setPendingAdmin(): new admin candidate — confirm + accept needed"),
    "0x0e18b681": RiskAnchor("HIGH", "acceptAdmin(): completes an admin handover"),
    # Diamond / facet operations
    "0x1f931c1c": RiskAnchor("HIGH", "diamondCut(): replaces/adds/removes selectors — bytecode-level change"),
}


def lookup(selector_hex: str) -> RiskAnchor | None:
    """Return the risk anchor for a selector, or None if no anchor is registered."""
    if not selector_hex or not selector_hex.startswith("0x"):
        return None
    return _ANCHORS.get(selector_hex.lower())


def format_anchors_block(signatures_and_anchors: list[tuple[str, RiskAnchor]]) -> str:
    """Render a prompt-ready section listing the anchors for the current calls."""
    if not signatures_and_anchors:
        return ""
    lines = ["These calls have observed risk profiles. Start from the anchor and adjust based on parameters."]
    for sig, anchor in signatures_and_anchors:
        lines.append(f"- {sig} → typically {anchor.level} ({anchor.rationale})")
    return "\n".join(lines)

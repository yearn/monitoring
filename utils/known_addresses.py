"""Curated address → label registry.

The highest-priority label source, ahead of Etherscan / swiss-knife, for
addresses those backends don't name usefully — governance multisigs, known
EOAs, and canonical burn addresses. A correct label here lets the LLM reason
about *who* an address is (e.g. ``grantRole`` to a known multisig reads very
differently from ``grantRole`` to an unknown EOA).

Keys are lowercase hex. Labels are either chain-agnostic (``_CHAIN_AGNOSTIC``,
e.g. burn addresses that mean the same everywhere) or chain-specific
(``_BY_CHAIN``, keyed by ``(chain_id, address)``).

Populate ``_BY_CHAIN`` per deployment with the multisigs/EOAs you care about —
only add an address you have independently verified, since a wrong label is
worse than none.
"""

# Same meaning on every chain.
_CHAIN_AGNOSTIC: dict[str, str] = {
    "0x0000000000000000000000000000000000000000": "Null address (0x0)",
    "0x000000000000000000000000000000000000dead": "Burn address (0x…dEaD)",
}

# (chain_id, lowercase address) → label. Curate per deployment, e.g.:
#   (1, "0x....."): "Yearn yChad (main multisig)",
#   (1, "0x....."): "Yearn dev multisig (ySafe)",
_BY_CHAIN: dict[tuple[int, str], str] = {}


def lookup(chain_id: int, address: str) -> str:
    """Return a curated label for ``address``, or "" if none is registered.

    Chain-specific entries take precedence over chain-agnostic ones.
    """
    if not address:
        return ""
    addr = address.lower()
    return _BY_CHAIN.get((chain_id, addr)) or _CHAIN_AGNOSTIC.get(addr, "")

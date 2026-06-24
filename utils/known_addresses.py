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

from utils.chains import safe_network_to_chain_id


def _lower(address: str) -> str:
    return address.lower()


def _watched_safe_labels() -> dict[tuple[int, str], str]:
    """Labels from the Safe monitor's checked-in watchlist."""
    from protocols.safe.addresses import ALL_SAFE_ADDRESSES

    labels: dict[tuple[int, str], str] = {}
    for entry in ALL_SAFE_ADDRESSES:
        protocol, network, address, *rest = entry
        chain_id = safe_network_to_chain_id(network)
        if not chain_id:
            continue
        label = str(rest[0]) if rest else f"{protocol} Safe"
        labels[(chain_id, _lower(address))] = label
    return labels


def _yearn_proposer_labels() -> dict[str, str]:
    """Yearn proposer EOAs are the same identity wherever they appear."""
    from protocols.safe.addresses import YEARN_PROPOSER_BOTS

    return {
        _lower(YEARN_PROPOSER_BOTS["chad"]): "Yearn yChad proposer bot",
        _lower(YEARN_PROPOSER_BOTS["strategist"]): "Yearn Strategist proposer bot",
        _lower(YEARN_PROPOSER_BOTS["curation"]): "Yearn Curation proposer bot",
    }


def _safe_utility_labels() -> dict[str, str]:
    """Canonical Safe utilities, mirrored from the Safe multisend helper."""
    from protocols.safe.multisend import _SAFE_UTILS

    return dict(_SAFE_UTILS)


# Same meaning on every chain.
_CHAIN_AGNOSTIC: dict[str, str] = {
    "0x0000000000000000000000000000000000000000": "Null address (0x0)",
    "0x000000000000000000000000000000000000dead": "Burn address (0x…dEaD)",
    **_safe_utility_labels(),
    **_yearn_proposer_labels(),
}

# (chain_id, lowercase address) → label.
_BY_CHAIN: dict[tuple[int, str], str] = {
    **_watched_safe_labels(),
    # Mainnet protocol timelocks supplied by monitoring operators.
    (1, "0xd8236031d8279d82e615af2bfab5fc0127a329ab"): "CAP TimelockController",
    (1, "0x5d8a7dc9405f08f14541ba918c1bf7eb2dace556"): "ETH+ Timelock",
    (1, "0x055e84e7fe8955e2781010b866f10ef6e1e77e59"): "Lombard Timelock",
    (1, "0x9f26d4c958fd811a1f59b01b86be7dffc9d20761"): "EtherFi Timelock",
    (1, "0x49bd9989e31ad35b0a62c20be86335196a3135b1"): "KelpDAO rsETH Timelock",
    (1, "0x3d18480cc32b6ab3b833dcabd80e76cfd41c48a9"): "Infinifi Long Timelock",
    (1, "0x4b174afbed7b98ba01f50e36109eee5e6d327c32"): "Infinifi Short Timelock",
    (1, "0x88ba032be87d5ef1fbe87336b7090767f367bf73"): "Yearn TimelockController",
    (1, "0x1dccd4628d48a50c1a7adea3848bcc869f08f8c2"): "3Jane 24h TimelockController",
    (1, "0x3d3c41419ab401cd25055e8f9421d7d96d887885"): "3Jane 7d TimelockController",
    (1, "0xb2a3cf69c97afd4de7882e5fee120e4efc77b706"): "Strata 48h Timelock",
    (1, "0x4f2682b78f37910704fb1aff29358a1da07e022d"): "Strata 24h Timelock",
    (1, "0x9aee0b04504cef83a65ac3f0e838d0593bcb2bc7"): "Aave Timelock",
    (1, "0x6d903f6003cca6255d85cca4d3b5e5146dc33925"): "Compound Timelock",
    (1, "0x2386dc45added673317ef068992f19421b481f4c"): "Fluid Timelock",
    (1, "0x2e59a20f205bb85a89c53f1936454680651e618e"): "Lido Timelock",
    (1, "0x2efff88747eb5a3ff00d4d8d0f0800e306c0426b"): "Maple GovernorTimelock",
}


def lookup(chain_id: int, address: str) -> str:
    """Return a curated label for ``address``, or "" if none is registered.

    Chain-specific entries take precedence over chain-agnostic ones.
    """
    if not address:
        return ""
    addr = address.lower()
    return _BY_CHAIN.get((chain_id, addr)) or _CHAIN_AGNOSTIC.get(addr, "")

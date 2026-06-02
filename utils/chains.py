from enum import Enum

EXPLORER_URLS: dict[int, str] = {
    1: "https://etherscan.io",
    10: "https://optimistic.etherscan.io",
    137: "https://polygonscan.com",
    8453: "https://basescan.org",
    42161: "https://arbiscan.io",
    747474: "https://katanascan.com",
}


class Chain(Enum):
    MAINNET = (1, "mainnet", "eth")
    OPTIMISM = (10, "optimism", "op")
    BASE = (8453, "base", "base")
    ARBITRUM = (42161, "arbitrum", "arb")
    POLYGON = (137, "polygon", "matic")
    KATANA = (747474, "katana", "kat")

    def __init__(self, chain_id: int, network_name: str, symbol: str):
        self.chain_id = chain_id
        self.network_name = network_name
        self.symbol = symbol

    @property
    def explorer_url(self) -> str | None:
        """Block explorer base URL for this chain (e.g. https://etherscan.io)."""
        return EXPLORER_URLS.get(self.chain_id)

    @classmethod
    def from_chain_id(cls, chain_id: int) -> "Chain":
        for chain in cls:
            if chain.chain_id == chain_id:
                return chain
        raise ValueError(f"Unknown chain_id: {chain_id}")

    @classmethod
    def from_name(cls, name: str) -> "Chain":
        name = name.lower()
        for chain in cls:
            if chain.network_name == name:
                return chain
        raise ValueError(f"Unknown chain name: {name}")


# Safe API uses network names like "arbitrum-main", "base-main", etc.
_SAFE_NETWORK_ALIASES: dict[str, str] = {
    "arbitrum-main": "arbitrum",
    "optimism-main": "optimism",
    "polygon-main": "polygon",
    "base-main": "base",
    "optim-yearn": "optimism",
    "katana-main": "katana",
}


def safe_network_to_chain_id(safe_network: str) -> int:
    """Convert a Safe API network name to a chain ID.

    Args:
        safe_network: Safe network name (e.g. "mainnet", "arbitrum-main").

    Returns:
        Chain ID (e.g. 1, 42161), or 0 if unknown.
    """
    canonical = _SAFE_NETWORK_ALIASES.get(safe_network, safe_network)
    try:
        return Chain.from_name(canonical).chain_id
    except ValueError:
        return 0

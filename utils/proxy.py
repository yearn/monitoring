"""Proxy upgrade detection utilities.

Detects proxy upgrade transactions (EIP-1967) and generates diff links
to compare old vs new implementation source code on Etherscan.
"""

from dataclasses import dataclass

from eth_utils import to_checksum_address

from utils.calldata.decoder import decode_calldata
from utils.chains import EXPLORER_URLS, Chain
from utils.logging import get_logger

logger = get_logger("utils.proxy")

# EIP-1967 implementation storage slot
# bytes32(uint256(keccak256("eip1967.proxy.implementation")) - 1)
EIP1967_IMPL_SLOT = 0x360894A13BA1A3210667C828492DB98DCA3E2076CC3735A920A3CA505D382BBC

# Selectors that indicate a proxy upgrade.
# - upgradeTo(address)                       — called on the proxy itself
# - upgradeToAndCall(address,bytes)          — called on the proxy itself
# - upgradeAndCall(address,address,bytes)    — called on a ProxyAdmin (proxy = arg 0)
_PROXY_DIRECT_SELECTORS = frozenset({"0x3659cfe6", "0x4f1ef286"})
_PROXY_ADMIN_SELECTOR = "0x9623609d"


@dataclass(frozen=True)
class ProxyUpgrade:
    """Result of detecting a proxy upgrade in calldata."""

    proxy_address: str  # the proxy whose impl is being changed (may differ from tx target)
    new_implementation: str


def detect_proxy_upgrade(data_hex: str, target: str = "") -> ProxyUpgrade | None:
    """Check if calldata is a proxy upgrade and return proxy + new impl.

    Supports:
        - upgradeTo(address)                       (called on the proxy itself)
        - upgradeToAndCall(address,bytes)          (called on the proxy itself)
        - upgradeAndCall(address,address,bytes)    (called on ProxyAdmin; proxy is arg 0)

    Args:
        data_hex: calldata hex with 0x prefix
        target: the tx's target address — used as the proxy address for the
            "called on the proxy itself" variants. For the ProxyAdmin variant
            the proxy address comes from the calldata.

    Returns:
        ProxyUpgrade(proxy_address, new_implementation) or None.
    """
    if not data_hex or len(data_hex) < 10:
        return None

    selector = data_hex[:10].lower()
    decoded = decode_calldata(data_hex)
    if not decoded or not decoded.params:
        return None

    if selector in _PROXY_DIRECT_SELECTORS:
        type_str, value = decoded.params[0]
        if type_str != "address" or not target:
            return None
        return ProxyUpgrade(
            proxy_address=to_checksum_address(target),
            new_implementation=to_checksum_address(value),
        )

    if selector == _PROXY_ADMIN_SELECTOR and len(decoded.params) >= 2:
        proxy_type, proxy_addr = decoded.params[0]
        impl_type, impl_addr = decoded.params[1]
        if proxy_type != "address" or impl_type != "address":
            return None
        return ProxyUpgrade(
            proxy_address=to_checksum_address(proxy_addr),
            new_implementation=to_checksum_address(impl_addr),
        )

    return None


def get_current_implementation(proxy_address: str, chain_id: int) -> str | None:
    """Read the current implementation address from the EIP-1967 storage slot.

    Args:
        proxy_address: The proxy contract address.
        chain_id: Chain ID to query.

    Returns:
        Current implementation address (checksummed), or None on failure.
    """
    try:
        chain = Chain.from_chain_id(chain_id)
        from utils.web3_wrapper import ChainManager

        client = ChainManager.get_client(chain)
        from web3 import Web3

        raw = client.eth.get_storage_at(Web3.to_checksum_address(proxy_address), EIP1967_IMPL_SLOT)

        # get_storage_at returns HexBytes (32 bytes), address is last 20 bytes
        hex_str = raw.hex() if isinstance(raw, bytes) else str(raw)
        hex_str = hex_str.replace("0x", "").zfill(64)
        addr = "0x" + hex_str[-40:]

        # Zero address means no implementation set (not a proxy)
        if int(addr, 16) == 0:
            return None

        return to_checksum_address(addr)
    except Exception:
        logger.debug("Failed to read implementation slot for %s on chain %s", proxy_address, chain_id, exc_info=True)
        return None


def build_diff_url(old_impl: str, new_impl: str, chain_id: int) -> str | None:
    """Build an Etherscan contract diff checker URL.

    Args:
        old_impl: Current implementation address.
        new_impl: New implementation address.
        chain_id: Chain ID for the correct block explorer.

    Returns:
        Diff checker URL, or None if no explorer is configured for this chain.
    """
    explorer = EXPLORER_URLS.get(chain_id)
    if not explorer:
        return None
    return f"{explorer}/contractdiffchecker?a1={old_impl}&a2={new_impl}"

"""Local lookup table of common function selectors seen in timelock operations.

Avoids API calls for frequently encountered selectors. The calldata decoder
falls through to the Sourcify 4byte API only for selectors not found here.
"""

# selector -> function signature
KNOWN_SELECTORS: dict[str, str] = {
    # Access control
    "0x2f2ff15d": "grantRole(bytes32,address)",
    "0xd547741f": "revokeRole(bytes32,address)",
    "0x36568abe": "renounceRole(bytes32,address)",
    # TimelockController
    "0x01d5062a": "schedule(address,uint256,bytes,bytes32,bytes32,uint256)",
    "0x8f2a0bb0": "scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)",
    "0xc4d252f5": "cancel(bytes32)",
    "0x134008d3": "execute(address,uint256,bytes,bytes32,bytes32)",
    "0xe38335e5": "executeBatch(address[],uint256[],bytes[],bytes32,bytes32)",
    "0x64d62353": "updateDelay(uint256)",
    # ERC20
    "0xa9059cbb": "transfer(address,uint256)",
    "0x095ea7b3": "approve(address,uint256)",
    "0x23b872dd": "transferFrom(address,address,uint256)",
    # Proxy / upgrades
    "0x3659cfe6": "upgradeTo(address)",
    "0x4f1ef286": "upgradeToAndCall(address,bytes)",
    # Ownable
    "0xf2fde38b": "transferOwnership(address)",
    # Pausable
    "0x8456cb59": "pause()",
    "0x3f4ba83a": "unpause()",
    # Governance admin
    "0xe177246e": "setDelay(uint256)",
    "0x4dd18bf5": "setPendingAdmin(address)",
    "0x0e18b681": "acceptAdmin()",
    # Compound Governor
    "0xddf0b009": "queue(uint256)",
    "0xfe0d94c1": "execute(uint256)",
    "0xbb913f41": "_setImplementation(address)",
    # Compound Comptroller
    "0xa76b3fda": "_supportMarket(address)",
    "0x55ee1fe1": "_setPriceOracle(address)",
    "0xe4028eee": "_setCollateralFactor(address,uint256)",
    "0x317b0b77": "_setCloseFactor(uint256)",
    "0x4fd42e17": "_setLiquidationIncentive(uint256)",
}

"""Static Morpho vault and collateral-monitoring configuration."""

from dataclasses import dataclass
from typing import Iterable

from utils.chains import Chain

KATANA_USDC = "0x203A662b0BD271A6ed5a60EdFbd04bFce608FD36"
KATANA_USDT = "0x2DCa96907fde857dd3D816880A0df407eeB2D2F2"
KATANA_WETH = "0xEE7D8BCFb72bC1880D0Cf19822eB0A2e6577aB62"
KATANA_WBTC = "0x0913DA6Da4b42f538B445599b46Bb4622342Cf52"


@dataclass(frozen=True)
class VaultConfig:
    """One vault monitored by the Morpho market and governance jobs."""

    name: str
    address: str
    risk_level: int
    collateral_asset: str | None = None


VAULTS_V1_BY_CHAIN: dict[Chain, tuple[VaultConfig, ...]] = {
    Chain.MAINNET: (
        VaultConfig("Yearn USDC", "0x68Aea7b82Df6CcdF76235D46445Ed83f85F845A3", 1),
        VaultConfig("Yearn USDT", "0x0963232eB842BAF53E8e517691f81745C1F228a0", 1),
        VaultConfig("Yearn WBTC", "0x2bB005127069A0F0325Fb7370967E8A2b64FB77E", 1),
        VaultConfig("Yearn OG WETH", "0xE89371eAaAC6D46d4C3ED23453241987916224FC", 2),
        VaultConfig("Yearn OG USDC", "0xF9bdDd4A9b3A45f980e11fDDE96e16364dDBEc49", 2),
        VaultConfig("OUSD", "0x5B8b9FA8e4145eE06025F642cAdB1B47e5F39F04", 2),
        VaultConfig("Vault Bridge USDC", "0xBEefb9f61CC44895d8AEc381373555a64191A9c4", 1),
        VaultConfig("Vault Bridge USDT", "0xc54b4E08C1Dcc199fdd35c6b5Ab589ffD3428a8d", 1),
        VaultConfig("Vault Bridge WETH", "0x31A5684983EeE865d943A696AAC155363bA024f9", 1),
        VaultConfig("Vault Bridge WBTC", "0x812B2C6Ab3f4471c0E43D4BB61098a9211017427", 2),
    ),
    Chain.BASE: (
        VaultConfig("Moonwell Flagship USDC", "0xc1256Ae5FF1cf2719D4937adb3bbCCab2E00A2Ca", 1),
        VaultConfig("Yearn OG USDC", "0xef417a2512C5a41f69AE4e021648b69a7CdE5D03", 2),
        VaultConfig("Yearn OG WETH", "0x1D795E29044A62Da42D927c4b179269139A28A6B", 2),
        VaultConfig("OUSD", "0x581Cc9a73Ec7431723A4a80699B8f801205841F1", 2),
    ),
    Chain.KATANA: (
        VaultConfig("Yearn OG WETH", "0xFaDe0C546f44e33C134c4036207B314AC643dc2E", 1, KATANA_WETH),
        VaultConfig("Yearn OG USDC", "0xCE2b8e464Fc7b5E58710C24b7e5EBFB6027f29D7", 1, KATANA_USDC),
        VaultConfig("Yearn OG USDT", "0x8ED68f91AfbE5871dCE31ae007a936ebE8511d47", 1, KATANA_USDT),
        VaultConfig("Yearn OG WBTC", "0xe107cCdeb8e20E499545C813f98Cc90619b29859", 1, KATANA_WBTC),
        VaultConfig("Gauntlet USDC", "0xE4248e2105508FcBad3fe95691551d1AF14015f7", 2, KATANA_USDC),
        VaultConfig(
            "Steakhouse High Yield USDC",
            "0x1445A01a57D7B7663CfD7B4EE0a8Ec03B379aabD",
            3,
            KATANA_USDC,
        ),
        VaultConfig("Gauntlet USDT", "0x1ecDC3F2B5E90bfB55fF45a7476FF98A8957388E", 1, KATANA_USDT),
        VaultConfig(
            "Steakhouse Prime USDC",
            "0x61D4F9D3797BA4dA152238c53a6f93Fb665C3c1d",
            1,
            KATANA_USDC,
        ),
        VaultConfig("Gauntlet WETH", "0xC5e7AB07030305fc925175b25B93b285d40dCdFf", 1, KATANA_WETH),
        VaultConfig("Gauntlet WBTC", "0xf243523996ADbb273F0B237B53f30017C4364bBC", 1, KATANA_WBTC),
    ),
}


VAULTS_V2_BY_CHAIN: dict[Chain, tuple[VaultConfig, ...]] = {
    Chain.MAINNET: (
        VaultConfig("Yearn USDC", "0xaA8d9E2aBa210639cE6C7cE21385e7c673ACa6f3", 1),
        VaultConfig("Yearn OG WETH V2", "0xbe518068EB6135117207256F8C9aFf81B4382DB1", 1),
        VaultConfig("Yearn OG USDC", "0xB885F6d448dA7E2C642Ec31190B629E40E87B069", 2),
        VaultConfig("Sentora RLUSD Main", "0x6dC58a0FdfC8D694e571DC59B9A52EEEa780E6bf", 2),
        VaultConfig("Sentora PaypalUSD Main", "0xb576765fB15505433aF24FEe2c0325895C559FB2", 2),
    ),
    Chain.BASE: (
        VaultConfig("Yearn OG USDC V2", "0xe7D0DBE3493830e2Ab62619211A2BfF0Fc60dB42", 2),
        VaultConfig("Yearn OG WETH V2", "0x2EfD54529329AD364B8Df988CE3BAb5Ff256ab3E", 2),
        VaultConfig("OUSD Vault V2", "0x2Ba14b2e1E7D2189D3550b708DFCA01f899f33c1", 2),
    ),
    Chain.KATANA: (
        VaultConfig("Yearn OG USDC", "0xca44cbe1FB03691d43d2d93AA460e2fCB03878fE", 1, KATANA_USDC),
        VaultConfig("Yearn OG USDT", "0x4284d4F9f4d61eA57B8F0943547c7C19C5B9B249", 1, KATANA_USDT),
        VaultConfig("Yearn OG ETH", "0x5920A6FC553af799542EDA628AdfCc9eA52e141C", 1, KATANA_WETH),
        VaultConfig("Yearn KAT", "0x9b1aE9548E4B46cEB6650f6CEc702bAf5CF2b8CC", 1),
        VaultConfig("Yearn Degen USDC", "0xA2d38c8A3D810EBcF4C2075821c5eC8F976bb692", 3, KATANA_USDC),
        VaultConfig("Gauntlet USDT", "0xaC596AD9771a8d0D4DF108ae0406e6f913aEdceb", 1, KATANA_USDT),
        VaultConfig(
            "Steakhouse High Yield USDC",
            "0xbeeff2d5d126d4809195EeA02b605423917bb6c6",
            2,
            KATANA_USDC,
        ),
        VaultConfig(
            "Steakhouse Prime USDC",
            "0xbeef042bAD4472c3F7Eb9A73070703788b5362D7",
            1,
            KATANA_USDC,
        ),
    ),
}


YV_COLLATERAL_MARKETS_BY_ASSET: dict[Chain, dict[str, tuple[str, ...]]] = {
    Chain.KATANA: {
        KATANA_USDC: ("0x6691cdcadd5d23ac68d2c1cf54dc97ab8242d2a888230de411094480252c2ed3",),
        KATANA_USDT: ("0xcdaf57d98c2f75bffb8f0d3f7aa79bbacda4a479c47e316aab14af1ca6d85ffc",),
        KATANA_WETH: ("0x08f67ef41398456dbc5ff72d43c8b6f7917abfd01498a9fc6c89dabe6eb78b8c",),
        KATANA_WBTC: ("0x3a22063bd258f3f75e3135cac4ec53435dfa5b47b3d5173bb8fd5278e6c1b305",),
    },
}


def iter_vaults(vaults_by_chain: dict[Chain, tuple[VaultConfig, ...]]) -> Iterable[tuple[Chain, VaultConfig]]:
    """Yield chain/config pairs from a generation-specific configuration."""
    for chain, vaults in vaults_by_chain.items():
        for vault in vaults:
            yield chain, vault


def get_vault_query_config(
    vaults_by_chain: dict[Chain, tuple[VaultConfig, ...]],
) -> tuple[dict[str, tuple[Chain, VaultConfig]], list[str], list[int]]:
    """Flatten vault configuration for GraphQL queries and response joins."""
    entries = list(iter_vaults(vaults_by_chain))
    metadata = {vault.address.lower(): (chain, vault) for chain, vault in entries}
    addresses = [vault.address for _, vault in entries]
    chain_ids = sorted({chain.chain_id for chain, _ in entries})
    return metadata, addresses, chain_ids


def get_vault_config(vault_address: str, chain: Chain, *, version: int) -> VaultConfig:
    """Return a configured vault or fail for an unknown address/version."""
    vaults_by_chain = _get_vaults_by_version(version)
    for vault in vaults_by_chain.get(chain, ()):
        if vault.address.lower() == vault_address.lower():
            return vault
    raise ValueError(f"Vault V{version} {vault_address} not found in Morpho configuration")


def get_collateral_vaults_by_asset(
    chain: Chain,
    *,
    version: int,
) -> dict[str, list[VaultConfig]]:
    """Group collateral-strategy vaults by their underlying asset address."""
    vaults_by_chain = _get_vaults_by_version(version)
    grouped: dict[str, list[VaultConfig]] = {}
    for vault in vaults_by_chain.get(chain, ()):
        if vault.collateral_asset is None:
            continue
        grouped.setdefault(vault.collateral_asset.lower(), []).append(vault)
    return grouped


def is_collateral_vault(vault_address: str, chain: Chain, *, version: int) -> bool:
    """Return whether a configured vault supplies YV-collateral unwind liquidity."""
    try:
        return get_vault_config(vault_address, chain, version=version).collateral_asset is not None
    except ValueError:
        return False


def _get_vaults_by_version(version: int) -> dict[Chain, tuple[VaultConfig, ...]]:
    if version == 1:
        return VAULTS_V1_BY_CHAIN
    if version == 2:
        return VAULTS_V2_BY_CHAIN
    raise ValueError(f"Unsupported Morpho vault version: {version}")

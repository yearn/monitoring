"""Morpho VaultV2 markets / allocation / risk monitor.

Iterates the explicit ``VAULTS_V2_BY_CHAIN`` list (Yearn-curated V2 vaults), then
for each vault reads its adapters on-chain and:

* For ``MorphoVaultV1Adapter`` (V2 wraps a v1 MetaMorpho vault) — sanity-checks
  that the wrapped v1 vault is already monitored. The wrapped v1 vault keeps
  receiving its full v1 analysis via ``markets.py``; we only flag the case where
  V2 introduces a *new* unknown v1 vault that operators should add.
* For ``MorphoMarketV1AdapterV2`` (V2 wraps Morpho Blue markets directly) —
  reads ``expectedSupplyAssets`` per market, fetches market metadata via
  GraphQL, and runs the existing v1 risk-tier scoring (``MARKETS_RISK_*`` +
  ``ALLOCATION_TIERS`` + ``MAX_RISK_THRESHOLDS``).

Bad debt is pulled per market from the same GraphQL endpoint v1 uses.
Liquidity monitoring is deferred to phase 2 (see TODO at bottom).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests
from web3 import Web3

from morpho._shared import (
    API_URL,
    SUPPORTED_CHAINS,
    VAULTS_V2_BY_CHAIN,
    MarketMetrics,
    fetch_market_metrics,
    get_market_url,
    get_vault_url,
)
from morpho.markets import (
    BAD_DEBT_RATIO,
    MARKETS_RISK_1,
    MARKETS_RISK_2,
    MARKETS_RISK_3,
    MARKETS_RISK_4,
    MAX_RISK_THRESHOLDS,
    VAULTS_BY_CHAIN,
    get_market_allocation_threshold,
)
from utils.abi import load_abi
from utils.chains import Chain
from utils.http import request_with_retry
from utils.logging import get_logger
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager, Web3Client

PROTOCOL = "morpho"
logger = get_logger("morpho.markets_v2")

ABI_VAULT_V2 = load_abi("morpho/abi/vault_v2.json")
ABI_MARKET_ADAPTER = load_abi("morpho/abi/morpho_market_v1_adapter_v2.json")
ABI_VAULT_ADAPTER = load_abi("morpho/abi/morpho_vault_v1_adapter.json")

ADAPTER_KIND_MARKET = "MorphoMarketV1AdapterV2"
ADAPTER_KIND_VAULT = "MorphoVaultV1Adapter"
ADAPTER_KIND_UNKNOWN = "Unknown"


@dataclass
class V2Vault:
    """Yearn-curated V2 vault declared in ``VAULTS_V2_BY_CHAIN``."""

    name: str
    address: str
    chain: Chain
    asset_address: str
    asset_symbol: str
    curator: str
    owner: str
    risk_level: int
    total_assets_usd: float = 0.0
    graphql_adapters: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AdapterInfo:
    """Result of on-chain adapter classification."""

    address: str
    kind: str
    wrapped_v1_vault: Optional[str] = None  # set for MorphoVaultV1Adapter
    market_ids: List[str] = field(default_factory=list)  # set for MorphoMarketV1AdapterV2
    expected_supply_assets: List[int] = field(default_factory=list)  # parallel to market_ids


# ----------------------------------------------------------------------------
# Vault state fetch (static list → GraphQL)
# ----------------------------------------------------------------------------


_STATE_QUERY = """
query VaultV2State($addresses: [String!]!, $chainIds: [Int!]!) {
  vaultV2s(first: 200, where: { address_in: $addresses, chainId_in: $chainIds }) {
    items {
      address
      name
      chain { id }
      curator { address }
      owner { address }
      asset { address symbol }
      totalAssetsUsd
      adapters {
        items { address type assetsUsd }
      }
    }
  }
}
"""


def discover_v2_vaults_by_chain() -> Dict[Chain, List[V2Vault]]:
    """Load state for every vault declared in ``VAULTS_V2_BY_CHAIN``.

    Issues a single GraphQL ``vaultV2s(where: { address_in })`` query, then
    joins the result back to the static list to inherit the configured risk
    level. Vaults missing from the GraphQL response are logged and skipped.
    """
    addr_to_meta: dict[str, tuple[Chain, str, int]] = {}
    addresses: list[str] = []
    chain_ids: list[int] = []
    for chain, vaults in VAULTS_V2_BY_CHAIN.items():
        chain_ids.append(chain.chain_id)
        for entry in vaults:
            name, address, risk = str(entry[0]), Web3.to_checksum_address(str(entry[1])), int(str(entry[2]))
            addr_to_meta[address.lower()] = (chain, name, risk)
            addresses.append(address)

    if not addresses:
        return {}

    try:
        response = request_with_retry(
            "post",
            API_URL,
            json={
                "query": _STATE_QUERY,
                "variables": {"addresses": addresses, "chainIds": sorted(set(chain_ids))},
            },
        )
    except requests.RequestException as e:
        logger.warning("Failed to fetch v2 vault state: %s", e)
        return {chain: [] for chain in SUPPORTED_CHAINS}

    data = response.json()
    if "errors" in data:
        logger.warning("GraphQL errors fetching v2 state: %s", data["errors"])
        return {chain: [] for chain in SUPPORTED_CHAINS}

    items = data.get("data", {}).get("vaultV2s", {}).get("items") or []
    by_addr: dict[str, dict[str, Any]] = {item["address"].lower(): item for item in items}

    result: Dict[Chain, List[V2Vault]] = {chain: [] for chain in SUPPORTED_CHAINS}
    for addr_lc, (chain, name, risk_level) in addr_to_meta.items():
        item = by_addr.get(addr_lc)
        if item is None:
            logger.warning("V2 vault %s on %s missing from GraphQL response", addr_lc, chain.name)
            continue
        result.setdefault(chain, []).append(
            V2Vault(
                name=name,
                address=Web3.to_checksum_address(item["address"]),
                chain=chain,
                asset_address=item["asset"]["address"],
                asset_symbol=item["asset"]["symbol"],
                curator=(item.get("curator") or {}).get("address") or "",
                owner=(item.get("owner") or {}).get("address") or "",
                risk_level=risk_level,
                total_assets_usd=float(item.get("totalAssetsUsd") or 0),
                graphql_adapters=(item.get("adapters") or {}).get("items") or [],
            )
        )

    for chain, chain_vaults in result.items():
        logger.info("Loaded %d V2 vault(s) on %s", len(chain_vaults), chain.name)
    return result


# ----------------------------------------------------------------------------
# Adapter classification & on-chain reads
# ----------------------------------------------------------------------------


def list_adapters(client: Web3Client, vault_address: str) -> List[str]:
    """Return the list of adapter addresses currently registered on a V2 vault."""
    vault = client.get_contract(vault_address, ABI_VAULT_V2)
    length = vault.functions.adaptersLength().call()
    if length == 0:
        return []
    with client.batch_requests() as batch:
        for i in range(length):
            batch.add(vault.functions.adapters(i))
        responses = client.execute_batch(batch)
    return [Web3.to_checksum_address(addr) for addr in responses]


def classify_adapter(client: Web3Client, adapter_address: str) -> AdapterInfo:
    """Detect whether an adapter is a market-v1 or vault-v1 adapter and pull view data."""
    market_adapter = client.get_contract(adapter_address, ABI_MARKET_ADAPTER)
    try:
        market_ids_length = market_adapter.functions.marketIdsLength().call()
    except Exception:
        market_ids_length = None

    if market_ids_length is not None:
        market_ids: List[str] = []
        if market_ids_length > 0:
            with client.batch_requests() as batch:
                for i in range(market_ids_length):
                    batch.add(market_adapter.functions.marketIds(i))
                market_ids = ["0x" + bytes(mid).hex() for mid in client.execute_batch(batch)]

        expected_assets: List[int] = []
        if market_ids:
            with client.batch_requests() as batch:
                for mid in market_ids:
                    batch.add(market_adapter.functions.expectedSupplyAssets(bytes.fromhex(mid[2:])))
                expected_assets = list(client.execute_batch(batch))

        return AdapterInfo(
            address=adapter_address,
            kind=ADAPTER_KIND_MARKET,
            market_ids=market_ids,
            expected_supply_assets=expected_assets,
        )

    vault_adapter = client.get_contract(adapter_address, ABI_VAULT_ADAPTER)
    try:
        wrapped = vault_adapter.functions.morphoVaultV1().call()
        return AdapterInfo(
            address=adapter_address,
            kind=ADAPTER_KIND_VAULT,
            wrapped_v1_vault=Web3.to_checksum_address(wrapped),
        )
    except Exception as e:
        logger.warning("Adapter %s could not be classified: %s", adapter_address, e)
        return AdapterInfo(address=adapter_address, kind=ADAPTER_KIND_UNKNOWN)


# ----------------------------------------------------------------------------
# Risk / allocation analysis
# ----------------------------------------------------------------------------


def _market_risk_level(market_id: str, chain: Chain) -> int:
    """Look up the Morpho Blue market's risk tier from v1 tables (1-5)."""
    mid = market_id.lower()
    for tier, table in (
        (1, MARKETS_RISK_1),
        (2, MARKETS_RISK_2),
        (3, MARKETS_RISK_3),
        (4, MARKETS_RISK_4),
    ):
        if mid in (m.lower() for m in table.get(chain, [])):
            return tier
    return 5


def score_market_allocations(
    vault: V2Vault,
    market_adapters: List[AdapterInfo],
    vault_total_assets_usd: float,
) -> None:
    """Aggregate per-market allocations across **all** market-v1 adapters and alert.

    The vault-level risk score must aggregate every adapter's exposure before
    comparing to ``MAX_RISK_THRESHOLDS`` — otherwise a curator can split a
    risky position across two adapters and dodge the threshold per adapter
    while the vault as a whole exceeds it.
    """
    if vault_total_assets_usd <= 0 or not market_adapters:
        return

    # Sum allocations per market_id across adapters, in case the same market
    # is wired through more than one adapter.
    underlying_per_market: dict[str, int] = {}
    for adapter in market_adapters:
        for market_id, expected_assets in zip(adapter.market_ids, adapter.expected_supply_assets):
            mid = market_id.lower()
            underlying_per_market[mid] = underlying_per_market.get(mid, 0) + int(expected_assets)

    if not underlying_per_market:
        return

    metrics = fetch_market_metrics(list(underlying_per_market.keys()), vault.chain)

    total_risk_score = 0.0
    allocation_violations: list[str] = []
    bad_debt_alerts: list[str] = []

    for market_id, expected_assets in underlying_per_market.items():
        market = metrics.get(market_id)
        if not market:
            logger.info("No GraphQL data for market %s; skipping", market_id)
            continue

        allocation_usd = _allocation_to_usd(market, expected_assets)
        if allocation_usd is None:
            logger.info("Cannot derive USD allocation for market %s; skipping", market_id)
            continue
        allocation_ratio = min(allocation_usd / vault_total_assets_usd, 1.0)
        risk_level = _market_risk_level(market_id, vault.chain)
        threshold = get_market_allocation_threshold(risk_level, vault.risk_level)
        total_risk_score += risk_level * allocation_ratio

        market_label = _market_label(market, market_id, vault.chain)
        if allocation_ratio > threshold:
            allocation_violations.append(
                f"- {market_label} (risk {risk_level}): {allocation_ratio:.1%} (max: {threshold:.1%})"
            )

        # Bad debt — same threshold as v1.
        bad_debt_usd = market.bad_debt.usd
        borrow_usd = market.state.borrow_assets_usd
        if borrow_usd > 0 and bad_debt_usd / borrow_usd > BAD_DEBT_RATIO:
            bad_debt_alerts.append(
                f"- {market_label}: ${bad_debt_usd:,.2f} ({bad_debt_usd / borrow_usd:.2%} of borrowed)"
            )

    vault_url = get_vault_url(vault.address, vault.chain)
    if allocation_violations:
        send_telegram_message(
            f"🔺 V2 high allocation in [{vault.name}]({vault_url}) (risk {vault.risk_level}) "
            f"on {vault.chain.name}\n" + "\n".join(allocation_violations),
            PROTOCOL,
        )

    if bad_debt_alerts:
        send_telegram_message(
            f"🚨 V2 bad debt in [{vault.name}]({vault_url}) on {vault.chain.name}\n" + "\n".join(bad_debt_alerts),
            PROTOCOL,
        )

    total_risk_score = round(total_risk_score, 2)
    logger.info("V2 vault %s on %s — total risk score %.2f", vault.name, vault.chain.name, total_risk_score)
    max_risk = MAX_RISK_THRESHOLDS[vault.risk_level]
    if total_risk_score > max_risk:
        send_telegram_message(
            f"⚠️ V2 high risk in [{vault.name}]({vault_url}) (risk {vault.risk_level}) "
            f"on {vault.chain.name}\n"
            f"🔢 Risk level: {total_risk_score:.2f} (max: {max_risk:.2f})\n"
            f"🔢 Total assets: ${vault_total_assets_usd:,.2f}",
            PROTOCOL,
        )


def _allocation_to_usd(market: MarketMetrics, expected_assets: int) -> Optional[float]:
    """Convert ``expectedSupplyAssets`` (underlying units) to USD via market state ratio.

    Uses ``supplyAssetsUsd / supplyAssets`` when both are non-zero, falling back to
    the borrow side ratio. Returns None when neither side carries enough state to
    derive a price (typically a freshly created or empty market).
    """
    state = market.state
    if state.supply_assets > 0 and state.supply_assets_usd > 0:
        return expected_assets * state.supply_assets_usd / state.supply_assets
    if state.borrow_assets > 0 and state.borrow_assets_usd > 0:
        return expected_assets * state.borrow_assets_usd / state.borrow_assets
    return None


def _market_label(market: MarketMetrics, market_id: str, chain: Chain) -> str:
    """Render a clickable Markdown label for a Morpho Blue market."""
    loan = market.loan_asset.symbol or "?"
    coll = market.collateral_asset.symbol if market.collateral_asset else "idle"
    return f"[{coll}/{loan}]({get_market_url(market_id, chain)})"


def analyze_vault_adapter(vault: V2Vault, adapter: AdapterInfo) -> None:
    """Sanity-check that a wrapped v1 vault is already monitored by markets.py."""
    if adapter.wrapped_v1_vault is None:
        return
    monitored = {str(entry[1]).lower() for entry in VAULTS_BY_CHAIN.get(vault.chain, [])}
    if adapter.wrapped_v1_vault.lower() not in monitored:
        vault_url = get_vault_url(vault.address, vault.chain)
        send_telegram_message(
            f"ℹ️ V2 [{vault.name}]({vault_url}) on {vault.chain.name} wraps unmonitored v1 vault "
            f"`{adapter.wrapped_v1_vault}` — consider adding it to "
            f"morpho/markets.py:VAULTS_BY_CHAIN.",
            PROTOCOL,
        )


def analyze_v2_vault(client: Web3Client, vault: V2Vault) -> None:
    """Run all v2 monitoring checks for a single vault."""
    adapter_addresses = list_adapters(client, vault.address)
    adapters = [classify_adapter(client, addr) for addr in adapter_addresses]

    market_adapters: List[AdapterInfo] = []
    for adapter in adapters:
        if adapter.kind == ADAPTER_KIND_MARKET:
            market_adapters.append(adapter)
        elif adapter.kind == ADAPTER_KIND_VAULT:
            analyze_vault_adapter(vault, adapter)
        else:
            logger.warning("Skipping unknown adapter kind for %s", adapter.address)

    if market_adapters:
        score_market_allocations(vault, market_adapters, vault.total_assets_usd)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def main() -> None:
    """Discover Yearn-relevant V2 vaults and run allocation/risk monitoring on each."""
    logger.info("Checking Morpho V2 vaults...")
    vaults_by_chain = discover_v2_vaults_by_chain()
    if not any(vaults_by_chain.values()):
        logger.info("No matching V2 vaults found yet.")
        return

    for chain, vaults in vaults_by_chain.items():
        if not vaults:
            continue
        client = ChainManager.get_client(chain)
        for vault in vaults:
            try:
                analyze_v2_vault(client, vault)
            except Exception as e:
                logger.exception("Failed to analyze V2 vault %s on %s: %s", vault.address, chain.name, e)


# TODO: phase 2 — implement liquidity monitoring once we have real V2 vaults to
# observe. Aggregating per-adapter `realAssets()` against a chosen liquid floor
# is non-trivial because borrowed Morpho Blue markets require per-market
# headroom rather than vault-level idle assets.

if __name__ == "__main__":
    main()

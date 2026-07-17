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

Bad debt is pulled per market from the same GraphQL endpoint v1 uses. Normal
Vault V2 liquidity uses the API's immediately withdrawable ``liquidityUsd``;
YV-collateral strategy vaults use the combined v1/v2 coverage check in
``markets.py`` instead.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests
from web3 import Web3

from protocols.morpho._shared import (
    API_URL,
    SUPPORTED_CHAINS,
    MarketMetrics,
    MorphoV2MonitoringError,
    fetch_market_metrics,
    get_market_url,
    get_v2_vault_config,
    get_vault_url,
)
from protocols.morpho.markets import (
    BAD_DEBT_RATIO,
    LIQUIDITY_THRESHOLD,
    MAX_RISK_THRESHOLDS,
    VAULTS_BY_CHAIN,
    get_market_allocation_threshold,
    get_market_risk_level,
    is_yv_collateral_v2_vault,
)
from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import (
    get_last_value_for_key_from_file,
    morpho_filename,
    morpho_key,
    write_last_value_to_file,
)
from utils.chains import Chain
from utils.http_client import request_with_retry
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager, Web3Client

PROTOCOL = "morpho"
logger = get_logger("morpho.markets_v2")

ABI_VAULT_V2 = load_abi("protocols/morpho/abi/vault_v2.json")
ABI_MARKET_ADAPTER = load_abi("protocols/morpho/abi/morpho_market_v1_adapter_v2.json")
ABI_VAULT_ADAPTER = load_abi("protocols/morpho/abi/morpho_vault_v1_adapter.json")

ADAPTER_KIND_MARKET = "MorphoMarketV1AdapterV2"
ADAPTER_KIND_VAULT = "MorphoVaultV1Adapter"

# Cache tag for "this wrapped v1 vault has already been flagged as unmonitored" —
# without this, every hourly run would re-spam the channel.
VAULT_ADAPTER_SEEN_TYPE = "v2_vault_adapter_seen"

# Ignore wrapped-v1 adapter sanity checks when exposure is negligible.
MIN_VAULT_ADAPTER_ALLOCATION_RATIO = 0.01


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
    liquidity_usd: float = 0.0
    graphql_adapters: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AdapterInfo:
    """Result of on-chain adapter classification."""

    address: str
    kind: str
    wrapped_v1_vault: Optional[str] = None  # set for MorphoVaultV1Adapter
    market_ids: List[str] = field(default_factory=list)  # set for MorphoMarketV1AdapterV2
    expected_supply_assets: List[int] = field(default_factory=list)  # parallel to market_ids


@dataclass(frozen=True)
class MarketAssessment:
    """Risk contribution and optional alert lines for one Morpho market."""

    risk_score: float
    allocation_violation: Optional[str] = None
    bad_debt_alert: Optional[str] = None


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
      liquidityUsd
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
    level. Raises if the API omits any configured vault.
    """
    addr_to_meta, addresses, chain_ids = get_v2_vault_config()

    if not addresses:
        return {}

    try:
        response = request_with_retry(
            "post",
            API_URL,
            json={
                "query": _STATE_QUERY,
                "variables": {"addresses": addresses, "chainIds": chain_ids},
            },
        )
    except requests.RequestException as e:
        raise MorphoV2MonitoringError(f"Failed to fetch Morpho Vault V2 state: {e}") from e

    data = response.json()
    if "errors" in data:
        raise MorphoV2MonitoringError(f"Morpho GraphQL errors fetching Vault V2 state: {data['errors']}")

    items = data.get("data", {}).get("vaultV2s", {}).get("items") or []
    by_addr: dict[str, dict[str, Any]] = {item["address"].lower(): item for item in items}
    missing_addresses = sorted(set(addr_to_meta) - set(by_addr))
    if missing_addresses:
        raise MorphoV2MonitoringError(
            "Morpho API omitted configured Vault V2 addresses: " + ", ".join(missing_addresses)
        )

    result: Dict[Chain, List[V2Vault]] = {chain: [] for chain in SUPPORTED_CHAINS}
    for addr_lc, (chain, name, risk_level) in addr_to_meta.items():
        item = by_addr[addr_lc]
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
                liquidity_usd=float(item.get("liquidityUsd") or 0),
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
    try:
        length = vault.functions.adaptersLength().call()
    except Exception as e:
        raise MorphoV2MonitoringError(f"Failed to read adaptersLength() for Vault V2 {vault_address}: {e}") from e
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
        raise MorphoV2MonitoringError(f"Adapter {adapter_address} could not be classified: {e}") from e


# ----------------------------------------------------------------------------
# Risk / allocation analysis
# ----------------------------------------------------------------------------


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

    underlying_per_market = _aggregate_expected_assets(market_adapters)
    if not underlying_per_market:
        return

    metrics = fetch_market_metrics(list(underlying_per_market.keys()), vault.chain)
    total_risk_score = 0.0
    allocation_violations: list[str] = []
    bad_debt_alerts: list[str] = []

    for market_id, expected_assets in underlying_per_market.items():
        assessment = _assess_market(vault, market_id, expected_assets, metrics, vault_total_assets_usd)
        if assessment is None:
            continue
        total_risk_score += assessment.risk_score
        if assessment.allocation_violation is not None:
            allocation_violations.append(assessment.allocation_violation)
        if assessment.bad_debt_alert is not None:
            bad_debt_alerts.append(assessment.bad_debt_alert)

    _send_market_assessment_alerts(vault, allocation_violations, bad_debt_alerts)
    total_risk_score = round(total_risk_score, 2)
    logger.info("V2 vault %s on %s — total risk score %.2f", vault.name, vault.chain.name, total_risk_score)
    _alert_vault_risk(vault, vault_total_assets_usd, total_risk_score)


def _aggregate_expected_assets(market_adapters: List[AdapterInfo]) -> dict[str, int]:
    """Sum expected assets by market across every adapter."""
    underlying_per_market: dict[str, int] = {}
    for adapter in market_adapters:
        for market_id, expected_assets in zip(adapter.market_ids, adapter.expected_supply_assets, strict=True):
            market_id = market_id.lower()
            underlying_per_market[market_id] = underlying_per_market.get(market_id, 0) + int(expected_assets)
    return underlying_per_market


def _assess_market(
    vault: V2Vault,
    market_id: str,
    expected_assets: int,
    metrics: Dict[str, MarketMetrics],
    vault_total_assets_usd: float,
) -> Optional[MarketAssessment]:
    """Calculate risk and alert details for one market exposure."""
    market = metrics.get(market_id)
    if market is None:
        logger.info("No GraphQL data for market %s; skipping", market_id)
        return None

    allocation_usd = _allocation_to_usd(market, expected_assets)
    if allocation_usd is None:
        logger.info("Cannot derive USD allocation for market %s; skipping", market_id)
        return None

    allocation_ratio = min(allocation_usd / vault_total_assets_usd, 1.0)
    risk_level = get_market_risk_level(market_id, vault.chain)
    threshold = get_market_allocation_threshold(risk_level, vault.risk_level)
    market_label = _market_label(market, market_id, vault.chain)
    allocation_violation = None
    if allocation_ratio > threshold:
        allocation_violation = f"- {market_label} (risk {risk_level}): {allocation_ratio:.1%} (max: {threshold:.1%})"

    bad_debt_usd = market.bad_debt.usd
    borrow_usd = market.state.borrow_assets_usd
    bad_debt_alert = None
    if borrow_usd > 0 and bad_debt_usd / borrow_usd > BAD_DEBT_RATIO:
        bad_debt_alert = f"- {market_label}: ${bad_debt_usd:,.2f} ({bad_debt_usd / borrow_usd:.2%} of borrowed)"

    return MarketAssessment(
        risk_score=risk_level * allocation_ratio,
        allocation_violation=allocation_violation,
        bad_debt_alert=bad_debt_alert,
    )


def _send_market_assessment_alerts(
    vault: V2Vault,
    allocation_violations: List[str],
    bad_debt_alerts: List[str],
) -> None:
    """Send consolidated allocation and bad-debt alerts for one vault."""
    vault_url = get_vault_url(vault.address, vault.chain)
    if allocation_violations:
        message = (
            f"🔺 V2 high allocation in [{vault.name}]({vault_url}) (risk {vault.risk_level}) "
            f"on {vault.chain.name}\n" + "\n".join(allocation_violations)
        )
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))

    if bad_debt_alerts:
        message = f"🚨 V2 bad debt in [{vault.name}]({vault_url}) on {vault.chain.name}\n" + "\n".join(bad_debt_alerts)
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))


def _alert_vault_risk(vault: V2Vault, vault_total_assets_usd: float, total_risk_score: float) -> None:
    """Alert when a Vault V2 weighted risk score exceeds its tier limit."""
    max_risk = MAX_RISK_THRESHOLDS[vault.risk_level]
    if total_risk_score <= max_risk:
        return

    vault_url = get_vault_url(vault.address, vault.chain)
    message = (
        f"⚠️ V2 high risk in [{vault.name}]({vault_url}) (risk {vault.risk_level}) on {vault.chain.name}\n"
        f"🔢 Risk level: {total_risk_score:.2f} (max: {max_risk:.2f})\n"
        f"🔢 Total assets: ${vault_total_assets_usd:,.2f}"
    )
    send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))


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


def _adapter_allocation_ratio(vault: V2Vault, adapter_address: str) -> Optional[float]:
    """Return an adapter's share of vault TVL from GraphQL ``assetsUsd`` data."""
    if vault.total_assets_usd <= 0:
        return None
    addr_lc = adapter_address.lower()
    for item in vault.graphql_adapters:
        if (item.get("address") or "").lower() == addr_lc:
            assets_usd = float(item.get("assetsUsd") or 0)
            return min(assets_usd / vault.total_assets_usd, 1.0)
    return None


def analyze_vault_adapter(vault: V2Vault, adapter: AdapterInfo) -> None:
    """Sanity-check that a wrapped v1 vault is already monitored by markets.py."""
    if adapter.wrapped_v1_vault is None:
        return

    allocation_ratio = _adapter_allocation_ratio(vault, adapter.address)
    if allocation_ratio is not None and allocation_ratio < MIN_VAULT_ADAPTER_ALLOCATION_RATIO:
        return
    monitored = {str(entry[1]).lower() for entry in VAULTS_BY_CHAIN.get(vault.chain, [])}
    wrapped_lc = adapter.wrapped_v1_vault.lower()
    if wrapped_lc in monitored:
        return

    # Dedup across hourly runs — alert once per (parent vault, wrapped v1 vault).
    cache_key = morpho_key(vault.address.lower(), wrapped_lc, VAULT_ADAPTER_SEEN_TYPE)
    if str(get_last_value_for_key_from_file(morpho_filename, cache_key)) == "1":
        return

    vault_url = get_vault_url(vault.address, vault.chain)
    send_alert(
        Alert(
            AlertSeverity.LOW,
            f"ℹ️ V2 [{vault.name}]({vault_url}) on {vault.chain.name} wraps unmonitored v1 vault "
            f"`{adapter.wrapped_v1_vault}` — consider adding it to "
            f"morpho/markets.py:VAULTS_BY_CHAIN.",
            PROTOCOL,
        )
    )
    write_last_value_to_file(morpho_filename, cache_key, 1)


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
            raise MorphoV2MonitoringError(f"Unsupported adapter kind {adapter.kind} for {adapter.address}")

    if market_adapters:
        score_market_allocations(vault, market_adapters, vault.total_assets_usd)

    if not is_yv_collateral_v2_vault(vault.address, vault.chain):
        check_low_liquidity(vault)


def check_low_liquidity(vault: V2Vault) -> None:
    """Alert when a non-collateral Vault V2 has less than 1% withdrawable liquidity."""
    if vault.total_assets_usd < 10_000:
        return

    liquidity_ratio = vault.liquidity_usd / vault.total_assets_usd
    if liquidity_ratio >= LIQUIDITY_THRESHOLD:
        return

    vault_url = get_vault_url(vault.address, vault.chain)
    message = (
        f"⚠️ Low liquidity in V2 [{vault.name}]({vault_url}) on {vault.chain.name}\n"
        f"💰 Liquidity: ${vault.liquidity_usd:,.2f} "
        f"({liquidity_ratio:.1%} of ${vault.total_assets_usd:,.2f})\n"
        f"📊 Min threshold: {LIQUIDITY_THRESHOLD:.1%}\n"
    )
    send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))


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

    failures: List[str] = []
    for chain, vaults in vaults_by_chain.items():
        if not vaults:
            continue
        client = ChainManager.get_client(chain)
        for vault in vaults:
            try:
                analyze_v2_vault(client, vault)
            except Exception as e:
                logger.exception("Failed to analyze V2 vault %s on %s", vault.address, chain.name)
                failures.append(f"{vault.name} on {chain.name}: {type(e).__name__}: {e}")

    if failures:
        raise MorphoV2MonitoringError("Failed Morpho Vault V2 analyses: " + "; ".join(failures))


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)

"""
Morpho markets monitoring script.

This module checks Morpho markets for:
1. Bad debt
2. High allocation levels
3. Low liquidity
"""

from typing import Any, Dict, List

from protocols.morpho._shared import (
    PROTOCOL,
    MorphoMonitoringError,
    execute_graphql,
    format_low_liquidity_message,
    get_market_url,
    get_vault_url,
    require_configured_keys,
)
from protocols.morpho.config import (
    VAULTS_V1_BY_CHAIN,
    VAULTS_V2_BY_CHAIN,
    YV_COLLATERAL_MARKETS_BY_ASSET,
    get_collateral_vaults_by_asset,
    get_vault_config,
    is_collateral_vault,
    iter_vaults,
)
from protocols.morpho.risk import (
    LIQUIDITY_THRESHOLD,
    MAX_RISK_THRESHOLDS,
    MIN_VAULT_ASSETS_USD,
    assess_exposure,
    get_market_risk_level,
    is_bad_debt_excessive,
    is_low_liquidity,
)
from utils.alert import Alert, AlertSeverity, send_alert
from utils.chains import Chain
from utils.logger import get_logger

# Configuration constants
logger = get_logger(PROTOCOL)
YV_COLLATERAL_LIQUIDATION_BUFFER = 1.25  # require 25% more withdrawable liquidity than collateral at risk
YV_COLLATERAL_MIN_BORROW_USD = 10_000  # skip dust markets
YV_COLLATERAL_MIN_AT_RISK_USD = 10_000  # skip markets with negligible collateral at risk
YV_COLLATERAL_AT_RISK_POINTS = 50  # 2% increments for the stable-market shock
YV_COLLATERAL_STABLE_PRICE_SHOCK = 0.02
YV_COLLATERAL_VOLATILE_PRICE_SHOCK = 0.15
YV_COLLATERAL_FALLBACK_PRICE_SHOCK = 0.10


def bad_debt_alert(
    markets: List[Dict[str, Any]],
    vault_name: str,
    vault_url: str,
    chain: Chain,
    alerted_markets: set[str],
) -> None:
    """
    Send telegram message if bad debt is detected in any market.

    Args:
        markets: List of market data
        vault_name: Name of the vault (for alert message)
        vault_url: URL of the vault
        chain: Chain the vault is on
        alerted_markets: Set of market IDs already alerted (prevents duplicates across vaults)
    """
    for market in markets:
        market_id = market["marketId"]
        if market_id in alerted_markets:
            continue

        bad_debt = market["badDebt"]["usd"]
        borrowed_tvl = market["state"]["borrowAssetsUsd"]

        # Skip markets with no borrows
        if borrowed_tvl == 0:
            continue

        # Alert if bad debt ratio exceeds threshold
        if is_bad_debt_excessive(bad_debt, borrowed_tvl):
            alerted_markets.add(market_id)
            market_url = get_market_url(market_id, chain)
            market_name = f"{market['collateralAsset']['symbol']}/{market['loanAsset']['symbol']}"

            message = (
                f"🚨 Bad debt detected in [{vault_name}]({vault_url}) on {chain.name}\n"
                f"💹 Market: [{market_name}]({market_url})\n"
                f"💸 Bad debt: ${bad_debt:,.2f} ({(bad_debt / borrowed_tvl):.2%} of borrowed)\n"
            )

            send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))


def check_allocation_and_risk(vault_data: Dict[str, Any]) -> None:
    """
    Check per-market allocation and total vault risk level.
    Sends a consolidated alert if any markets exceed allocation thresholds.
    Sends a separate alert if total risk level exceeds the vault's maximum.
    """
    total_assets = vault_data.get("state", {}).get("totalAssetsUsd", 0) or 0
    if total_assets < MIN_VAULT_ASSETS_USD:
        return

    vault_name = vault_data["name"]
    chain = Chain.from_chain_id(vault_data["chain"]["id"])
    vault_address = vault_data["address"]
    vault_url = get_vault_url(vault_address, chain)
    risk_level = get_vault_config(vault_address, chain, version=1).risk_level

    total_risk_level = 0.0
    allocation_violations: list[str] = []

    for allocation in vault_data["state"]["allocation"]:
        # market without collateral asset is idle asset; supplyCap == 0 means the
        # curator has not enabled this market for active supply.
        if int(allocation.get("supplyCap", 0)) == 0 or allocation["market"]["collateralAsset"] is None:
            continue

        market = allocation["market"]
        market_id = market["marketId"]
        market_supply = allocation.get("supplyAssetsUsd", 0) or 0
        if market_supply == 0:
            logger.info("Skipping market %s has 0 supply assets", market_id)
            continue
        market_risk_level = get_market_risk_level(market_id, chain)
        assessment = assess_exposure(market_supply / total_assets, market_risk_level, risk_level)

        if assessment.allocation_exceeded:
            market_url = get_market_url(market_id, chain)
            market_name = f"{market['collateralAsset']['symbol']}/{market['loanAsset']['symbol']}"
            allocation_violations.append(
                f"- [{market_name}]({market_url}) (risk {market_risk_level}): "
                f"{assessment.allocation_ratio:.1%} (max: {assessment.allocation_threshold:.1%})"
            )

        total_risk_level += assessment.risk_score

    # Send consolidated allocation alert if any markets exceed thresholds
    if allocation_violations:
        violations_text = "\n".join(allocation_violations)
        message = (
            f"🔺 High allocation in [{vault_name}]({vault_url}) (risk {risk_level}) on {chain.name}\n"
            f"{violations_text}\n"
        )
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))

    # print total risk level and vault name
    logger.info("Total risk level: %s, vault: %s on %s", f"{total_risk_level:.2f}", vault_name, chain.name)
    # round total_risk_level to 2 decimal places
    total_risk_level = round(total_risk_level, 2)
    if total_risk_level > MAX_RISK_THRESHOLDS[risk_level]:
        message = (
            f"⚠️ High risk level in [{vault_name}]({vault_url}) (risk {risk_level}) on {chain.name}\n"
            f"🔢 Risk level: {total_risk_level:.2f} (max: {MAX_RISK_THRESHOLDS[risk_level]:.2f})\n"
            f"🔢 Total assets: ${total_assets:,.2f}\n"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))


def group_vaults_by_chain(vaults_data: List[Dict[str, Any]]) -> Dict[Chain, List[Dict[str, Any]]]:
    """Group vaults by their chain."""
    vaults_by_chain: Dict[Chain, List[Dict[str, Any]]] = {}
    for vault_data in vaults_data:
        chain = Chain.from_chain_id(vault_data["chain"]["id"])
        if chain not in vaults_by_chain:
            vaults_by_chain[chain] = []
        vaults_by_chain[chain].append(vault_data)
    return vaults_by_chain


def find_yv_vaults_for_asset(
    chain_vaults: List[Dict[str, Any]],
    asset_address: str,
    yv_vault_addresses: List[str],
) -> List[Dict[str, Any]]:
    """Find all YV collateral vaults for a specific asset."""
    asset_yv_vaults = []
    yv_vault_addresses_lower = {address.lower() for address in yv_vault_addresses}

    for vault_data in chain_vaults:
        vault_address = vault_data["address"].lower()
        vault_asset_address = vault_data.get("asset", {}).get("address", "").lower()

        # Check if this vault is for the current asset and is YV collateral
        if vault_asset_address == asset_address and vault_address in yv_vault_addresses_lower:
            asset_yv_vaults.append(vault_data)

    return asset_yv_vaults


def calculate_combined_metrics(asset_yv_vaults: List[Dict[str, Any]]) -> tuple[float, float, List[str]]:
    """Calculate combined v1/v2 total assets, liquidity, and vault names."""
    combined_total_assets = 0
    unshared_liquidity = 0.0
    liquidity_sources: Dict[str, List[float]] = {}
    vault_names = []

    for vault in asset_yv_vaults:
        if vault.get("__typename") == "VaultV2":
            total_assets = vault.get("totalAssetsUsd") or 0
            liquidity = vault.get("liquidityUsd") or 0
            vault_name = f"{vault['name']} (V2)"
        else:
            total_assets = vault["state"]["totalAssetsUsd"] or 0
            liquidity = vault["liquidity"]["usd"] or 0
            vault_name = vault["name"]

        # Only include vaults with meaningful assets
        if total_assets >= MIN_VAULT_ASSETS_USD:
            combined_total_assets += total_assets
            vault_names.append(vault_name)
            sources = _get_vault_liquidity_sources(vault, liquidity)
            source_total = sum(source[1] for source in sources)
            scale = min(liquidity / source_total, 1.0) if source_total else 0.0
            attributed_liquidity = source_total * scale
            unshared_liquidity += max(liquidity - attributed_liquidity, 0)

            for source_key, source_liquidity, source_cap in sources:
                source_liquidity *= scale
                if source_key in liquidity_sources:
                    liquidity_sources[source_key][0] += source_liquidity
                    liquidity_sources[source_key][1] = min(liquidity_sources[source_key][1], source_cap)
                else:
                    liquidity_sources[source_key] = [source_liquidity, source_cap]

    shared_liquidity = sum(min(liquidity, cap) for liquidity, cap in liquidity_sources.values())
    combined_liquidity = unshared_liquidity + shared_liquidity

    return combined_total_assets, combined_liquidity, vault_names


def _get_vault_liquidity_sources(vault: Dict[str, Any], reported_liquidity: float) -> List[tuple[str, float, float]]:
    """Return market-backed liquidity contributions as (market, amount, market cap)."""
    if vault.get("__typename") == "VaultV2":
        idle_liquidity = min(float(vault.get("idleAssetsUsd") or 0), reported_liquidity)
        market = (vault.get("liquidityData") or {}).get("market")
        market_liquidity = (market or {}).get("state", {}).get("liquidityAssetsUsd")
        if market is None or market_liquidity is None:
            return []
        adapter_liquidity = max(reported_liquidity - idle_liquidity, 0)
        return [(market["marketId"].lower(), adapter_liquidity, float(market_liquidity))]

    sources = []
    for allocation in vault.get("state", {}).get("allocation") or []:
        market = allocation.get("market") or {}
        market_liquidity = market.get("state", {}).get("liquidityAssetsUsd")
        if allocation.get("withdrawQueueIndex") is None or market.get("collateralAsset") is None:
            continue
        if market_liquidity is None:
            continue
        supplied = float(allocation.get("supplyAssetsUsd") or 0)
        sources.append((market["marketId"].lower(), min(supplied, float(market_liquidity)), float(market_liquidity)))
    return sources


def parse_lltv(lltv: str | int | None) -> float:
    """Convert Morpho's WAD-scaled LLTV into a decimal ratio."""
    if lltv is None:
        return 0.0
    try:
        return int(lltv) / 1e18
    except (TypeError, ValueError):
        return 0.0


def get_yv_collateral_price_shock(lltv: str | int | None) -> float:
    """Pick the adverse price move used for collateral-at-risk checks from LLTV."""
    lltv_ratio = parse_lltv(lltv)
    if lltv_ratio >= 0.86:
        return YV_COLLATERAL_STABLE_PRICE_SHOCK
    if lltv_ratio <= 0.77:
        return YV_COLLATERAL_VOLATILE_PRICE_SHOCK
    return YV_COLLATERAL_FALLBACK_PRICE_SHOCK


def get_yv_collateral_liquidity_by_asset(
    chain: Chain,
    chain_vaults: List[Dict[str, Any]],
    chain_v2_vaults: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Build withdrawable liquidity groups for Yearn-vault collateral underlying assets."""
    yv_vaults_by_asset = get_collateral_vaults_by_asset(chain, version=1)
    yv_v2_vaults_by_asset = get_collateral_vaults_by_asset(chain, version=2)
    liquidity_by_asset: Dict[str, Dict[str, Any]] = {}

    asset_addresses = yv_vaults_by_asset.keys() | yv_v2_vaults_by_asset.keys()
    for asset_address in asset_addresses:
        asset_yv_vaults = find_yv_vaults_for_asset(
            chain_vaults,
            asset_address,
            [vault.address for vault in yv_vaults_by_asset.get(asset_address, [])],
        )
        asset_yv_vaults.extend(
            find_yv_vaults_for_asset(
                chain_v2_vaults,
                asset_address,
                [vault.address for vault in yv_v2_vaults_by_asset.get(asset_address, [])],
            )
        )

        if not asset_yv_vaults:
            continue

        asset_symbol = asset_yv_vaults[0].get("asset", {}).get("symbol", "UNKNOWN")
        (
            combined_total_assets,
            combined_liquidity,
            vault_names,
        ) = calculate_combined_metrics(asset_yv_vaults)

        if combined_total_assets < MIN_VAULT_ASSETS_USD:
            logger.info(
                "%s YV collateral liquidity group has only $%s total assets; retaining zero/low liquidity coverage",
                asset_symbol,
                f"{combined_total_assets:,.2f}",
            )

        asset_key = asset_address.lower()
        group_data = {
            "asset_symbol": asset_symbol,
            "asset_address": asset_key,
            "combined_total_assets": combined_total_assets,
            "combined_liquidity": combined_liquidity,
            "vault_names": vault_names,
            "vault_count": len(vault_names),
        }
        if asset_key in liquidity_by_asset:
            existing = liquidity_by_asset[asset_key]
            logger.warning(
                "Duplicate YV collateral liquidity group for %s on %s; aggregating %s into existing group",
                asset_symbol,
                chain.name,
                asset_address,
            )
            existing["combined_total_assets"] += group_data["combined_total_assets"]
            existing["combined_liquidity"] += group_data["combined_liquidity"]
            existing["vault_names"].extend(group_data["vault_names"])
            existing["vault_count"] += group_data["vault_count"]
        else:
            liquidity_by_asset[asset_key] = group_data

        liquidity_ratio = combined_liquidity / combined_total_assets if combined_total_assets else 0
        logger.info(
            "YV collateral liquidity group %s on %s: %s vaults, $%s total assets, $%s liquidity (%s)",
            asset_symbol,
            chain.name,
            len(vault_names),
            f"{combined_total_assets:,.2f}",
            f"{combined_liquidity:,.2f}",
            f"{liquidity_ratio:.1%}",
        )

    return liquidity_by_asset


def collect_yv_collateral_markets(
    chain: Chain,
    configured_markets: List[Dict[str, Any]],
    liquidity_by_asset: Dict[str, Dict[str, Any]],
) -> Dict[str, tuple[Dict[str, Any], Dict[str, Any]]]:
    """Collect configured direct Yearn vault collateral markets."""
    configured_markets_by_asset = YV_COLLATERAL_MARKETS_BY_ASSET.get(chain, {})
    market_to_asset = {
        market_id.lower(): asset_address.lower()
        for asset_address, market_ids in configured_markets_by_asset.items()
        for market_id in market_ids
    }
    markets: Dict[str, tuple[Dict[str, Any], Dict[str, Any]]] = {}

    for market in configured_markets:
        collateral_asset = market.get("collateralAsset")
        if collateral_asset is None or collateral_asset.get("chain", {}).get("id") != chain.chain_id:
            continue

        market_id = market["marketId"]
        asset_address = market_to_asset.get(market_id.lower())
        if asset_address is None:
            continue

        borrow_usd = market.get("state", {}).get("borrowAssetsUsd") or 0
        if borrow_usd < YV_COLLATERAL_MIN_BORROW_USD:
            continue

        liquidity_group = liquidity_by_asset.get(asset_address)
        if liquidity_group is None:
            raise MorphoMonitoringError(
                f"No {chain.name} liquidity group for configured YV collateral market {market_id} "
                f"and asset {asset_address}"
            )

        markets[market_id] = (market, liquidity_group)

    return markets


def get_markets_collateral_at_risk_usd(market_shocks: Dict[str, float], chain: Chain) -> Dict[str, float]:
    """Fetch Morpho collateral at risk for many markets in a single aliased request.

    Args:
        market_shocks: Mapping of market id to the adverse price move to evaluate for that market.
        chain: Chain the markets live on.

    Returns:
        Mapping of market id to collateral-at-risk USD at its target price.
    """
    if not market_shocks:
        return {}

    alias_to_market = {f"m{index}": market_id for index, market_id in enumerate(market_shocks)}
    variable_definitions = ["$chainId: Int!", "$numberOfPoints: Int!"]
    variables: Dict[str, Any] = {
        "chainId": chain.chain_id,
        "numberOfPoints": YV_COLLATERAL_AT_RISK_POINTS,
    }
    query_fields = []
    for alias, market_id in alias_to_market.items():
        variable_definitions.append(f"${alias}: String!")
        variables[alias] = market_id
        query_fields.append(
            f"{alias}: marketCollateralAtRisk(uniqueKey: ${alias}, chainId: $chainId, numberOfPoints: $numberOfPoints) {{"
            " collateralAtRisk { collateralPriceRatio collateralUsd } }"
        )

    query = (
        "query GetMarketsCollateralAtRisk("
        + ", ".join(variable_definitions)
        + ") {\n"
        + "\n".join(query_fields)
        + "\n}"
    )
    response_data = execute_graphql(query, variables, f"collateral at risk on {chain.name}")
    collateral_at_risk_by_market: Dict[str, float] = {}
    for alias, market_id in alias_to_market.items():
        market_data = response_data.get(alias) or {}
        points = market_data.get("collateralAtRisk", [])
        if not points:
            collateral_at_risk_by_market[market_id] = 0
            continue

        target_price_ratio = 1 - market_shocks[market_id]
        target_point = min(points, key=lambda point: abs((point.get("collateralPriceRatio") or 0) - target_price_ratio))
        collateral_at_risk_by_market[market_id] = target_point.get("collateralUsd") or 0

    return collateral_at_risk_by_market


def check_yv_collateral_market_liquidity(
    chain: Chain,
    configured_markets: List[Dict[str, Any]],
    liquidity_by_asset: Dict[str, Dict[str, Any]],
) -> None:
    """Alert only when underlying liquidity cannot cover risky direct YV collateral liquidations."""
    markets = collect_yv_collateral_markets(chain, configured_markets, liquidity_by_asset)
    if not markets:
        return

    market_shocks = {
        market_id: get_yv_collateral_price_shock(market.get("lltv"))
        for market_id, (market, liquidity_group) in markets.items()
    }
    collateral_at_risk_by_market = get_markets_collateral_at_risk_usd(market_shocks, chain)

    checks_by_asset: Dict[str, Dict[str, Any]] = {}
    for market_id, (market, liquidity_group) in markets.items():
        collateral_asset = market["collateralAsset"]
        loan_asset = market["loanAsset"]
        asset_symbol = liquidity_group["asset_symbol"]
        price_shock = market_shocks[market_id]
        collateral_at_risk = collateral_at_risk_by_market.get(market_id)

        if collateral_at_risk is None:
            continue
        if collateral_at_risk < YV_COLLATERAL_MIN_AT_RISK_USD:
            logger.info(
                "Skipping %s/%s YV liquidity check on %s: collateral at risk $%s below threshold",
                collateral_asset["symbol"],
                loan_asset["symbol"],
                chain.name,
                f"{collateral_at_risk:,.2f}",
            )
            continue

        asset_address = liquidity_group["asset_address"]
        required_liquidity = collateral_at_risk * YV_COLLATERAL_LIQUIDATION_BUFFER
        group_check = checks_by_asset.setdefault(
            asset_address,
            {
                "liquidity_group": liquidity_group,
                "total_collateral_at_risk": 0.0,
                "total_required_liquidity": 0.0,
                "market_lines": [],
            },
        )
        group_check["total_collateral_at_risk"] += collateral_at_risk
        group_check["total_required_liquidity"] += required_liquidity
        market_url = get_market_url(market_id, chain)
        market_name = f"{collateral_asset['symbol']}/{loan_asset['symbol']}"
        group_check["market_lines"].append(
            f"- [{market_name}]({market_url}): ${collateral_at_risk:,.2f} at risk "
            f"({price_shock:.0%} shock, LLTV {parse_lltv(market.get('lltv')):.1%})"
        )

    for group_check in checks_by_asset.values():
        liquidity_group = group_check["liquidity_group"]
        combined_liquidity = liquidity_group["combined_liquidity"]
        required_liquidity = group_check["total_required_liquidity"]
        coverage = combined_liquidity / required_liquidity
        asset_symbol = liquidity_group["asset_symbol"]

        logger.info(
            "YV collateral liquidity check for %s on %s: $%s liquidity, $%s collateral at risk, %sx coverage",
            asset_symbol,
            chain.name,
            f"{combined_liquidity:,.2f}",
            f"{group_check['total_collateral_at_risk']:,.2f}",
            f"{coverage:.2f}",
        )

        if combined_liquidity >= required_liquidity:
            continue

        vault_list = ", ".join(liquidity_group["vault_names"])
        market_lines = "\n".join(group_check["market_lines"])
        message = (
            f"⚠️ Insufficient {asset_symbol} unwind liquidity for YV collateral markets on {chain.name}\n"
            f"🏦 Vaults: {vault_list}\n"
            f"💰 Withdrawable {asset_symbol}: ${combined_liquidity:,.2f}\n"
            f"🔥 Total collateral at risk: ${group_check['total_collateral_at_risk']:,.2f}\n"
            f"📊 Required with buffer: ${required_liquidity:,.2f} ({coverage:.2f}x coverage)\n"
            f"💹 Markets:\n{market_lines}\n"
        )
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))


def check_individual_liquidity_for_chain(chain: Chain, chain_vaults: List[Dict[str, Any]]) -> None:
    """Check individual liquidity for non-YV collateral vaults on a specific chain."""
    for vault_data in chain_vaults:
        vault_address = vault_data["address"]
        if not is_collateral_vault(vault_address, chain, version=1):
            check_low_liquidity(vault_data)


def check_low_liquidity_combined(
    vaults_data: List[Dict[str, Any]],
    v2_vaults_data: List[Dict[str, Any]],
    configured_markets: List[Dict[str, Any]],
) -> None:
    """
    Check individual and combined collateral-strategy vault liquidity.
    For YV collateral vaults, combine all vaults with the same asset and check if
    combined liquidity can cover direct Yearn-vault collateral liquidations at risk.
    """
    # Group vaults by chain for processing
    vaults_by_chain = group_vaults_by_chain(vaults_data)
    v2_vaults_by_chain = group_vaults_by_chain(v2_vaults_data)

    # Process each chain separately
    for chain in vaults_by_chain.keys() | v2_vaults_by_chain.keys():
        chain_vaults = vaults_by_chain.get(chain, [])
        chain_v2_vaults = v2_vaults_by_chain.get(chain, [])
        # Check market-aware YV collateral unwind liquidity
        yv_liquidity_by_asset = get_yv_collateral_liquidity_by_asset(chain, chain_vaults, chain_v2_vaults)
        check_yv_collateral_market_liquidity(chain, configured_markets, yv_liquidity_by_asset)

        # Check individual liquidity for non-YV collateral vaults
        check_individual_liquidity_for_chain(chain, chain_vaults)


def check_low_liquidity(vault_data: Dict[str, Any]) -> None:
    """
    Send telegram message if low liquidity is detected.
    """
    vault_name = vault_data["name"]
    total_assets = vault_data["state"]["totalAssetsUsd"]
    liquidity = vault_data["liquidity"]["usd"] or 0
    chain = Chain.from_chain_id(vault_data["chain"]["id"])
    vault_url = get_vault_url(vault_data["address"], chain)

    if not total_assets or not is_low_liquidity(total_assets, liquidity):
        return

    message = format_low_liquidity_message(
        vault_name,
        vault_url,
        chain,
        total_assets,
        liquidity,
        LIQUIDITY_THRESHOLD,
    )
    send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))


_VAULTS_QUERY = """
    query GetVaults($addresses: [String!]!, $v2Addresses: [String!]!, $marketIds: [String!]!) {
        vaults(where: { address_in: $addresses } ) {
            items {
                __typename
                address
                name
                chain {
                  id
                }
                asset {
                    address
                    symbol
                    name
                }
                liquidity {
                  usd
                }
                state {
                    totalAssetsUsd
                    allocation {
                        supplyCap
                        supplyAssetsUsd
                        withdrawQueueIndex
                        pendingSupplyCapUsd
                        pendingSupplyCapValidAt
                        market {
                            marketId
                            lltv
                            loanAsset {
                                address
                                symbol
                            }
                            collateralAsset {
                                address
                                symbol
                                chain {
                                    id
                                }
                            }
                            state {
                                utilization
                                borrowAssetsUsd
                                supplyAssetsUsd
                                liquidityAssetsUsd
                            }
                            badDebt {
                                underlying
                                usd
                            }
                        }
                    }
                }
            }
        }
        vaultV2s(first: 200, where: { address_in: $v2Addresses }) {
            items {
                __typename
                address
                name
                chain { id }
                asset { address symbol name }
                totalAssetsUsd
                idleAssetsUsd
                liquidityUsd
                liquidityData {
                    __typename
                    ... on MarketV1LiquidityData {
                        market {
                            marketId
                            state { liquidityAssetsUsd }
                        }
                    }
                }
            }
        }
        markets(first: 200, where: { uniqueKey_in: $marketIds }) {
            items {
                marketId
                lltv
                loanAsset { address symbol }
                collateralAsset {
                    address
                    symbol
                    chain { id }
                }
                state { borrowAssetsUsd }
            }
        }
    }
"""


def get_configured_v2_collateral_vault_addresses() -> List[str]:
    """Return every configured Vault V2 used by a YV-collateral strategy."""
    return [vault.address for _, vault in iter_vaults(VAULTS_V2_BY_CHAIN) if vault.collateral_asset is not None]


def get_configured_yv_collateral_market_ids() -> List[str]:
    """Return every direct YV-collateral Morpho market configured for coverage checks."""
    return [
        market_id
        for markets_by_asset in YV_COLLATERAL_MARKETS_BY_ASSET.values()
        for market_ids in markets_by_asset.values()
        for market_id in market_ids
    ]


def fetch_configured_vaults() -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Fetch configured v1 vaults, v2 collateral vaults, and direct collateral markets."""
    vault_addresses = [vault.address for _, vault in iter_vaults(VAULTS_V1_BY_CHAIN)]
    v2_vault_addresses = get_configured_v2_collateral_vault_addresses()
    market_ids = get_configured_yv_collateral_market_ids()

    data = execute_graphql(
        _VAULTS_QUERY,
        {
            "addresses": vault_addresses,
            "v2Addresses": v2_vault_addresses,
            "marketIds": market_ids,
        },
        "configured vaults and collateral markets",
    )

    vaults_data = data.get("vaults", {}).get("items", [])
    found_v1_addresses = {vault["address"] for vault in vaults_data}
    require_configured_keys(vault_addresses, found_v1_addresses, "Vault V1 addresses")

    v2_vaults_data = data.get("vaultV2s", {}).get("items", [])
    found_v2_addresses = {vault["address"] for vault in v2_vaults_data}
    require_configured_keys(v2_vault_addresses, found_v2_addresses, "YV-collateral Vault V2 addresses")

    markets_data = data.get("markets", {}).get("items", [])
    found_market_ids = {market["marketId"] for market in markets_data}
    require_configured_keys(market_ids, found_market_ids, "YV-collateral market IDs")

    return vaults_data, v2_vaults_data, markets_data


def get_active_vault_markets(vault_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return non-idle markets with a cap and more than $10k supplied."""
    vault_markets = []
    for allocation in vault_data["state"]["allocation"]:
        market_supply_usd = allocation.get("market", {}).get("state", {}).get("supplyAssetsUsd")
        if int(allocation.get("supplyCap", 0)) == 0 or (market_supply_usd or 0) <= 1e4:
            continue
        market = allocation["market"]
        if market["collateralAsset"] is not None:
            vault_markets.append(market)
    return vault_markets


def main() -> None:
    """Check markets for low liquidity, high allocation, risk, and bad debt."""
    logger.info("Checking Morpho markets...")
    vaults_data, v2_vaults_data, configured_markets = fetch_configured_vaults()

    # Check combined liquidity for all vaults (handles YV collateral grouping)
    check_low_liquidity_combined(vaults_data, v2_vaults_data, configured_markets)

    alerted_markets: set[str] = set()

    for vault_data in vaults_data:
        total_assets = vault_data.get("state", {}).get("totalAssetsUsd", 0) or 0
        if total_assets < MIN_VAULT_ASSETS_USD:
            logger.info(
                "Skipping vault %s on chain %s: TVL $%s below $%s min",
                vault_data["name"],
                vault_data["chain"]["id"],
                f"{total_assets:,.2f}",
                f"{MIN_VAULT_ASSETS_USD:,.0f}",
            )
            continue

        # Check per-market allocation and total risk level
        check_allocation_and_risk(vault_data)

        vault_name = vault_data["name"]
        chain = Chain.from_chain_id(vault_data["chain"]["id"])
        vault_url = get_vault_url(vault_data["address"], chain)
        bad_debt_alert(get_active_vault_markets(vault_data), vault_name, vault_url, chain, alerted_markets)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)

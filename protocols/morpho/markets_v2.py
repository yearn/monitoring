"""Morpho VaultV2 markets / allocation / risk monitor.

Fetches every configured V2 vault from Morpho GraphQL in one query (TVL,
liquidity, ``MorphoMarketV1`` adapter positions), then batches market state /
bad debt per chain. No RPC.

Applies the shared [risk.py](./risk.py) policy against each market's
``supplyAssetsUsd``. Normal Vault V2 liquidity uses the API's immediately
withdrawable ``liquidityUsd``; YV-collateral strategy vaults use the combined
v1/v2 coverage check in ``markets.py`` instead.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from web3 import Web3

from protocols.morpho._shared import (
    PROTOCOL,
    MarketMetrics,
    MorphoV2MonitoringError,
    execute_graphql,
    fetch_market_metrics,
    format_low_liquidity_message,
    get_market_url,
    get_vault_url,
    require_configured_keys,
)
from protocols.morpho.config import (
    VAULTS_V2_BY_CHAIN,
    get_vault_query_config,
    is_collateral_vault,
)
from protocols.morpho.risk import (
    LIQUIDITY_THRESHOLD,
    MAX_RISK_THRESHOLDS,
    MIN_VAULT_ASSETS_USD,
    assess_exposure,
    get_market_risk_level,
    is_low_liquidity,
)
from utils.alert import Alert, AlertSeverity, send_alert
from utils.chains import Chain
from utils.logger import get_logger

logger = get_logger("morpho.markets_v2")

ADAPTER_TYPE_MARKET = "MorphoMarketV1"
# GraphQL complexity scales with these `first` args — keep headroom under 1M.
MAX_ADAPTERS_PER_VAULT = 3
MAX_POSITIONS_PER_ADAPTER = 20


@dataclass
class V2Vault:
    """Yearn-curated V2 vault declared in ``VAULTS_V2_BY_CHAIN``."""

    name: str
    address: str
    chain: Chain
    asset_address: str
    asset_symbol: str
    risk_level: int
    total_assets_usd: float = 0.0
    liquidity_usd: float = 0.0
    # market_id (lowercase) -> vault supply USD from MorphoMarketV1Adapter positions
    market_allocations_usd: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketAssessment:
    """Risk contribution and optional alert lines for one Morpho market."""

    risk_score: float
    allocation_violation: Optional[str] = None
    bad_debt_alert: Optional[str] = None


# ----------------------------------------------------------------------------
# Vault state fetch (static list → GraphQL)
# ----------------------------------------------------------------------------


_STATE_QUERY = f"""
query VaultV2State($addresses: [String!]!) {{
  vaultV2s(first: 200, where: {{ address_in: $addresses }}) {{
    items {{
      address
      name
      chain {{ id }}
      asset {{ address symbol }}
      totalAssetsUsd
      liquidityUsd
      adapters(first: {MAX_ADAPTERS_PER_VAULT}) {{
        items {{
          address
          type
          ... on MorphoMarketV1Adapter {{
            positions(first: {MAX_POSITIONS_PER_ADAPTER}) {{
              items {{
                state {{ supplyAssetsUsd }}
                market {{ marketId }}
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


def discover_v2_vaults_by_chain() -> Dict[Chain, List[V2Vault]]:
    """Load state + market allocations for every vault in ``VAULTS_V2_BY_CHAIN``.

    Issues a single GraphQL ``vaultV2s`` query with adapter positions, then joins
    back to the static list for risk level. Raises if the API omits a configured
    vault, returns a non-market adapter, or hits the positions page size.
    """
    addr_to_meta, addresses, _chain_ids = get_vault_query_config(VAULTS_V2_BY_CHAIN)

    if not addresses:
        return {}

    data = execute_graphql(
        _STATE_QUERY,
        {"addresses": addresses},
        "Vault V2 state",
        error_type=MorphoV2MonitoringError,
    )
    items = data.get("vaultV2s", {}).get("items") or []
    by_addr: dict[str, dict[str, Any]] = {item["address"].lower(): item for item in items}
    require_configured_keys(
        addr_to_meta,
        by_addr,
        "Vault V2 addresses",
        error_type=MorphoV2MonitoringError,
    )

    result: Dict[Chain, List[V2Vault]] = {chain: [] for chain in VAULTS_V2_BY_CHAIN}
    for addr_lc, (chain, config) in addr_to_meta.items():
        item = by_addr[addr_lc]
        result.setdefault(chain, []).append(
            V2Vault(
                name=config.name,
                address=Web3.to_checksum_address(item["address"]),
                chain=chain,
                asset_address=item["asset"]["address"],
                asset_symbol=item["asset"]["symbol"],
                risk_level=config.risk_level,
                total_assets_usd=float(item.get("totalAssetsUsd") or 0),
                liquidity_usd=float(item.get("liquidityUsd") or 0),
                market_allocations_usd=_parse_market_allocations(item, config.name, chain),
            )
        )

    for chain, chain_vaults in result.items():
        logger.info("Loaded %d V2 vault(s) on %s", len(chain_vaults), chain.name)
    return result


def _parse_market_allocations(item: Dict[str, Any], vault_name: str, chain: Chain) -> Dict[str, float]:
    """Aggregate MorphoMarketV1 position supply USD by market id."""
    adapters = (item.get("adapters") or {}).get("items") or []
    if len(adapters) >= MAX_ADAPTERS_PER_VAULT:
        raise MorphoV2MonitoringError(
            f"Vault V2 {vault_name} on {chain.name} returned {len(adapters)} adapters; "
            f"raise MAX_ADAPTERS_PER_VAULT (currently {MAX_ADAPTERS_PER_VAULT})"
        )

    allocations: Dict[str, float] = {}
    for adapter in adapters:
        adapter_type = adapter.get("type")
        if adapter_type != ADAPTER_TYPE_MARKET:
            raise MorphoV2MonitoringError(
                f"Vault V2 {vault_name} on {chain.name} has unsupported adapter type "
                f"{adapter_type!r} at {adapter.get('address')} (expected {ADAPTER_TYPE_MARKET})"
            )

        positions = (adapter.get("positions") or {}).get("items") or []
        if len(positions) >= MAX_POSITIONS_PER_ADAPTER:
            raise MorphoV2MonitoringError(
                f"Vault V2 {vault_name} on {chain.name} adapter {adapter.get('address')} "
                f"returned {len(positions)} positions; raise MAX_POSITIONS_PER_ADAPTER "
                f"(currently {MAX_POSITIONS_PER_ADAPTER})"
            )

        for position in positions:
            market_id = ((position.get("market") or {}).get("marketId") or "").lower()
            supply_usd = float((position.get("state") or {}).get("supplyAssetsUsd") or 0)
            if not market_id or supply_usd <= 0:
                continue
            allocations[market_id] = allocations.get(market_id, 0.0) + supply_usd

    return allocations


# ----------------------------------------------------------------------------
# Risk / allocation analysis
# ----------------------------------------------------------------------------


def score_market_allocations(
    vault: V2Vault,
    metrics: Dict[str, MarketMetrics],
) -> None:
    """Score every market allocation on a vault and emit consolidated alerts."""
    if vault.total_assets_usd <= 0 or not vault.market_allocations_usd:
        return

    total_risk_score = 0.0
    allocation_violations: list[str] = []
    bad_debt_alerts: list[str] = []

    for market_id, allocation_usd in vault.market_allocations_usd.items():
        assessment = _assess_market(vault, market_id, allocation_usd, metrics)
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
    _alert_vault_risk(vault, total_risk_score)


def _assess_market(
    vault: V2Vault,
    market_id: str,
    allocation_usd: float,
    metrics: Dict[str, MarketMetrics],
) -> Optional[MarketAssessment]:
    """Calculate risk and alert details for one market exposure."""
    market = metrics.get(market_id)
    if market is None:
        logger.info("No GraphQL data for market %s; skipping", market_id)
        return None

    allocation_ratio = min(allocation_usd / vault.total_assets_usd, 1.0)
    risk_level = get_market_risk_level(market_id, vault.chain)
    assessment = assess_exposure(
        allocation_ratio,
        risk_level,
        vault.risk_level,
        bad_debt_usd=market.bad_debt.usd,
        borrow_assets_usd=market.state.borrow_assets_usd,
    )
    market_label = _market_label(market, market_id, vault.chain)
    allocation_violation = None
    if assessment.allocation_exceeded:
        allocation_violation = (
            f"- {market_label} (risk {risk_level}): {assessment.allocation_ratio:.1%} "
            f"(max: {assessment.allocation_threshold:.1%})"
        )

    bad_debt_usd = market.bad_debt.usd
    bad_debt_alert = None
    if assessment.bad_debt_exceeded:
        bad_debt_alert = f"- {market_label}: ${bad_debt_usd:,.2f} ({assessment.bad_debt_ratio:.2%} of borrowed)"

    return MarketAssessment(
        risk_score=assessment.risk_score,
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


def _alert_vault_risk(vault: V2Vault, total_risk_score: float) -> None:
    """Alert when a Vault V2 weighted risk score exceeds its tier limit."""
    max_risk = MAX_RISK_THRESHOLDS[vault.risk_level]
    if total_risk_score <= max_risk:
        return

    vault_url = get_vault_url(vault.address, vault.chain)
    message = (
        f"⚠️ V2 high risk in [{vault.name}]({vault_url}) (risk {vault.risk_level}) on {vault.chain.name}\n"
        f"🔢 Risk level: {total_risk_score:.2f} (max: {max_risk:.2f})\n"
        f"🔢 Total assets: ${vault.total_assets_usd:,.2f}"
    )
    send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))


def _market_label(market: MarketMetrics, market_id: str, chain: Chain) -> str:
    """Render a clickable Markdown label for a Morpho Blue market."""
    loan = market.loan_asset.symbol or "?"
    coll = market.collateral_asset.symbol if market.collateral_asset else "idle"
    return f"[{coll}/{loan}]({get_market_url(market_id, chain)})"


def analyze_v2_vault(vault: V2Vault, metrics: Dict[str, MarketMetrics]) -> None:
    """Run all v2 monitoring checks for a single vault."""
    if vault.total_assets_usd < MIN_VAULT_ASSETS_USD:
        logger.info(
            "Skipping V2 vault %s on %s: TVL $%s below $%s min",
            vault.name,
            vault.chain.name,
            f"{vault.total_assets_usd:,.2f}",
            f"{MIN_VAULT_ASSETS_USD:,.0f}",
        )
        return

    score_market_allocations(vault, metrics)

    if not is_collateral_vault(vault.address, vault.chain, version=2):
        check_low_liquidity(vault)


def check_low_liquidity(vault: V2Vault) -> None:
    """Alert when a non-collateral Vault V2 has less than 1% withdrawable liquidity."""
    if not is_low_liquidity(vault.total_assets_usd, vault.liquidity_usd):
        return

    vault_url = get_vault_url(vault.address, vault.chain)
    message = format_low_liquidity_message(
        vault.name,
        vault_url,
        vault.chain,
        vault.total_assets_usd,
        vault.liquidity_usd,
        LIQUIDITY_THRESHOLD,
        version_label="V2",
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

        market_ids = sorted({market_id for vault in vaults for market_id in vault.market_allocations_usd})
        try:
            metrics = fetch_market_metrics(market_ids, chain)
        except Exception as e:
            logger.exception("Failed to fetch market metrics on %s", chain.name)
            failures.append(f"markets on {chain.name}: {type(e).__name__}: {e}")
            continue

        for vault in vaults:
            try:
                analyze_v2_vault(vault, metrics)
            except Exception as e:
                logger.exception("Failed to analyze V2 vault %s on %s", vault.address, chain.name)
                failures.append(f"{vault.name} on {chain.name}: {type(e).__name__}: {e}")

    if failures:
        raise MorphoV2MonitoringError("Failed Morpho Vault V2 analyses: " + "; ".join(failures))


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)

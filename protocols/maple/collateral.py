"""
Maple Finance Syrup collateral monitoring.

Fetches collateral breakdown from the Maple Finance GraphQL API across both
syrupUSDC and syrupUSDT pools, and calculates a weighted risk score based on
predefined asset risk ratings.

Uses syrupGlobals for the official combined collateralization ratio across all
Syrup pools (syrupUSDC + syrupUSDT). The ratio uses only overcollateralized loans
as the denominator, excluding DeFi strategy deployments (Sky, Aave, etc.).
See: https://docs.maple.finance/integrate/technical-resources/collateral-and-yield-disclosure

Monitors:
- Weighted collateral risk score — alerts when above threshold
- Collateralization ratio (via syrupGlobals) — alerts when ratio drops below threshold
- Unrealized losses — alerts when >= 0.5% of pool total assets
"""

import time
from datetime import datetime, timezone
from typing import Any

import requests

from utils.alert import Alert, AlertSeverity, send_alert
from utils.config import Config
from utils.formatting import format_usd
from utils.http_client import request_with_retry
from utils.logger import get_logger
from utils.telegram import send_error_message

PROTOCOL = "maple"
logger = get_logger(PROTOCOL)

MAPLE_GRAPHQL_URL = "https://api.maple.finance/v2/graphql"
SYRUP_USDC_POOL_ID = "0x80ac24aa929eaf5013f6436cda2a7ba190f5cc0b"
SYRUP_USDT_POOL_ID = "0x356b8d89c1e1239cbbb9de4815c39a1474d5ba7d"

# Asset risk scores from issue #147
# 1 = low risk, 2 = medium risk, 3 = high risk
ASSET_RISK_SCORES: dict[str, int] = {
    "BTC": 1,
    "ETH": 1,
    "cbBTC": 1,
    "sUSDS": 1,
    "weETH": 2,
    "SOL": 2,
    "XRP": 2,
    "USTB": 2,
    "LBTC": 2,
    "HYPE": 2,
    "jitoSOL": 3,
    "LP_USR": 3,
    "OrcaLP_PYUSDC": 3,
    "PT_USR": 3,
    "PT_sUSDE": 3,
    "USR": 3,
    "tETH": 3,
}

# Alert if collateralization ratio drops below this threshold
COLLATERALIZATION_RATIO_THRESHOLD = 1.35  # 135%

# Alert if unrealized losses exceed this % of pool total assets
UNREALIZED_LOSSES_THRESHOLD = 0.005  # 0.5%

# Alert if Proof-of-Reserves total collateral diverges from syrupGlobals by more than this
PROOF_OF_RESERVES_DIVERGENCE_THRESHOLD = 0.001  # 0.1%

# syrupGlobals provides the official combined collateralization ratio across all Syrup pools.
# collateralRatio = collateralValue / loansValue (only overcollateralized loans, excludes DeFi strategies).
# See: https://docs.maple.finance/integrate/technical-resources/collateral-and-yield-disclosure
SYRUP_GLOBALS_QUERY = """
{
  syrupGlobals {
    collateralRatio
    collateralValue
    loansValue
  }
}
"""

# Proof of Reserves is Maple's third-party attestation (by The Network Firm).
# The GraphQL endpoint exposes only an aggregate totalCollateralValue, so we use
# it to sanity-check the syrupGlobals collateral figure.
PROOF_OF_RESERVES_QUERY = """
{
  proofOfReserves {
    totalCollateralValue
  }
}
"""

COLLATERAL_QUERY = """
{
  _meta {
    block {
      number
      timestamp
    }
  }
  poolV2S(where: {id_in: ["%s", "%s"]}) {
    id
    name
    totalAssets
    principalOut
    unrealizedLosses
    accountedInterest
  }
}
""" % (SYRUP_USDC_POOL_ID, SYRUP_USDT_POOL_ID)

# collateralDisclosure lists the assets backing each pool. Unlike poolCollaterals,
# it does not provide USD values, but its resolver does not fail on unregistered
# native assets, so we use it to detect new/unknown collateral types.
COLLATERAL_DISCLOSURE_QUERY = """
{
  poolV2S(where: {id_in: ["%s", "%s"]}) {
    id
    name
    poolMeta {
      collateralDisclosure {
        asset
      }
    }
  }
}
""" % (SYRUP_USDC_POOL_ID, SYRUP_USDT_POOL_ID)


def _format_graphql_errors(errors: Any) -> str:
    if not isinstance(errors, list):
        return str(errors)

    error_lines = []
    for error in errors:
        if not isinstance(error, dict):
            error_lines.append(str(error))
            continue

        message = error.get("message", "unknown error")
        path = error.get("path") or []
        code = (error.get("extensions") or {}).get("code")

        details = str(message)
        if path:
            details += f" at {'.'.join(str(part) for part in path)}"
        if code:
            details += f" ({code})"
        error_lines.append(details)

    return "; ".join(error_lines)


def _is_retryable_graphql_error(errors: Any) -> bool:
    if not isinstance(errors, list):
        return False

    for error in errors:
        if not isinstance(error, dict):
            continue

        message = str(error.get("message", "")).lower()
        code = (error.get("extensions") or {}).get("code")

        # Data errors such as an unknown native-asset symbol are not transient;
        # retrying will not fix them.
        if "native asset" in message and "not found" in message:
            return False

        if code == "INTERNAL_SERVER_ERROR" or "database unavailable" in message or "store error" in message:
            return True

    return False


def _post_maple_graphql(query: str, query_name: str) -> dict:
    retries = Config.get_retry_count()
    backoff_factor = Config.get_backoff_factor()

    for attempt in range(retries + 1):
        response = request_with_retry(
            "post",
            MAPLE_GRAPHQL_URL,
            json={"query": query},
            timeout=30,
        )
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"Maple GraphQL {query_name} response is not a JSON object")

        errors = data.get("errors")
        if not errors:
            return data

        if _is_retryable_graphql_error(errors) and attempt < retries:
            wait_time = backoff_factor * (2**attempt)
            logger.warning(
                "Maple GraphQL %s returned retryable errors (attempt %d/%d): %s. Retrying in %.1fs...",
                query_name,
                attempt + 1,
                retries + 1,
                _format_graphql_errors(errors),
                wait_time,
            )
            time.sleep(wait_time)
            continue

        raise ValueError(f"Maple GraphQL {query_name} errors: {_format_graphql_errors(errors)}")

    raise ValueError(f"Maple GraphQL {query_name} errors: retry attempts exhausted")


def _alert_maple_graphql_skip(check_name: str, error: Exception) -> None:
    logger.warning("Skipping Maple %s check: %s", check_name, error)
    send_error_message(
        f"Maple GraphQL unavailable; skipping {check_name} check for this run. Error: {error}",
        PROTOCOL,
    )


def fetch_pools_data() -> list[dict]:
    """Fetch per-pool data for syrupUSDC and syrupUSDT from Maple GraphQL API.

    The valued `poolCollaterals` resolver is currently broken for unregistered
    native assets (e.g. PT_sUSDE), so this query no longer requests it.
    Per-asset collateral values are unavailable until Maple fixes the resolver.

    Returns:
        pools_data: per-pool totalAssets, principalOut, unrealizedLosses,
        accountedInterest fields.

    Raises:
        ValueError: If the API response is malformed or pools not found.
        requests.RequestException: If the API request fails.
    """
    data = _post_maple_graphql(COLLATERAL_QUERY, "pools")

    # Log subgraph sync status
    meta = data.get("data", {}).get("_meta", {})
    block_info = meta.get("block", {})
    block_number = block_info.get("number")
    block_timestamp = block_info.get("timestamp")
    if block_timestamp:
        sync_time = datetime.fromtimestamp(block_timestamp, tz=timezone.utc)
        logger.info("Subgraph synced to block %s (%s UTC)", block_number, sync_time.strftime("%Y-%m-%d %H:%M:%S"))

    pools = data.get("data", {}).get("poolV2S", [])
    if not pools:
        raise ValueError("No Syrup pools found in Maple API response")

    if len(pools) < 2:
        logger.warning(
            "Expected 2 Syrup pools (syrupUSDC + syrupUSDT), got %d — subgraph may be incomplete",
            len(pools),
        )

    pools_data = []
    for pool in pools:
        pool_name = pool.get("name", pool["id"])
        pools_data.append(
            {
                "name": pool_name,
                "totalAssets": int(pool.get("totalAssets", "0")),
                "principalOut": int(pool.get("principalOut", "0")),
                "unrealizedLosses": int(pool.get("unrealizedLosses", "0")),
                "accountedInterest": int(pool.get("accountedInterest", "0")),
            }
        )

    return pools_data


def fetch_collateral_disclosure() -> set[str]:
    """Fetch the list of disclosed collateral assets for both Syrup pools.

    Returns:
        Set of unique asset symbols disclosed across syrupUSDC and syrupUSDT.

    Raises:
        ValueError: If the API response is malformed or pools not found.
        requests.RequestException: If the API request fails.
    """
    data = _post_maple_graphql(COLLATERAL_DISCLOSURE_QUERY, "collateralDisclosure")

    pools = data.get("data", {}).get("poolV2S", [])
    if not pools:
        raise ValueError("No Syrup pools found in Maple API response")

    assets: set[str] = set()
    for pool in pools:
        for disclosure in pool.get("poolMeta", {}).get("collateralDisclosure", []):
            asset = disclosure.get("asset")
            if asset:
                assets.add(asset)

    return assets


def fetch_syrup_globals() -> dict:
    """Fetch combined collateralization data from syrupGlobals.

    Returns the official combined ratio across all Syrup pools. loansValue only
    includes overcollateralized borrower loans, excluding DeFi strategy deployments.

    Returns:
        Dict with collateralRatio (float), collateralValue (float USD), loansValue (float USD).

    Raises:
        ValueError: If the API response is malformed.
        requests.RequestException: If the API request fails.
    """
    data = _post_maple_graphql(SYRUP_GLOBALS_QUERY, "syrupGlobals")

    globals_data = data.get("data", {}).get("syrupGlobals")
    if not globals_data:
        raise ValueError("syrupGlobals not found in Maple API response")

    collateral_ratio = globals_data.get("collateralRatio")
    collateral_value = globals_data.get("collateralValue")
    loans_value = globals_data.get("loansValue")
    if collateral_ratio is None or collateral_value is None or loans_value is None:
        raise ValueError(
            f"syrupGlobals missing required fields: collateralRatio={collateral_ratio!r}, "
            f"collateralValue={collateral_value!r}, loansValue={loans_value!r}"
        )

    return {
        "collateralRatio": int(collateral_ratio) / 1e8,
        "collateralValue": int(collateral_value) / 1e6,
        "loansValue": int(loans_value) / 1e6,
    }


def fetch_proof_of_reserves() -> float:
    """Fetch the aggregate collateral value from Maple's Proof of Reserves attestation.

    Returns:
        Total collateral value in USD.

    Raises:
        ValueError: If the API response is malformed.
        requests.RequestException: If the API request fails.
    """
    data = _post_maple_graphql(PROOF_OF_RESERVES_QUERY, "proofOfReserves")

    por_snapshots = data.get("data", {}).get("proofOfReserves")
    if not por_snapshots or not isinstance(por_snapshots, list):
        raise ValueError("proofOfReserves not found in Maple API response")

    latest_snapshot = por_snapshots[0]
    total_collateral_value = latest_snapshot.get("totalCollateralValue")
    if total_collateral_value is None:
        raise ValueError("proofOfReserves missing totalCollateralValue")

    return float(total_collateral_value)


def check_collateralization_ratio() -> None:
    """Check combined collateralization ratio via syrupGlobals and alert if below threshold.

    Uses Maple's official syrupGlobals endpoint which provides the combined ratio
    across all Syrup pools. loansValue only includes overcollateralized borrower loans,
    excluding DeFi strategy deployments (Sky, Aave, etc.).
    """
    syrup_globals = fetch_syrup_globals()
    ratio = syrup_globals["collateralRatio"]
    collateral_usd = syrup_globals["collateralValue"]
    loans_usd = syrup_globals["loansValue"]

    logger.info(
        "Collateralization ratio (syrupGlobals): %.1f%% (threshold: %.0f%%) | Collateral: %s / Loans: %s",
        ratio * 100,
        COLLATERALIZATION_RATIO_THRESHOLD * 100,
        format_usd(collateral_usd),
        format_usd(loans_usd),
    )

    if ratio < COLLATERALIZATION_RATIO_THRESHOLD:
        message = (
            f"🚨 *Maple Collateralization Ratio Alert*\n"
            f"📊 Ratio: {ratio:.1%} (threshold: {COLLATERALIZATION_RATIO_THRESHOLD:.0%})\n"
            f"💰 Collateral: {format_usd(collateral_usd)}\n"
            f"📋 Loans (excl. strategies): {format_usd(loans_usd)}\n"
            f"⚠️ Collateral coverage is below safe threshold\n"
            f"🔗 [Pool Details](https://app.maple.finance/earn/details)"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))


def check_unrealized_losses(pools_data: list[dict]) -> None:
    """Check unrealized losses per pool and alert if >= 0.5% of total assets."""
    for pool_data in pools_data:
        total_assets = pool_data["totalAssets"]
        unrealized_losses = pool_data["unrealizedLosses"]
        if total_assets <= 0:
            continue

        ratio = unrealized_losses / total_assets
        if ratio >= UNREALIZED_LOSSES_THRESHOLD:
            losses_usd = unrealized_losses / 1e6
            assets_usd = total_assets / 1e6
            message = (
                f"🚨 *Maple Syrup Unrealized Losses Alert*\n"
                f"📊 {pool_data['name']}: unrealized losses are {ratio:.1%} of pool assets\n"
                f"💰 Losses: {format_usd(losses_usd)} | Pool assets: {format_usd(assets_usd)}\n"
                f"⚠️ Threshold: {UNREALIZED_LOSSES_THRESHOLD:.1%}\n"
                f"🔗 [Pool Details](https://app.maple.finance/earn/details)"
            )
            send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))


def check_proof_of_reserves() -> None:
    """Cross-check Proof-of-Reserves collateral value against syrupGlobals.

    The Network Firm attestation exposes only an aggregate totalCollateralValue.
    We compare it to the on-chain/GraphQL syrupGlobals collateralValue and alert
    if the divergence exceeds the configured threshold.
    """
    try:
        por_total = fetch_proof_of_reserves()
        syrup_globals = fetch_syrup_globals()
    except (requests.RequestException, ValueError) as e:
        _alert_maple_graphql_skip("proof of reserves", e)
        return

    syrup_collateral = syrup_globals["collateralValue"]
    if syrup_collateral <= 0:
        logger.warning("Cannot compare proof of reserves: syrupGlobals collateralValue is zero")
        return

    divergence = abs(por_total - syrup_collateral) / syrup_collateral

    logger.info(
        "Proof of reserves: PoR=%s, syrupGlobals=%s, divergence=%.2f%%",
        format_usd(por_total),
        format_usd(syrup_collateral),
        divergence * 100,
    )

    if divergence > PROOF_OF_RESERVES_DIVERGENCE_THRESHOLD:
        message = (
            f"*Maple Proof of Reserves Divergence Alert*\n"
            f"📊 PoR total collateral: {format_usd(por_total)}\n"
            f"📊 syrupGlobals collateral: {format_usd(syrup_collateral)}\n"
            f"📈 Divergence: {divergence:.2%} (threshold: {PROOF_OF_RESERVES_DIVERGENCE_THRESHOLD:.1%})\n"
            f"⚠️ Third-party attestation differs significantly from on-chain disclosure\n"
            f"🔗 [Proof of Reserves Dashboard](https://app.maple.finance/earn/details)"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))


def check_unknown_collateral_assets() -> None:
    """Alert on collateral assets disclosed by Maple that are not in the risk map.

    Uses `collateralDisclosure`, which returns asset symbols without USD values.
    This lets us detect new collateral types even when `poolCollaterals` is broken.
    """
    try:
        disclosed_assets = fetch_collateral_disclosure()
    except (requests.RequestException, ValueError) as e:
        _alert_maple_graphql_skip("collateral disclosure", e)
        return

    unknown_assets = sorted(asset for asset in disclosed_assets if asset not in ASSET_RISK_SCORES)
    if not unknown_assets:
        return

    logger.info("Unknown collateral assets disclosed by Maple: %s", ", ".join(unknown_assets))
    unknown_lines = [f"• {asset}" for asset in unknown_assets]
    message = (
        "*Maple Syrup Unknown Collateral Asset*\n"
        "New collateral assets detected that are not in the risk mapping:\n"
        + "\n".join(unknown_lines)
        + "\n\nPlease update the risk scores in `maple/collateral.py`"
    )
    send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))


def check_collateral_risk() -> None:
    """Check loan collateral risk and alert if weighted risk score exceeds threshold."""
    pools_data: list[dict] = []

    try:
        pools_data = fetch_pools_data()
    except (requests.RequestException, ValueError) as e:
        _alert_maple_graphql_skip("pools data", e)
        return

    # Log per-pool subgraph data
    for pool_data in pools_data:
        logger.info(
            "Subgraph %s — TVL: %s, Principal out: %s, Unrealized losses: %s, Accrued interest: %s",
            pool_data["name"],
            format_usd(pool_data["totalAssets"] / 1e6),
            format_usd(pool_data["principalOut"] / 1e6),
            format_usd(pool_data["unrealizedLosses"] / 1e6),
            format_usd(pool_data["accountedInterest"] / 1e6),
        )

    # Check combined collateralization ratio via syrupGlobals
    try:
        check_collateralization_ratio()
    except (requests.RequestException, ValueError) as e:
        _alert_maple_graphql_skip("collateralization ratio", e)

    # Cross-check Proof-of-Reserves aggregate against syrupGlobals
    check_proof_of_reserves()

    # Check unrealized losses vs pool size
    check_unrealized_losses(pools_data)

    # Detect new/unknown collateral assets (poolCollaterals values are currently
    # unavailable because Maple's resolver fails on unregistered native assets).
    check_unknown_collateral_assets()

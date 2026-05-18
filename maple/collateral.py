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
from utils.http import request_with_retry
from utils.logging import get_logger

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
    "XRP": 2,
    "USTB": 2,
    "LBTC": 2,
    "HYPE": 2,
    "jitoSOL": 3,
}

# Default risk score for unknown assets
DEFAULT_RISK_SCORE = 5

# Alert if weighted risk score exceeds this threshold
RISK_SCORE_THRESHOLD = 1.5

# Alert if collateralization ratio drops below this threshold
COLLATERALIZATION_RATIO_THRESHOLD = 1.4  # 140%

# Alert if unrealized losses exceed this % of pool total assets
UNREALIZED_LOSSES_THRESHOLD = 0.005  # 0.5%

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
    poolMeta {
      poolCollaterals {
        asset
        assetAmount
        assetDecimals
        assetValueUsd
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
    send_alert(
        Alert(
            AlertSeverity.MEDIUM,
            f"Maple GraphQL unavailable; skipping {check_name} check for this run.\nError: {error}",
            PROTOCOL,
        ),
        silent=True,
        plain_text=True,
    )


def fetch_collateral_data() -> tuple[list[dict], list[dict]]:
    """Fetch collateral and pool data for both syrupUSDC and syrupUSDT from Maple GraphQL API.

    Collateral is merged across both pools (same asset from different pools is combined).

    Returns:
        Tuple of (combined_collaterals, pools_data) where pools_data contains per-pool
        totalAssets, principalOut, unrealizedLosses, accountedInterest fields.

    Raises:
        ValueError: If the API response is malformed or pools not found.
        requests.RequestException: If the API request fails.
    """
    data = _post_maple_graphql(COLLATERAL_QUERY, "collateral")

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

    # Merge collateral across both pools — same asset from different pools gets combined
    collateral_by_asset: dict[str, dict] = {}
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

        for collateral in pool.get("poolMeta", {}).get("poolCollaterals", []):
            asset = collateral.get("asset")
            if not asset:
                logger.warning("Skipping collateral with missing asset: %s", collateral)
                continue
            usd_value = float(collateral.get("assetValueUsd") or "0")
            if asset in collateral_by_asset:
                existing = collateral_by_asset[asset]
                existing["assetValueUsd"] = str(float(existing.get("assetValueUsd") or "0") + usd_value)
            else:
                collateral_by_asset[asset] = {**collateral}

    return list(collateral_by_asset.values()), pools_data


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


def calculate_risk_score(collaterals: list[dict]) -> tuple[float, list[dict]]:
    """Calculate weighted average risk score from collateral data.

    Args:
        collaterals: List of collateral dicts from the Maple API.

    Returns:
        Tuple of (weighted_risk_score, active_collaterals) where active_collaterals
        contains only collaterals with non-zero USD value, enriched with risk info.
    """
    active_collaterals = []
    total_usd_value = 0.0
    weighted_risk_sum = 0.0

    for collateral in collaterals:
        usd_value = float(collateral.get("assetValueUsd") or "0")
        if usd_value <= 0:
            continue

        asset = collateral.get("asset")
        if not asset:
            continue
        risk_score = ASSET_RISK_SCORES.get(asset, DEFAULT_RISK_SCORE)

        # assetValueUsd is in 6 decimal, convert to dollars
        usd_value_dollars = usd_value / 1e6

        active_collaterals.append(
            {
                "asset": asset,
                "usd_value": usd_value_dollars,
                "risk_score": risk_score,
            }
        )

        total_usd_value += usd_value_dollars
        weighted_risk_sum += risk_score * usd_value_dollars

    if total_usd_value == 0:
        return 0.0, active_collaterals

    weighted_risk = weighted_risk_sum / total_usd_value
    return weighted_risk, active_collaterals


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


def check_collateral_risk() -> None:
    """Check loan collateral risk and alert if weighted risk score exceeds threshold."""
    try:
        collaterals, pools_data = fetch_collateral_data()
    except (requests.RequestException, ValueError) as e:
        _alert_maple_graphql_skip("collateral risk", e)
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

    # Check unrealized losses vs pool size
    check_unrealized_losses(pools_data)

    risk_score, active_collaterals = calculate_risk_score(collaterals)

    if not active_collaterals:
        logger.warning("No active collateral found across Syrup pools")
        return

    total_usd = sum(c["usd_value"] for c in active_collaterals)

    # Log collateral breakdown
    breakdown_lines = []
    for c in sorted(active_collaterals, key=lambda x: x["usd_value"], reverse=True):
        pct = c["usd_value"] / total_usd * 100 if total_usd > 0 else 0
        risk_label = {1: "Low", 2: "Medium", 3: "High"}.get(c["risk_score"], "Unknown")
        breakdown_lines.append(
            f"  {c['asset']}: {format_usd(c['usd_value'])} ({pct:.1f}%) — risk: {c['risk_score']} ({risk_label})"
        )

    logger.info(
        "Collateral risk score: %.2f (threshold: %.2f) | Total: %s\n%s",
        risk_score,
        RISK_SCORE_THRESHOLD,
        format_usd(total_usd),
        "\n".join(breakdown_lines),
    )

    if risk_score > RISK_SCORE_THRESHOLD:
        collateral_lines = []
        for c in sorted(active_collaterals, key=lambda x: x["usd_value"], reverse=True):
            pct = c["usd_value"] / total_usd * 100 if total_usd > 0 else 0
            risk_label = {1: "Low", 2: "Medium", 3: "High"}.get(c["risk_score"], "Unknown")
            collateral_lines.append(
                f"• {c['asset']}: {format_usd(c['usd_value'])} ({pct:.1f}%) — Risk {c['risk_score']} ({risk_label})"
            )

        message = (
            f"🚨 *Maple Syrup Collateral Risk Alert*\n"
            f"📊 Weighted risk score: {risk_score:.2f} (threshold: {RISK_SCORE_THRESHOLD:.1f})\n"
            f"💰 Total collateral: {format_usd(total_usd)}\n\n"
            f"*Collateral Breakdown:*\n" + "\n".join(collateral_lines) + "\n\n"
            "⚠️ High-risk collateral concentration detected\n"
            "🔗 [Pool Details](https://app.maple.finance/earn/details)"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))

    # Also alert on unknown assets (not in our risk mapping)
    unknown_assets = [c for c in active_collaterals if c["asset"] not in ASSET_RISK_SCORES]
    if unknown_assets:
        unknown_lines = [f"• {c['asset']}: {format_usd(c['usd_value'])}" for c in unknown_assets]
        message = (
            "⚠️ *Maple Syrup Unknown Collateral Asset*\n"
            "New collateral assets detected that are not in the risk mapping:\n"
            + "\n".join(unknown_lines)
            + "\n\nPlease update the risk scores in `maple/collateral.py`"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))

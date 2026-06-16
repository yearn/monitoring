import re
from datetime import datetime, timedelta

import requests

from utils.assets import (
    MAX_RISK_THRESHOLDS,
    SUPPLY_ASSETS_DICT,
    get_market_allocation_threshold,
)
from utils.formatting import format_usd
from utils.logger import get_logger

logger = get_logger("utils.gauntlet")


def get_gauntlet_build_id() -> str | None:
    """Get the latest build ID from Gauntlet dashboard"""
    try:
        # Request the main page first to get the latest build ID
        response = requests.get("https://dashboards.gauntlet.xyz/")
        response.raise_for_status()

        # Find the build ID in the HTML
        # It's usually in a script tag with id="__NEXT_DATA__"
        build_id = re.search(r'"buildId":"([^"]+)"', response.text)
        if build_id:
            return build_id.group(1)
    except Exception as e:
        logger.error("Error fetching Gauntlet build ID: %s", e)
    return None


def get_markets_for_protocol(protocol, max_retries=3) -> list[dict]:
    base_url = "https://dashboards.gauntlet.xyz/_next/data/{}/protocols/{}.json?protocolSlug={}"

    for attempt in range(max_retries):
        try:
            # Get the latest build ID
            build_id = get_gauntlet_build_id()
            if not build_id:
                # or return []
                raise Exception("Failed to get build ID")

            # Construct the URL with the latest build ID
            protocol_lower = protocol.lower()
            urlHealthMetrics = base_url.format(build_id, protocol_lower, protocol_lower)

            response = requests.get(urlHealthMetrics)
            response.raise_for_status()
            data = response.json()

            # If we get here, the request was successful
            # Continue with the existing logic
            markets = data["pageProps"]["protocolPage"]["markets"]
            return markets

        except requests.RequestException as e:
            if attempt == max_retries - 1:  # Last attempt
                logger.error("Error fetching Gauntlet metrics after %s attempts: %s", max_retries, e)
                return []
            logger.warning("Attempt %s failed, retrying...", attempt + 1)
            continue
        except ValueError as e:
            logger.error("Error parsing Gauntlet JSON response: %s", e)
            return []
        except Exception as e:
            logger.error("Unexpected error: %s", e)
            return []


def get_charts_for_protocol_market(protocol, market, max_retries=3):
    base_url = "https://dashboards.gauntlet.xyz/_next/data/{}/protocols/{}/markets/{}.json"

    for attempt in range(max_retries):
        try:
            # Get the latest build ID
            build_id = get_gauntlet_build_id()
            if not build_id:
                raise Exception("Failed to get build ID")

            protocol_lower = protocol.lower()
            urlCharts = base_url.format(build_id, protocol_lower, market)

            response = requests.get(urlCharts)
            response.raise_for_status()
            data = response.json()

            # this is used only for euler, if there are more protocols, we need to change this
            # response has ["scalarCards"] and ["charts"]
            return data["pageProps"]["chartSections"][0]

        except requests.RequestException as e:
            if attempt == max_retries - 1:  # Last attempt
                logger.error("Error fetching Gauntlet charts after %s attempts: %s", max_retries, e)
                return []
            logger.warning("Attempt %s failed, retrying...", attempt + 1)
            continue
        except ValueError as e:
            logger.error("Error parsing Gauntlet JSON response: %s", e)
            return []
        except Exception as e:
            logger.error("Unexpected error: %s", e)
            return []


def get_timestamp_before(hours: int):
    """Get timestamp from one hour ago in ISO format"""
    now = datetime.utcnow()
    one_hour_ago = now - timedelta(hours=hours)
    return one_hour_ago.strftime("%Y-%m-%dT%H:00:00.000Z")


def fetch_borrow_metrics_from_gauntlet(
    protocol: str, market_key: str, vault_risk_level: int, debt_supply_ratio: float
) -> list[str]:
    """Fetch and analyze market allocation metrics from Gauntlet.

    Args:
        protocol: Gauntlet protocol slug.
        market_key: Market identifier within the protocol.
        vault_risk_level: Risk level used for allocation and max-risk thresholds.
        debt_supply_ratio: Alert when total_borrow / total_supply exceeds this value.

    Returns:
        Alert messages if any thresholds are exceeded.
    """
    alerts = []
    charts = get_charts_for_protocol_market(protocol, market_key)
    if not charts:
        alerts.append(f"🚨 Market {market_key} charts cannot be fetched")
        return alerts

    cards = charts["scalarCards"]
    total_supply = cards[0]["value"]["amount"]
    total_borrow = cards[1]["value"]["amount"]
    last_updated = cards[0]["lastUpdated"]
    logger.info("Last updated: %s", last_updated)

    old_data_threshold = 36  # hours is the max time for a market to be updated
    if last_updated < get_timestamp_before(hours=old_data_threshold):
        alerts.append(
            f"🚨 Market {market_key} is not updated for {old_data_threshold} hours. Last updated at {last_updated}"
        )
        return alerts

    charts = charts["charts"]
    total_risk_level = 0.0
    logger.info("Market: %s", market_key)
    logger.info("Assigned Risk Level: %s", vault_risk_level)
    logger.info("Total supply: %s", format_usd(total_supply))
    logger.info("Total borrow: %s", format_usd(total_borrow))
    logger.debug("--------------------------------")
    logger.debug("Asset | Supply | Allocation")

    for chart in charts:
        if chart["key"] == "market_health_timeseries_asset_supply":
            # reverse the data so we get the biggest markets/vaults first
            for data in reversed(chart["data"]):
                asset = data["id"]
                supply = data["data"][-1]["y"]
                if supply == 0:
                    continue

                # Use dictionary lookup instead of list indexing
                asset_risk_tier = SUPPLY_ASSETS_DICT.get(asset, 5)  # Default to tier 5 if asset not found
                allocation_threshold = get_market_allocation_threshold(asset_risk_tier, vault_risk_level)

                # Calculate allocation ratio
                allocation_ratio = supply / total_supply if total_supply > 0 else 0

                # Check if allocation exceeds threshold
                if allocation_ratio > allocation_threshold:
                    alerts.append(
                        f"🔺 High allocation detected for {asset} in market {market_key}\n"
                        f"💹 Current allocation: {allocation_ratio:.1%}\n"
                        f"📊 Max acceptable allocation: {allocation_threshold:.1%}\n"
                        f"💰 Supply amount: {format_usd(supply)}"
                    )

                # Calculate risk contribution
                risk_multiplier = asset_risk_tier
                total_risk_level += risk_multiplier * allocation_ratio
                logger.debug("%s | %s | %s", asset, format_usd(supply), f"{allocation_ratio:.1%}")

    # Check total risk level against threshold for vault risk level
    if total_risk_level > MAX_RISK_THRESHOLDS[vault_risk_level]:
        alerts.append(
            f"🔺 High total risk level detected in market {market_key}:\n"
            f"📊 Total risk level: {total_risk_level:.1%}\n"
            f"📈 Max acceptable risk: {MAX_RISK_THRESHOLDS[vault_risk_level]:.1%}\n"
            f"💰 Total assets: {format_usd(total_supply)}"
        )

    if total_borrow / total_supply > debt_supply_ratio:
        alerts.append(
            f"🔺 High borrow/supply ratio detected in market {market_key}:\n"
            f"📊 Total borrow/supply ratio: {total_borrow / total_supply:.1%}\n"
            f"💰 Total assets: {format_usd(total_supply)}\n"
            f"💸 Total borrow: {format_usd(total_borrow)}\n"
        )

    logger.info("--------------------------------")
    logger.info("Total risk level: %s", f"{total_risk_level:.1%}")
    logger.info("================================")

    return alerts

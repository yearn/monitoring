from decimal import Decimal

import requests
from web3 import Web3

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.config import Config
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

# Constants
PROTOCOL = "infinifi"
logger = get_logger(PROTOCOL)

IUSD_ADDRESS = Web3.to_checksum_address("0x48f9e38f3070AD8945DFEae3FA70987722E3D89c")
IUSD_DECIMALS = 18

LIQUID_RESERVES_THRESHOLD = 12_000_000
BACKING_PER_IUSD_MIN = 0.999
MINT_THRESHOLD_PERCENT = Decimal(Config.get_env("IUSD_LARGE_MINT_THRESHOLD_PERCENT", "0.05"))
REDEMPTION_TO_LIQUID_RATIO_MAX = 0.8
FARM_RATIO_CHANGE_ALERT_THRESHOLD = 0.30
FARM_RATIO_ACTIVATION_ALERT_THRESHOLD = 0.03
FARM_RATIO_MIN_TVL_ALERT = 0.01
SAFE_FARM_IDENTIFIERS = [
    "sentora pyusd",
    "sgho",
    "maple",
    "cap",
    "aave horizon",
    "aave usdc",
    "syrupusdc",
    "autousd",
    "spark susdc",
    "fluid usdc",
    "euler sentora usdc",
]
JUNIOR_RISKY_COVERAGE_MIN = 0.5

# API Configuration
API_BASE_URL = "https://api.infinifi.xyz"
API_PROTOCOL_DATA = "/api/protocol/data"


def fetch_api_data():
    """Fetches data from the Infinifi API."""
    url = f"{API_BASE_URL}{API_PROTOCOL_DATA}"
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            logger.error("API Error %s for %s: %s", response.status_code, url, response.text[:200])
            return None
    except Exception as e:
        logger.error("API Request Failed for %s: %s", url, e)
        return None


def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def send_breach_alert_once(cache_key, alert_message, severity=AlertSeverity.HIGH):
    last_state = int(get_last_value_for_key_from_file(cache_filename, cache_key))
    if last_state == 0:
        send_alert(Alert(severity, alert_message, PROTOCOL))
        write_last_value_to_file(cache_filename, cache_key, 1)


def clear_breach_state(cache_key):
    last_state = int(get_last_value_for_key_from_file(cache_filename, cache_key))
    if last_state == 1:
        write_last_value_to_file(cache_filename, cache_key, 0)


def _format_iusd_units(raw_value: int) -> Decimal:
    return Decimal(raw_value) / (Decimal(10) ** IUSD_DECIMALS)


def main():
    client = ChainManager.get_client(Chain.MAINNET)
    erc20_abi = load_abi("common-abi/ERC20.json")

    iusd_contract = client.get_contract(IUSD_ADDRESS, erc20_abi)

    try:
        # --- 1. iUSD Supply ---
        # Batch call for totalSupply
        with client.batch_requests() as batch:
            batch.add(iusd_contract.functions.totalSupply())

            batch_results = client.execute_batch(batch)

        iusd_supply_raw = int(batch_results[0])

        iusd_supply = iusd_supply_raw / (10**IUSD_DECIMALS)

        # --- 2. Fetch API Data ---
        liquid_reserves = 0
        total_backing = 0
        pending_redemptions = 0
        reserve_ratio = 0
        illiquid_ratio = 0
        junior_tvl = 0
        farms = []
        # target_reserve_ratio = 0
        # target_illiquid_ratio = 0
        api_data = fetch_api_data()

        if not api_data:
            logger.warning("No API data received.")
        elif api_data.get("code") != "OK" or "data" not in api_data:
            logger.warning("API Data format unexpected. Code: %s", api_data.get("code"))
        else:
            stats = api_data["data"]["stats"]
            # Extract Liquid Reserves & TVL (using 'asset' key for USDC)
            asset_stats = stats.get("asset")

            if asset_stats:
                if "totalLiquidAssetNormalized" in asset_stats:
                    liquid_reserves = to_float(asset_stats["totalLiquidAssetNormalized"])

                if "totalTVLAssetNormalized" in asset_stats:
                    total_backing = to_float(asset_stats["totalTVLAssetNormalized"])

                if "pendingRedemptionsAssetNormalized" in asset_stats:
                    pending_redemptions = to_float(asset_stats["pendingRedemptionsAssetNormalized"])

            if total_backing > 0:
                reserve_ratio = liquid_reserves / total_backing
                illiquid_ratio = 1 - reserve_ratio

            receipt_stats = stats.get("receipt")
            if receipt_stats and "totalLockedNormalized" in receipt_stats:
                junior_tvl = to_float(receipt_stats["totalLockedNormalized"])

            farms = api_data["data"].get("farms", [])

            # params = api_data["data"].get("params", {})
            # target_reserve_ratio = to_float(params.get("reserveRatio"))
            # target_illiquid_ratio = to_float(params.get("illiquidTargetRatio"))

        logger.info("--- Infinifi Stats ---")
        logger.info("iUSD Supply:     $%s", f"{iusd_supply:,.2f}")

        if liquid_reserves > 0:
            logger.info("Liquid Reserves: $%s", f"{liquid_reserves:,.2f}")
        else:
            logger.info("Liquid Reserves: [Data Not Available]")

        if total_backing > 0:
            logger.info("Total Backing:   $%s", f"{total_backing:,.2f}")
            # Calculate strategy assets implied if not fetched directly
            implied_strategies = total_backing - liquid_reserves
            if implied_strategies > 0:
                logger.info("Implied Strategy Assets: $%s", f"{implied_strategies:,.2f}")
        else:
            logger.info("Total Backing:   [Data Not Available]")

        if total_backing > 0:
            logger.info("Liquid Ratio:    %.2f%%", reserve_ratio * 100)
            logger.info("Illiquid Ratio:  %.2f%%", illiquid_ratio * 100)

        if pending_redemptions > 0:
            logger.info("Pending Redeems: $%s", f"{pending_redemptions:,.2f}")

        if total_backing > 0 and iusd_supply > 0:
            backing_per_iusd = total_backing / iusd_supply
            logger.info("Backing / iUSD:  %.6f", backing_per_iusd)
        else:
            backing_per_iusd = 0

        # --- Alerts ---

        # Alert 0: Large iUSD mint by supply delta (no event scanning)
        cache_key_large_mints = f"{PROTOCOL}_large_mints_last_supply"
        last_supply_cached = int(get_last_value_for_key_from_file(cache_filename, cache_key_large_mints))
        if last_supply_cached > 0:
            delta_raw = iusd_supply_raw - last_supply_cached
            threshold_raw = int(last_supply_cached * MINT_THRESHOLD_PERCENT)
            if delta_raw >= threshold_raw:
                delta = _format_iusd_units(delta_raw)
                previous = _format_iusd_units(last_supply_cached)
                current = _format_iusd_units(iusd_supply_raw)
                threshold_tokens = _format_iusd_units(int(last_supply_cached * MINT_THRESHOLD_PERCENT))
                threshold_percent_display = MINT_THRESHOLD_PERCENT * Decimal(100)

                send_alert(
                    Alert(
                        AlertSeverity.LOW,
                        (
                            "*iUSD Large Mint Alert (Supply Delta)*\n\n"
                            f"Threshold: {threshold_percent_display:,.2f}% of totalSupply "
                            f"(~{threshold_tokens:,.2f} iUSD at previous supply)\n"
                            f"Supply increase: {delta:,.2f} iUSD\n"
                            f"Previous totalSupply: {previous:,.2f}\n"
                            f"Current totalSupply: {current:,.2f}\n\n"
                            "This monitor intentionally uses only totalSupply deltas (no event scanning)."
                        ),
                        PROTOCOL,
                    )
                )
        write_last_value_to_file(cache_filename, cache_key_large_mints, iusd_supply_raw)

        # Alert 1: Low Liquid Reserves
        if liquid_reserves > 0:
            cache_key_reserves = f"{PROTOCOL}_liquid_reserves"
            last_reserves = float(get_last_value_for_key_from_file(cache_filename, cache_key_reserves))

            if (
                last_reserves != 0
                and liquid_reserves < LIQUID_RESERVES_THRESHOLD
                and last_reserves >= LIQUID_RESERVES_THRESHOLD
            ):
                msg = f"📉 *Infinifi Liquid Reserves Alert*\n\nReserves dropped below ${LIQUID_RESERVES_THRESHOLD:,.0f}: ${liquid_reserves:,.2f}"
                send_alert(Alert(AlertSeverity.HIGH, msg, PROTOCOL))

            write_last_value_to_file(cache_filename, cache_key_reserves, liquid_reserves)

        # Alert 2 and Alert 3 intentionally disabled:
        # reserveRatio and illiquidTargetRatio have been persistently violated since inception,
        # so they are not useful for actionable alerting.
        #
        # # Alert 2: Reserve ratio below protocol target
        # if target_reserve_ratio > 0 and total_backing > 0:
        #     send_breach_alert_once(
        #         cache_key=f"{PROTOCOL}_reserve_ratio_breach",
        #         alert_message=(
        #             "⚠️ *Infinifi Reserve Ratio Breach*\n\n"
        #             f"Liquid ratio is {reserve_ratio:.2%}, below target {target_reserve_ratio:.2%}.\n"
        #             f"Liquid: ${liquid_reserves:,.2f}\nTotal backing: ${total_backing:,.2f}"
        #         ),
        #     )
        #
        # # Alert 3: Illiquid allocation above protocol target
        # if target_illiquid_ratio > 0 and total_backing > 0:
        #     send_breach_alert_once(
        #         cache_key=f"{PROTOCOL}_illiquid_ratio_breach",
        #         alert_message=(
        #             "⚠️ *Infinifi Illiquid Allocation Breach*\n\n"
        #             f"Illiquid ratio is {illiquid_ratio:.2%}, above target {target_illiquid_ratio:.2%}.\n"
        #             f"Illiquid assets: ${max(total_backing - liquid_reserves, 0):,.2f}"
        #         ),
        #     )

        # Alert 4: Backing per iUSD below expected level (loss signal)
        if backing_per_iusd > 0:
            cache_key_backing = f"{PROTOCOL}_backing_per_iusd_breach"
            if backing_per_iusd < BACKING_PER_IUSD_MIN:
                send_breach_alert_once(
                    cache_key=cache_key_backing,
                    severity=AlertSeverity.CRITICAL,
                    alert_message=(
                        "🚨 *Infinifi Backing Alert*\n\n"
                        f"Backing per iUSD is {backing_per_iusd:.6f}, below {BACKING_PER_IUSD_MIN:.3f}.\n"
                        f"TVL: ${total_backing:,.2f}\nSupply: ${iusd_supply:,.2f}"
                    ),
                )
            else:
                clear_breach_state(cache_key_backing)

        # Alert 5: Pending redemptions too high relative to liquid reserves
        if liquid_reserves > 0 and pending_redemptions >= 0:
            cache_key_redemption = f"{PROTOCOL}_redemption_pressure_breach"
            redemption_to_liquid = pending_redemptions / liquid_reserves
            if redemption_to_liquid > REDEMPTION_TO_LIQUID_RATIO_MAX:
                send_breach_alert_once(
                    cache_key=cache_key_redemption,
                    alert_message=(
                        "⚠️ *Infinifi Redemption Pressure Alert*\n\n"
                        f"Pending/Liquid ratio is {redemption_to_liquid:.2%}, above {REDEMPTION_TO_LIQUID_RATIO_MAX:.0%}.\n"
                        f"Pending redemptions: ${pending_redemptions:,.2f}\nLiquid reserves: ${liquid_reserves:,.2f}"
                    ),
                )
            else:
                clear_breach_state(cache_key_redemption)

        # Alert 6: Farm allocation ratio changed by more than 10% since last run.
        # ratio = farm assets / total TVL
        if total_backing > 0 and farms:
            moved_farms = []
            activated_farms = []

            for farm in farms:
                farm_assets = to_float(farm.get("assetsNormalized"))
                farm_ratio = farm_assets / total_backing if total_backing > 0 else 0
                farm_address = farm.get("address", "unknown")
                farm_label = farm.get("label", farm.get("name", farm_address))
                cache_key_farm_ratio = f"{PROTOCOL}_farm_ratio_{farm_address.lower()}"

                last_ratio = to_float(get_last_value_for_key_from_file(cache_filename, cache_key_farm_ratio))
                if last_ratio > 0:
                    ratio_change_pct = abs(farm_ratio - last_ratio) / last_ratio
                    # skip farm if ratio change is less than 1% of TVL
                    if (
                        ratio_change_pct > FARM_RATIO_CHANGE_ALERT_THRESHOLD
                        and max(last_ratio, farm_ratio) >= FARM_RATIO_MIN_TVL_ALERT
                    ):
                        moved_farms.append(
                            {
                                "label": farm_label,
                                "last_ratio": last_ratio,
                                "new_ratio": farm_ratio,
                                "change_pct": ratio_change_pct,
                            }
                        )
                        write_last_value_to_file(cache_filename, cache_key_farm_ratio, farm_ratio)
                else:
                    # Farm had no previous ratio (or previously zero). Alert if now materially active.
                    if farm_ratio > FARM_RATIO_ACTIVATION_ALERT_THRESHOLD:
                        activated_farms.append(
                            {
                                "label": farm_label,
                                "new_ratio": farm_ratio,
                            }
                        )
                        write_last_value_to_file(cache_filename, cache_key_farm_ratio, farm_ratio)

            if moved_farms:
                moved_farms.sort(key=lambda x: x["change_pct"], reverse=True)
                moved_lines = [
                    (f"- {f['label']}: {f['last_ratio']:.2%} -> {f['new_ratio']:.2%} ({f['change_pct']:.2%} change)")
                    for f in moved_farms
                ]
                more_count = len(moved_farms) - 10
                if more_count > 0:
                    moved_lines.append(f"- ...and {more_count} more farms")

                send_alert(
                    Alert(
                        AlertSeverity.MEDIUM,
                        "*Infinifi Farm Allocation Shift Alert*\n\n"
                        "Farm allocation ratio changed by more than 10% vs previous run:\n" + "\n".join(moved_lines),
                        PROTOCOL,
                    )
                )

            if activated_farms:
                activated_farms.sort(key=lambda x: x["new_ratio"], reverse=True)
                activated_lines = [f"- {f['label']}: {f['new_ratio']:.2%}" for f in activated_farms]
                more_count = len(activated_farms) - 10
                if more_count > 0:
                    activated_lines.append(f"- ...and {more_count} more farms")

                send_alert(
                    Alert(
                        AlertSeverity.LOW,
                        "*Infinifi Farm Activation Alert*\n\n"
                        "Farms previously at 0 ratio are now above 5% of TVL:\n" + "\n".join(activated_lines),
                        PROTOCOL,
                    )
                )

        # Alert 7: Junior TVL below risky farm exposure
        # Junior tranche (locked iUSD) should cover at least 80% of risky farms TVL.
        # Risky farms = all farms NOT in the safe farms whitelist.
        if junior_tvl > 0 and farms:

            def is_safe_farm(farm):
                name = farm.get("name", "").lower()
                label = farm.get("label", "").lower()
                return any(identifier in name or identifier in label for identifier in SAFE_FARM_IDENTIFIERS)

            risky_farms = [f for f in farms if not is_safe_farm(f)]
            risky_exposure = sum(to_float(f.get("assetsNormalized")) for f in risky_farms)

            if risky_exposure > 0:
                min_required = risky_exposure * JUNIOR_RISKY_COVERAGE_MIN
                logger.info("Junior TVL:      $%s", f"{junior_tvl:,.2f}")
                logger.info("Risky Exposure:  $%s", f"{risky_exposure:,.2f}")
                logger.info("Min Required:    $%s (%.0f%%)", f"{min_required:,.2f}", JUNIOR_RISKY_COVERAGE_MIN * 100)

                cache_key_junior_risky = f"{PROTOCOL}_junior_below_risky_breach"
                if junior_tvl < min_required:
                    risky_farms_detail = [
                        f"- {f.get('label', f.get('name', 'unknown'))}: ${to_float(f.get('assetsNormalized')):,.2f}"
                        for f in risky_farms
                        if to_float(f.get("assetsNormalized")) > 0
                    ]
                    coverage_pct = (junior_tvl / risky_exposure) * 100 if risky_exposure > 0 else 0
                    send_breach_alert_once(
                        cache_key=cache_key_junior_risky,
                        alert_message=(
                            "🚨 *Infinifi Junior TVL Below Risky Farm Exposure*\n\n"
                            f"Junior TVL (locked iUSD): ${junior_tvl:,.2f}\n"
                            f"Total risky farm exposure: ${risky_exposure:,.2f}\n"
                            f"Coverage: {coverage_pct:.1f}% (min required: {JUNIOR_RISKY_COVERAGE_MIN:.0%})\n"
                            f"Shortfall: ${min_required - junior_tvl:,.2f}\n\n"
                            f"Risky farms:\n" + "\n".join(risky_farms_detail)
                        ),
                    )
                else:
                    clear_breach_state(cache_key_junior_risky)

    except Exception as e:
        logger.error("Error: %s", e)
        send_alert(Alert(AlertSeverity.LOW, f"Infinifi monitoring failed: {e}", PROTOCOL))


if __name__ == "__main__":
    main()

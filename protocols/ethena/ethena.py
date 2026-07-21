from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.logger import get_logger
from utils.telegram import send_error_message
from utils.web3_wrapper import Chain, ChainManager

PROTOCOL = "ethena"
logger = get_logger(PROTOCOL)

# Ethena transparency API endpoints (usable from our VPS; were previously blocked for GitHub Actions IPs)
SUPPLY_URL = "https://app.ethena.fi/api/solvency/token-supply?symbol=USDe"
COLLATERAL_URL = "https://app.ethena.fi/api/positions/current/collateral?latest=true"
RESERVE_FUND_URL = "https://app.ethena.fi/api/solvency/reserve-fund"
LLAMARISK_URL = "https://api.llamarisk.com/protocols/ethena/overview/all/?format=json"

USDE_ADDRESS = "0x4c9EDD5852cd905f086C759E8383e09bff1E68B3"
SUSDE_ADDRESS = "0x9D39A5DE30e57443BfF2A8307A4256c8797A3497"

ABI_ERC20 = load_abi("common-abi/ERC20.json")

# Alert thresholds
COLLATERAL_RATIO_TRIGGER = 1.005  # must be overcollateralized by at least 0.5%

REQUEST_TIMEOUT = 15  # seconds

# Provider labels so every alert makes clear which data source triggered it.
# The two backing checks run independently against different providers.
ETHENA_SOURCE = "Ethena API"
LLAMARISK_SOURCE = "LlamaRisk"


@dataclass
class ChainMetrics:
    total_usde_supply: float
    total_usde_staked: float
    total_susde_supply: float
    usde_price: float
    susde_price: float
    timestamp: str


@dataclass
class LlamaRiskData:
    timestamp: str
    collateral_value: float
    chain_metrics: ChainMetrics
    reserve_fund: float


def fetch_json(url: str) -> dict | None:
    """Helper that fetches JSON with basic error handling."""
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.error("HTTP %s for %s\n%s", resp.status_code, url, resp.text)
            return None
        return resp.json()
    except Exception as e:
        logger.error("Failed to fetch %s: %s", url, e)
        return None


def _parse_timestamp(ts: str) -> datetime | None:
    """Parse various timestamp formats returned by Ethena & LlamaRisk APIs."""
    formats = [
        "%Y-%m-%d %H:%M:%S.%f UTC",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue

    # Fallback to fromisoformat after normalising Z→+00:00
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" UTC", ""))
    except Exception:
        return None


def is_stale_timestamp(ts: str, max_age_hours: int = 3) -> bool:
    """Return True if `ts` is older than `max_age_hours`. Un-parsable → considered stale."""
    dt = _parse_timestamp(ts)
    if dt is None:
        return True
    # _parse_timestamp returns naive datetimes, so compare against a naive UTC "now"
    # (datetime.utcnow() is deprecated).
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    return dt < now_utc - timedelta(hours=max_age_hours)


def get_usde_supply() -> float | None:
    """Return total circulating USDe supply in USD terms (raw token amount / 1e18)."""
    data = fetch_json(SUPPLY_URL)
    if not data:
        return None

    timestamp = data.get("timestamp")  # May be missing
    if timestamp and is_stale_timestamp(timestamp):
        logger.warning("Data from ethena is old: %s", timestamp)
        return None

    return float(data["supply"]) / 1e18


def get_total_collateral_usd() -> float | None:
    """Return USD value of all collateral backing USDe."""
    data = fetch_json(COLLATERAL_URL)
    if not data:
        return None

    return float(data["totalBackingAssetsInUsd"])


def get_reserve_fund() -> float | None:
    """Return the latest USD value of Ethena's reserve fund.

    The endpoint returns a full time series under ``queryIndex[0].yields`` as
    ``{timestamp, value}`` points; we take the most recent one and treat stale
    data (older than 3 hours) as unavailable.
    """
    data = fetch_json(RESERVE_FUND_URL)
    if not data:
        return None

    try:
        series = data["queryIndex"][0]["yields"]
        latest = series[-1]
    except (KeyError, IndexError, TypeError):
        logger.error("Unexpected reserve fund response shape: %s", data)
        return None

    timestamp = latest.get("timestamp")
    if timestamp and is_stale_timestamp(timestamp):
        logger.warning("Reserve fund data from ethena is old: %s", timestamp)
        return None

    return float(latest["value"])


def get_llamarisk_data() -> LlamaRiskData | None:
    """Return data from LlamaRisk API."""
    data = fetch_json(LLAMARISK_URL)
    if not data:
        return None

    collateral_metrics = data["collateral_metrics"]
    chain_metrics_raw = data["chain_metrics"]
    reserve_fund = data["reserve_fund_metrics"]

    timestamp_collateral = collateral_metrics["latest"]["timestamp"]
    timestamp_chain = chain_metrics_raw["latest"]["timestamp"]
    timestamp_reserve = reserve_fund["latest"]["timestamp"]

    hours_ago = 12
    if is_stale_timestamp(timestamp_collateral, hours_ago):
        send_alert(
            Alert(
                AlertSeverity.LOW,
                f"⚠️ Collateral data is older than {hours_ago} hours. Timestamp: {timestamp_collateral}",
                PROTOCOL,
            )
        )

    if is_stale_timestamp(timestamp_chain, hours_ago):
        # NOTE: don't send telegram message because there is a problem with the API
        logger.warning("Chain data is older than %s hours. Timestamp: %s", hours_ago, timestamp_chain)

    if is_stale_timestamp(timestamp_reserve, hours_ago):
        send_alert(
            Alert(
                AlertSeverity.LOW,
                f"⚠️ Reserve data is older than {hours_ago} hours. Timestamp: {timestamp_reserve}",
                PROTOCOL,
            )
        )

    # sum all collateral values
    collateral_metrics = collateral_metrics["latest"]["data"]["collateral"]
    collateral_sum = sum(item["usdAmount"] for item in collateral_metrics)

    chain_metrics_data = chain_metrics_raw["latest"]["data"]
    reserve_fund_val = float(reserve_fund["latest"]["data"]["value"])

    # Build ChainMetrics dataclass with safe conversions
    def _to_float(value):
        try:
            return float(value)
        except Exception:
            return 0.0

    cm = ChainMetrics(
        total_usde_supply=_to_float(chain_metrics_data.get("totalUsdeSupply", 0)) / 1e18,
        total_usde_staked=_to_float(chain_metrics_data.get("totalUsdeStaked", 0)) / 1e18,
        total_susde_supply=_to_float(chain_metrics_data.get("totalSusdeSupply", 0)) / 1e18,
        usde_price=_to_float(chain_metrics_data.get("usdePrice", 1)),
        susde_price=_to_float(chain_metrics_data.get("susdePrice", 1)),
        timestamp=timestamp_chain,
    )

    return LlamaRiskData(
        timestamp=timestamp_collateral,
        collateral_value=collateral_sum,
        chain_metrics=cm,
        reserve_fund=reserve_fund_val,
    )


def get_tokens_supply() -> tuple[float, float] | tuple[None, None]:
    client = ChainManager.get_client(Chain.MAINNET)

    try:
        usde = client.eth.contract(address=USDE_ADDRESS, abi=ABI_ERC20)
        susde = client.eth.contract(address=SUSDE_ADDRESS, abi=ABI_ERC20)
    except Exception as e:
        error_message = f"Error creating contract instances: {e}. Check ABI paths and contract addresses."
        logger.error("%s", error_message)
        return None, None  # Cannot proceed without contracts

    usde_supply = None
    susde_supply = None
    # --- Combined Blockchain Calls ---
    try:
        with client.batch_requests() as batch:
            batch.add(usde.functions.totalSupply())
            batch.add(susde.functions.totalSupply())

            responses = client.execute_batch(batch)

            if len(responses) == 2:
                usde_supply, susde_supply = responses
                logger.info("Raw Data - USDe Supply: %s, Susde Supply: %s", usde_supply, susde_supply)
            else:
                raise Exception(f"Batch Call: Expected 3 responses, got {len(responses)}")

    except Exception:
        send_error_message("Error during batch blockchain calls", PROTOCOL)
        return None, None  # Cannot proceed if batch fails

    return usde_supply, susde_supply


def llama_risk_check() -> None:
    """Independent USDe backing check using LlamaRisk's transparency data.

    Runs alongside (and independently of) ``ethena_backing_check`` so the two
    providers cross-check each other: if one API is wrong or stale, the other
    still reports. Every alert is prefixed with ``LLAMARISK_SOURCE`` so it is
    obvious which provider fired.

    Backing = collateral + reserve fund. Alerts CRITICAL when total backing no
    longer covers supply (ratio < 1) and HIGH when the buffer thins below
    COLLATERAL_RATIO_TRIGGER. Also validates LlamaRisk's supply figures against
    on-chain ``totalSupply()`` and warns (MEDIUM) if they diverge materially.
    """
    llama_risk = get_llamarisk_data()
    if llama_risk is None:
        send_error_message(f"⚠️ [{LLAMARISK_SOURCE}] Failed to fetch backing data", PROTOCOL)
        return

    supply = llama_risk.chain_metrics.total_usde_supply
    collateral = llama_risk.collateral_value
    reserve_fund = llama_risk.reserve_fund
    if supply == 0:
        send_error_message(f"⚠️ [{LLAMARISK_SOURCE}] Supply reported as 0; skipping backing check", PROTOCOL)
        return

    total_backing = collateral + reserve_fund
    ratio = total_backing / supply

    if ratio < 1:
        send_alert(
            Alert(
                AlertSeverity.CRITICAL,
                f"🚨 [{LLAMARISK_SOURCE}] USDe NOT FULLY BACKED!\n"
                f"Backing Assets: ${total_backing:,.2f} (collateral ${collateral:,.2f} + reserve ${reserve_fund:,.2f})\n"
                f"Total Supply: {supply:,.2f}\n"
                f"Backing Ratio: {ratio:.4f} ({ratio * 100 - 100:+.2f}%)\n"
                f"LlamaRisk timestamp: {llama_risk.timestamp}",
                PROTOCOL,
            )
        )
    elif ratio < COLLATERAL_RATIO_TRIGGER:
        send_alert(
            Alert(
                AlertSeverity.HIGH,
                f"🚨 [{LLAMARISK_SOURCE}] USDe backing buffer is thin!\n"
                f"Backing Assets: ${total_backing:,.2f} (collateral ${collateral:,.2f} + reserve ${reserve_fund:,.2f})\n"
                f"Total Supply: {supply:,.2f}\n"
                f"Backing Ratio: {ratio:.4f} ({ratio * 100 - 100:+.2f}%)\n"
                f"LlamaRisk timestamp: {llama_risk.timestamp}",
                PROTOCOL,
            )
        )

    logger.info(
        "[%s] backing: $%s (collateral $%s + reserve $%s) | supply: %s | ratio: %s | timestamp: %s",
        LLAMARISK_SOURCE,
        f"{total_backing:,.2f}",
        f"{collateral:,.2f}",
        f"{reserve_fund:,.2f}",
        f"{supply:,.2f}",
        f"{ratio:.4f}",
        llama_risk.timestamp,
    )

    # Cross-validate LlamaRisk supply figures against on-chain totalSupply().
    # NOTE: skip if LlamaRisk data is stale — it would be out of sync with chain state.
    # Use is_stale_timestamp (naive-UTC comparison) so this is correct on non-UTC hosts;
    # a plain datetime.now() would use local time and mark fresh data stale under DST.
    if is_stale_timestamp(llama_risk.chain_metrics.timestamp, max_age_hours=2):
        logger.warning("[%s] data is old, skipping on-chain validation: %s", LLAMARISK_SOURCE, llama_risk.timestamp)
        return

    usde_supply, susde_supply = get_tokens_supply()
    if usde_supply is None or susde_supply is None:
        return  # get_tokens_supply already reported the failure

    # LlamaRisk values are token amounts without decimals, so scale on-chain wei down.
    usde_supply /= 1e18
    susde_supply /= 1e18

    # NOTE: higher tolerance because on-chain and off-chain values are not perfectly in sync.
    value_diff_trigger = 0.005  # 0.5%
    error_messages = []
    if abs(usde_supply - supply) / supply > value_diff_trigger:
        error_messages.append(
            f"USDe supply differs on-chain vs LlamaRisk: {supply:,.2f} != {usde_supply:,.2f} "
            f"(diff: {abs(usde_supply - supply) / supply:.4%})"
        )

    susde_llama = llama_risk.chain_metrics.total_susde_supply
    if susde_llama and abs(susde_supply - susde_llama) / susde_supply > value_diff_trigger:
        error_messages.append(
            f"sUSDe supply differs on-chain vs LlamaRisk: {susde_llama:,.2f} != {susde_supply:,.2f} "
            f"(diff: {abs(susde_supply - susde_llama) / susde_supply:.4%})"
        )

    if error_messages:
        message = f"⚠️ [{LLAMARISK_SOURCE}] " + "\n".join(error_messages)
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))


def ethena_backing_check() -> None:
    """Check that USDe remains fully backed using Ethena's transparency API.

    This is the primary backing check. Ethena's transparency API (app.ethena.fi) is
    usable now that monitoring runs on our VPS — it was previously blocked for GitHub
    Actions IPs, which is why a Chaos Labs / Oracle Security PoR endpoint was used
    instead. That endpoint has since been decommissioned (returns 503), and Chainlink's
    USDe PoR is not published as a public on-chain feed, so we rely on Ethena's own
    transparency data.

    Backing = collateral + reserve fund. USDe targets ~1:1 collateral backing with a
    SEPARATE reserve fund as the buffer, so the collateral-only figure hovers right
    around 1.0 and dips fractionally below it in normal operation. Including the reserve
    fund (fetched from Ethena's own /solvency/reserve-fund endpoint) gives the true
    solvency ratio and lets us apply COLLATERAL_RATIO_TRIGGER without false-positiving.

    Alerts CRITICAL when total backing no longer covers supply (ratio < 1) and HIGH when
    the buffer thins below COLLATERAL_RATIO_TRIGGER.
    """
    supply = get_usde_supply()
    collateral = get_total_collateral_usd()
    reserve_fund = get_reserve_fund()
    if supply is None or collateral is None or reserve_fund is None or supply == 0:
        send_error_message("⚠️ ETHENA: Failed to fetch backing data from Ethena transparency API", PROTOCOL)
        return

    total_backing = collateral + reserve_fund
    backing_ratio = total_backing / supply
    if backing_ratio < 1:
        send_alert(
            Alert(
                AlertSeverity.CRITICAL,
                f"🚨 [{ETHENA_SOURCE}] USDe NOT FULLY BACKED!\n"
                f"Backing Assets: ${total_backing:,.2f} (collateral ${collateral:,.2f} + reserve ${reserve_fund:,.2f})\n"
                f"Total Supply: {supply:,.2f}\n"
                f"Backing Ratio: {backing_ratio:.4f} ({backing_ratio * 100 - 100:+.2f}%)",
                PROTOCOL,
            )
        )
    elif backing_ratio < COLLATERAL_RATIO_TRIGGER:
        send_alert(
            Alert(
                AlertSeverity.HIGH,
                f"🚨 [{ETHENA_SOURCE}] USDe backing buffer is thin!\n"
                f"Backing Assets: ${total_backing:,.2f} (collateral ${collateral:,.2f} + reserve ${reserve_fund:,.2f})\n"
                f"Total Supply: {supply:,.2f}\n"
                f"Backing Ratio: {backing_ratio:.4f} ({backing_ratio * 100 - 100:+.2f}%)",
                PROTOCOL,
            )
        )

    logger.info(
        "[%s] backing: $%s (collateral $%s + reserve $%s) | supply: %s | ratio: %s",
        ETHENA_SOURCE,
        f"{total_backing:,.2f}",
        f"{collateral:,.2f}",
        f"{reserve_fund:,.2f}",
        f"{supply:,.2f}",
        f"{backing_ratio:.4f}",
    )


def main() -> None:
    """Run both backing checks independently.

    Each provider is checked in isolation so a failure (or false positive) in one
    data source never suppresses the other. Any unhandled error in one check is
    contained and reported without aborting the other.
    """
    for check in (ethena_backing_check, llama_risk_check):
        try:
            check()
        except Exception:
            logger.exception("%s crashed", check.__name__)
            send_error_message(f"⚠️ {check.__name__} crashed unexpectedly", PROTOCOL)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)

from datetime import datetime, timedelta, timezone

import requests

from utils.alert import Alert, AlertSeverity, send_alert
from utils.logger import get_logger
from utils.telegram import send_error_message

PROTOCOL = "ethena"
logger = get_logger(PROTOCOL)

# Ethena transparency API endpoints (usable from our VPS; were previously blocked for GitHub Actions IPs)
SUPPLY_URL = "https://app.ethena.fi/api/solvency/token-supply?symbol=USDe"
COLLATERAL_URL = "https://app.ethena.fi/api/positions/current/collateral?latest=true"
RESERVE_FUND_URL = "https://app.ethena.fi/api/solvency/reserve-fund"

# Alert thresholds
COLLATERAL_RATIO_TRIGGER = 1.005  # must be overcollateralized by at least 0.5%

REQUEST_TIMEOUT = 15  # seconds

# Label alerts with the data source they came from.
ETHENA_SOURCE = "Ethena API"


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
    """Parse the timestamp formats returned by Ethena's transparency API."""
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
    """Return USD value of collateral backing USDe (Ethena's NET-backing figure).

    NOTE: ``COLLATERAL_URL`` carries ``?latest=true``, which returns Ethena's
    freshest aggregate ``totalBackingAssetsInUsd`` and an EMPTY breakdown array.
    That aggregate is a *net backing* number that tracks supply ~1:1 (ratio ≈
    1.00). The SAME endpoint without ``latest=true`` instead returns a detailed
    per-exchange breakdown whose total is *gross collateral* (~2.7% above supply,
    e.g. $4.14B vs $4.03B) but is a stale snapshot (items lag several hours). We
    use the fresh net figure and add the reserve fund as the buffer.
    """
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


def ethena_backing_check() -> None:
    """Check that USDe remains fully backed using Ethena's transparency API.

    Ethena's transparency API (app.ethena.fi) is usable now that monitoring runs on
    our VPS — it was previously blocked for GitHub Actions IPs, which is why a Chaos
    Labs / Oracle Security PoR endpoint was used instead. That endpoint has since been
    decommissioned (returns 503), and Chainlink's USDe PoR is not published as a public
    on-chain feed, so we rely on Ethena's own transparency data.

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


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(ethena_backing_check, PROTOCOL)

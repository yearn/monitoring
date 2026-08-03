"""Monitor CAP Stabledrop availability and stcUSD accounting invariants."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager

PROTOCOL = "cap"
CUSD = "0xcCcc62962d17b8914c62D74FfB843d73B2a3cccC"
STCUSD = "0x88887bE419578051FF9F4eb6C858A951921D8888"
STABLEDROP = "0x1D7D4485B4737FAb29180A7C143E72Cb4666357c"
CUSD_DECIMALS = 18
ONE_STCUSD = 10**18

CACHE_KEY_STABLEDROP_PAUSED = "CAP_STABLEDROP_PAUSED"
CACHE_KEY_STCUSD_BACKING_DEFICIT = "CAP_STCUSD_BACKING_DEFICIT"
CACHE_KEY_STCUSD_ASSETS_PER_SHARE = "CAP_STCUSD_ASSETS_PER_SHARE"

logger = get_logger("cap_status")


@dataclass(frozen=True)
class StcUsdState:
    """Current backing and exchange-rate state for stcUSD."""

    cusd_balance: int
    total_assets: int
    locked_profit: int
    assets_per_share: int


def _cache_flag(key: str) -> bool:
    """Return a cached boolean flag stored as zero or one."""
    return str(get_last_value_for_key_from_file(cache_filename, key)) == "1"


def _to_int(value: Any, label: str) -> int:
    """Convert an RPC response to int and fail with field context when absent."""
    if value is None:
        raise RuntimeError(f"CAP status RPC returned no value for {label}")
    return int(value)


def _format_cusd(raw_value: int) -> str:
    """Format an 18-decimal cUSD amount for alert messages."""
    value = Decimal(raw_value) / Decimal(10**CUSD_DECIMALS)
    return f"{value:,.6f}"


def check_stabledrop_paused(is_paused: bool) -> None:
    """Alert once when the CAP Stabledrop enters the paused state.

    Args:
        is_paused: Current value returned by the Stabledrop's ``paused()`` call.
    """
    previous_paused = _cache_flag(CACHE_KEY_STABLEDROP_PAUSED)
    logger.info("CAP Stabledrop paused: %s", is_paused)

    if is_paused and not previous_paused:
        message = (
            "*CAP Stabledrop PAUSED*\n"
            "Claims are disabled while the contract remains paused.\n"
            f"🔗 [Stabledrop](https://etherscan.io/address/{STABLEDROP})"
        )
        send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL))

    if is_paused != previous_paused:
        write_last_value_to_file(cache_filename, CACHE_KEY_STABLEDROP_PAUSED, int(is_paused))


def check_stcusd_backing(state: StcUsdState) -> None:
    """Alert once while stcUSD lacks cUSD for accounted assets and locked profit.

    Args:
        state: Current stcUSD accounting values read from Mainnet.
    """
    required_backing = state.total_assets + state.locked_profit
    has_deficit = state.cusd_balance < required_backing
    previous_deficit = _cache_flag(CACHE_KEY_STCUSD_BACKING_DEFICIT)
    logger.info(
        "stcUSD backing: balance=%s required=%s deficit=%s",
        state.cusd_balance,
        required_backing,
        has_deficit,
    )

    if has_deficit and not previous_deficit:
        shortfall = required_backing - state.cusd_balance
        message = (
            "*stcUSD BACKING DEFICIT*\n"
            "The stcUSD contract does not hold enough cUSD for totalAssets plus locked profit.\n"
            f"cUSD held: {_format_cusd(state.cusd_balance)}\n"
            f"Required: {_format_cusd(required_backing)}\n"
            f"Shortfall: {_format_cusd(shortfall)} cUSD\n"
            f"🔗 [stcUSD](https://etherscan.io/address/{STCUSD})"
        )
        send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL))

    if has_deficit != previous_deficit:
        write_last_value_to_file(cache_filename, CACHE_KEY_STCUSD_BACKING_DEFICIT, int(has_deficit))


def check_stcusd_assets_per_share(current_assets_per_share: int) -> None:
    """Alert when one stcUSD converts to fewer cUSD than on the previous run.

    Args:
        current_assets_per_share: Result of ``convertToAssets(1e18)``.
    """
    cached_value = get_last_value_for_key_from_file(cache_filename, CACHE_KEY_STCUSD_ASSETS_PER_SHARE)
    previous_assets_per_share = int(cached_value) if cached_value else 0
    logger.info(
        "stcUSD assets per share: current=%s previous=%s",
        current_assets_per_share,
        previous_assets_per_share,
    )

    if previous_assets_per_share > 0 and current_assets_per_share < previous_assets_per_share:
        decrease = previous_assets_per_share - current_assets_per_share
        message = (
            "*stcUSD ASSETS PER SHARE DECREASED*\n"
            f"Previous: {_format_cusd(previous_assets_per_share)} cUSD\n"
            f"Current: {_format_cusd(current_assets_per_share)} cUSD\n"
            f"Decrease: {_format_cusd(decrease)} cUSD per stcUSD\n"
            f"🔗 [stcUSD](https://etherscan.io/address/{STCUSD})"
        )
        send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL))

    if current_assets_per_share != previous_assets_per_share:
        write_last_value_to_file(
            cache_filename,
            CACHE_KEY_STCUSD_ASSETS_PER_SHARE,
            current_assets_per_share,
        )


def load_status(client: Any) -> tuple[bool, StcUsdState]:
    """Load CAP status values in one Mainnet RPC batch.

    Args:
        client: Mainnet Web3 client supporting batch requests.

    Returns:
        Stabledrop pause state and current stcUSD accounting state.
    """
    stabledrop = client.eth.contract(address=STABLEDROP, abi=load_abi("protocols/cap/abi/Stabledrop.json"))
    cusd = client.eth.contract(address=CUSD, abi=load_abi("protocols/cap/abi/CToken.json"))
    stcusd = client.eth.contract(address=STCUSD, abi=load_abi("protocols/cap/abi/StakedCap.json"))

    with client.batch_requests() as batch:
        batch.add(stabledrop.functions.paused())
        batch.add(cusd.functions.balanceOf(STCUSD))
        batch.add(stcusd.functions.totalAssets())
        batch.add(stcusd.functions.lockedProfit())
        batch.add(stcusd.functions.convertToAssets(ONE_STCUSD))
        responses = batch.execute()

    is_paused = responses[0]
    if not isinstance(is_paused, bool):
        raise RuntimeError("CAP status RPC returned an invalid value for Stabledrop paused()")

    state = StcUsdState(
        cusd_balance=_to_int(responses[1], "stcUSD cUSD balance"),
        total_assets=_to_int(responses[2], "stcUSD totalAssets"),
        locked_profit=_to_int(responses[3], "stcUSD lockedProfit"),
        assets_per_share=_to_int(responses[4], "stcUSD convertToAssets"),
    )
    return is_paused, state


def main() -> None:
    """Fetch and check CAP status on Mainnet."""
    client = ChainManager.get_client(Chain.MAINNET)
    is_paused, stcusd_state = load_status(client)
    check_stabledrop_paused(is_paused)
    check_stcusd_backing(stcusd_state)
    check_stcusd_assets_per_share(stcusd_state.assets_per_share)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)

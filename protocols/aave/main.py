"""
Aave protocol monitoring script for tracking utilization rates of assets.

This module tracks utilization rates across multiple chains and sends alerts
when thresholds are exceeded.
"""

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import (
    HOURLY_CACHE_STALE_AFTER_SECONDS,
    cache_filename,
    get_fresh_last_value_for_key_from_file,
    write_last_value_with_timestamp_to_file,
)
from utils.chains import Chain
from utils.logger import get_logger
from utils.telegram import send_error_message
from utils.web3_wrapper import ChainManager

PROTOCOL = "aave"
logger = get_logger(PROTOCOL)

ABI_ATOKEN = load_abi("protocols/aave/abi/AToken.json")

# Map addresses and symbols by chain
ADDRESSES_BY_CHAIN = {
    # aToken, underlying, symbol
    Chain.MAINNET: [
        (
            "0x4d5F47FA6A74757f35C14fD3a6Ef8E3C9BC514E8",
            "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "WETH",
        ),
        (
            "0x23878914EFE38d27C4D67Ab83ed1b93A74D4086a",
            "0xdAC17F958D2ee523a2206206994597C13D831ec7",
            "USDT",
        ),
        (
            "0x98C23E9d8f34FEFb1B7BD6a91B7FF122F4e16F5c",
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "USDC",
        ),
        (
            "0x018008bfb33d285247A21d44E50697654f754e63",
            "0x6B175474E89094C44Da98b954EedeAC495271d0F",
            "DAI",
        ),
        (
            "0xb82fa9f31612989525992FCfBB09AB22Eff5c85A",
            "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E",
            "crvUSD",
        ),
        (
            "0x32a6268f9Ba3642Dda7892aDd74f1D34469A4259",
            "0xdC035D45d973E3EC169d2276DDab16f1e407384F",
            "USDS",
        ),
        (
            "0xBdfa7b7893081B35Fb54027489e2Bc7A38275129",
            "0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee",
            "weETH",
        ),
        (
            "0x5c647cE0Ae10658ec44FA4E11A51c96e94efd1Dd",
            "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
            "cbBTC",
        ),
        (
            "0x5Ee5bf7ae06D1Be5997A1A72006FE6C607eC6DE8",
            "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
            "WBTC",
        ),
        (
            "0x10Ac93971cdb1F5c778144084242374473c350Da",
            "0x18084fbA666a33d37592fA2633fD49a74DD93a88",
            "tBTC",
        ),
        (
            "0x65906988ADEe75306021C417a1A3458040239602",
            "0x8236a87084f8B84306f72007F36F2618A5634494",
            "LBTC",
        ),
    ],
}

THRESHOLD_UR = 0.99
# Alert only after this many consecutive runs above THRESHOLD_UR to filter out short spikes
MIN_CONSECUTIVE_HIGH_UR = 2


def high_ur_streak_key(chain_name: str, token_name: str) -> str:
    """Build the cache key holding the consecutive high-utilization count for a market.

    Args:
        chain_name: Name of the chain.
        token_name: Symbol of the market asset.

    Returns:
        Cache key string.
    """
    return f"{PROTOCOL}_high_ur_streak_{chain_name}_{token_name}"


def update_high_ur_streak(chain_name: str, token_name: str, ur: float) -> int:
    """Update and return the number of consecutive runs with utilization above threshold.

    The streak resets when utilization drops to or below the threshold, or when the
    previous observation is stale (e.g. missed runs).

    Args:
        chain_name: Name of the chain.
        token_name: Symbol of the market asset.
        ur: Current utilization rate.

    Returns:
        Current streak length, 0 if utilization is not above threshold.
    """
    key = high_ur_streak_key(chain_name, token_name)
    if ur > THRESHOLD_UR:
        previous = int(get_fresh_last_value_for_key_from_file(cache_filename, key, HOURLY_CACHE_STALE_AFTER_SECONDS))
        streak = previous + 1
    else:
        streak = 0
    write_last_value_with_timestamp_to_file(cache_filename, key, streak)
    return streak


def print_stuff(chain_name: str, token_name: str, ur: float) -> None:
    logger.debug(f"Chain: {chain_name}, Token: {token_name}, UR: {ur}")
    streak = update_high_ur_streak(chain_name, token_name, ur)
    if streak >= MIN_CONSECUTIVE_HIGH_UR:
        message = (
            f"**BEEP BOP**\n💎 Market asset: {token_name}\n📊 Utilization rate: {ur:.2%}\n🌐 Chain: {chain_name}\n"
            f"⏱️ Above {THRESHOLD_UR:.0%} for {streak} consecutive checks"
        )
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))
    elif streak > 0:
        logger.info(
            "%s on %s above threshold (%.2f%%), streak %d - not alerting yet", token_name, chain_name, ur * 100, streak
        )


def process_assets(chain: Chain) -> None:
    client = ChainManager.get_client(chain)
    addresses = ADDRESSES_BY_CHAIN[chain]
    block_number = int(client.eth.block_number)

    # Prepare all contracts and batch calls
    contracts = []
    with client.batch_requests() as batch:
        for atoken_address, underlying_token_address, token_symbol in addresses:
            atoken = client.eth.contract(address=atoken_address, abi=ABI_ATOKEN)
            underlying_token = client.eth.contract(address=underlying_token_address, abi=ABI_ATOKEN)
            contracts.append((atoken, underlying_token))

            batch.add(atoken.functions.totalSupply().call(block_identifier=block_number))
            batch.add(underlying_token.functions.balanceOf(atoken_address).call(block_identifier=block_number))

        responses = client.execute_batch(batch)
        num_pairs = len(addresses)
        expected_responses = num_pairs * 2  # Now only 2 calls per token pair
        if len(responses) != expected_responses:
            raise ValueError(f"Expected {expected_responses} responses from batch, got: {len(responses)}")

    # Process results
    for i, (_, _, token_symbol) in enumerate(addresses):
        ts = responses[i * 2]  # totalSupply
        av = responses[i * 2 + 1]  # balanceOf

        debt = ts - av
        ur = debt / ts if ts != 0 else 0

        print_stuff(chain.name, token_symbol, ur)


def main() -> None:
    for chain in [Chain.MAINNET]:
        logger.info("Processing %s assets...", chain.name)
        try:
            process_assets(chain)
        except Exception as e:
            logger.error("Error processing %s: %s", chain.name, e)
            send_error_message(f"Error processing Aave assets on {chain.name}: {e}", PROTOCOL)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)

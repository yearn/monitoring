"""
Aave protocol monitoring script for tracking utilization rates of assets.

This module tracks utilization rates across multiple chains and sends alerts
when thresholds are exceeded.
"""

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.chains import Chain
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

PROTOCOL = "aave"
logger = get_logger(PROTOCOL)

ABI_ATOKEN = load_abi("aave/abi/AToken.json")

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
    ],
}

THRESHOLD_UR = 0.99


def print_stuff(chain_name: str, token_name: str, ur: float) -> None:
    if ur > THRESHOLD_UR:
        message = f"**BEEP BOP**\n💎 Market asset: {token_name}\n📊 Utilization rate: {ur:.2%}\n🌐 Chain: {chain_name}"
        send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))


def process_assets(chain: Chain) -> None:
    client = ChainManager.get_client(chain)
    addresses = ADDRESSES_BY_CHAIN[chain]

    # Prepare all contracts and batch calls
    contracts = []
    with client.batch_requests() as batch:
        for atoken_address, underlying_token_address, token_symbol in addresses:
            atoken = client.eth.contract(address=atoken_address, abi=ABI_ATOKEN)
            underlying_token = client.eth.contract(address=underlying_token_address, abi=ABI_ATOKEN)
            contracts.append((atoken, underlying_token))

            batch.add(atoken.functions.totalSupply())
            batch.add(underlying_token.functions.balanceOf(atoken_address))

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


if __name__ == "__main__":
    main()

"""
SparkLend monitoring script for tracking utilization rates of assets.

SparkLend is an Aave v3 fork, so utilization is computed the same way as in
protocols/aave/main.py: debt = aToken totalSupply - available underlying.
"""

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.chains import Chain
from utils.logger import get_logger
from utils.telegram import send_error_message
from utils.web3_wrapper import ChainManager

PROTOCOL = "spark"
logger = get_logger(PROTOCOL)

ABI_ATOKEN = load_abi("protocols/aave/abi/AToken.json")

# Active, non-frozen SparkLend reserves (Pool 0xC13e21B648A5Ee794902342038FF3aDAB66BE987).
ADDRESSES_BY_CHAIN = {
    # spToken, underlying, symbol
    Chain.MAINNET: [
        (
            "0x4DEDf26112B3Ec8eC46e7E31EA5e123490B05B8B",
            "0x6B175474E89094C44Da98b954EedeAC495271d0F",
            "DAI",
        ),
        (
            "0x78f897F0fE2d3B5690EbAe7f19862DEacedF10a7",
            "0x83F20F44975D03b1b09e64809B757c47f942BEeA",
            "sDAI",
        ),
        (
            "0x377C3bd93f2a2984E1E7bE6A5C22c525eD4A4815",
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "USDC",
        ),
        (
            "0x59cD1C87501baa753d0B5B5Ab5D8416A45cD71DB",
            "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "WETH",
        ),
        (
            "0x12B54025C112Aa61fAce2CDB7118740875A566E9",
            "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0",
            "wstETH",
        ),
        (
            "0x4197ba364AE6698015AE5c1468f54087602715b2",
            "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
            "WBTC",
        ),
        (
            "0xe7dF13b8e3d6740fe17CBE928C7334243d86c92f",
            "0xdAC17F958D2ee523a2206206994597C13D831ec7",
            "USDT",
        ),
        (
            "0x3CFd5C0D4acAA8Faee335842e4f31159fc76B008",
            "0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee",
            "weETH",
        ),
        (
            "0xb3973D459df38ae57797811F2A1fd061DA1BC123",
            "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
            "cbBTC",
        ),
        (
            "0x6715bc100A183cc65502F05845b589c1919ca3d3",
            "0xa3931d71877C0E7a3148CB7Eb4463524FEc27fbD",
            "sUSDS",
        ),
        (
            "0xC02aB1A5eaA8d1B114EF786D9bde108cD4364359",
            "0xdC035D45d973E3EC169d2276DDab16f1e407384F",
            "USDS",
        ),
        (
            "0xa9d4EcEBd48C282a70CfD3c469d6C8F178a5738E",
            "0x8236a87084f8B84306f72007F36F2618A5634494",
            "LBTC",
        ),
        (
            "0x779224df1c756b4EDD899854F32a53E8c2B2ce5d",
            "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8",
            "PYUSD",
        ),
    ],
}

THRESHOLD_UR = 0.99


def print_stuff(chain_name: str, token_name: str, ur: float) -> None:
    logger.debug(f"Chain: {chain_name}, Token: {token_name}, UR: {ur}")
    if ur > THRESHOLD_UR:
        message = f"**BEEP BOP**\n💎 Market asset: {token_name}\n📊 Utilization rate: {ur:.2%}\n🌐 Chain: {chain_name}"
        send_alert(Alert(AlertSeverity.MEDIUM, message, PROTOCOL))


def process_assets(chain: Chain) -> None:
    client = ChainManager.get_client(chain)
    addresses = ADDRESSES_BY_CHAIN[chain]

    # Prepare all contracts and batch calls
    with client.batch_requests() as batch:
        for sptoken_address, underlying_token_address, _ in addresses:
            sptoken = client.eth.contract(address=sptoken_address, abi=ABI_ATOKEN)
            underlying_token = client.eth.contract(address=underlying_token_address, abi=ABI_ATOKEN)

            batch.add(sptoken.functions.totalSupply())
            batch.add(underlying_token.functions.balanceOf(sptoken_address))

        responses = client.execute_batch(batch)
        expected_responses = len(addresses) * 2
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
            send_error_message(f"Error processing SparkLend assets on {chain.name}: {e}", PROTOCOL)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)

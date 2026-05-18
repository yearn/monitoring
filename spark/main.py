import json

from utils.alert import Alert, AlertSeverity, send_alert
from utils.chains import Chain
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

PROTOCOL = "spark"
logger = get_logger(PROTOCOL)

with open("aave/abi/AToken.json") as f:
    abi_data = json.load(f)
    if isinstance(abi_data, dict):
        abi_atoken = abi_data["result"]
    elif isinstance(abi_data, list):
        abi_atoken = abi_data

mainnet_addresses = [
    [
        "0x78f897F0fE2d3B5690EbAe7f19862DEacedF10a7",
        "0x83F20F44975D03b1b09e64809B757c47f942BEeA",
    ],  # asdai, sdai
    [
        "0x377C3bd93f2a2984E1E7bE6A5C22c525eD4A4815",
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    ],  # ausdc, usdc
    [
        "0x4197ba364AE6698015AE5c1468f54087602715b2",
        "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    ],  # awbtc, wbtc
    [
        "0x59cD1C87501baa753d0B5B5Ab5D8416A45cD71DB",
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    ],  # aweth, weth
    [
        "0x12B54025C112Aa61fAce2CDB7118740875A566E9",
        "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0",
    ],  # awsteth, wsteth
    [
        "0x4DEDf26112B3Ec8eC46e7E31EA5e123490B05B8B",
        "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    ],  # adai, dai
    [
        "0xe7dF13b8e3d6740fe17CBE928C7334243d86c92f",
        "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    ],  # ausdt, usdt
    [
        "0x9985dF20D7e9103ECBCeb16a84956434B6f06ae8",
        "0xae78736Cd615f374D3085123A210448E74Fc6393",
    ],  # areth, reth
    [
        "0xb3973D459df38ae57797811F2A1fd061DA1BC123",
        "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
    ],  # spcwbtc, cbwbtc
    [
        "0x3CFd5C0D4acAA8Faee335842e4f31159fc76B008",
        "0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee",
    ],  # spweeth, weeth
]

# TODO: Add different threshold UR's for each asset
THRESHOLD_UR = 0.99


def print_stuff(chain_name, token_name, ur):
    logger.info("Chain: %s, Token: %s, UR: %s", chain_name, token_name, ur)
    if ur > THRESHOLD_UR:
        message = f"**BEEP BOP**\n💎 Market asset: {token_name}\n📊 Utilization rate: {ur:.2%}\n🌐 Chain: {chain_name}"
        send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))


# Function to process assets for a specific network
def process_assets(chain, addresses):
    # Get Web3 client using ChainManager
    client = ChainManager.get_client(chain)

    # Prepare all contracts and batch calls
    with client.batch_requests() as batch:
        for atoken_address, underlying_token_address in addresses:
            atoken = client.eth.contract(address=atoken_address, abi=abi_atoken)
            underlying_token = client.eth.contract(address=underlying_token_address, abi=abi_atoken)

            # Add all calls to the batch
            batch.add(atoken.functions.totalSupply())
            batch.add(underlying_token.functions.balanceOf(atoken_address))
            batch.add(underlying_token.functions.symbol())

        # Execute all calls at once
        responses = batch.execute()
        expected_responses = len(addresses) * 3
        if len(responses) != expected_responses:
            raise ValueError(f"Expected {expected_responses} responses from batch, got: {len(responses)}")

    # Process results
    expected_responses = len(addresses) * 3
    if len(responses) != expected_responses:
        raise ValueError(f"Expected {expected_responses} responses, got: {len(responses)}")

    for i in range(0, len(responses), 3):
        ts = responses[i]  # totalSupply
        av = responses[i + 1]  # balanceOf
        token_name = responses[i + 2]  # symbol

        if None in (ts, av, token_name):
            continue

        # Calculate debt and utilization rate
        debt = ts - av
        ur = debt / ts if ts != 0 else 0

        print_stuff(chain.name, token_name, ur)


# Main function
def main():
    logger.info("Processing Mainnet assets...")
    process_assets(Chain.MAINNET, mainnet_addresses)


# Run the main function
if __name__ == "__main__":
    main()

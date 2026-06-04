from utils.abi import load_abi
from utils.chains import Chain
from utils.logging import get_logger
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager

PROTOCOL = "pegs"
logger = get_logger("lrt-pegs.fluid")

# Fluid DEX Resolver
# https://github.com/Instadapp/fluid-contracts-public/blob/main/deployments/deployments.md
FLUID_DEX_RESOLVER = "0x05Bd8269A20C472b148246De20E6852091BF16Ff"
# Load FLUID ABI
ABI_FLUID_POOL = load_abi("protocols/lrt-pegs/abi/Fluid_DexResolver.json")
# Collateral Reserves Index
COLLATERAL_RESERVES_INDEX = 5
MIN_ASSET_BALANCE = 1e18
THRESHOLD_RATIO = 90.0


# Pool configurations
POOL_CONFIGS = [
    (
        "weETH / ETH FLUID Pool",
        "0x86f874212335Af27C41cDb855C2255543d1499cE",
        0,
        1,
        THRESHOLD_RATIO,
    ),
]


def process_pools(chain: Chain = Chain.MAINNET):
    client = ChainManager.get_client(chain)
    resolver = client.eth.contract(address=FLUID_DEX_RESOLVER, abi=ABI_FLUID_POOL)

    poolsArray = []

    for _, pool_address, _, _, _ in POOL_CONFIGS:
        poolsArray.append(pool_address)

    responses = resolver.functions.getPoolsReserves(poolsArray).call()

    if len(responses) != len(POOL_CONFIGS):
        raise ValueError(f"Expected {len(POOL_CONFIGS)} responses from batch, got: {len(responses)}")

    # Process results
    for (
        pool_name,
        pool_address,
        idx_lrt,
        idx_other_token,
        peg_threshold,
    ), pool_reserves in zip(POOL_CONFIGS, responses):
        assert pool_address == pool_reserves[0], f"Expected {pool_address} but got {pool_reserves[0]}"
        lrt_balance = int(pool_reserves[COLLATERAL_RESERVES_INDEX][idx_lrt])
        other_token_balance = int(pool_reserves[COLLATERAL_RESERVES_INDEX][idx_other_token])
        if lrt_balance < MIN_ASSET_BALANCE or other_token_balance < MIN_ASSET_BALANCE:
            send_telegram_message(
                f"🚨 Fluid Alert! {pool_name} has less than {MIN_ASSET_BALANCE} balance",
                PROTOCOL,
            )

        percentage = (lrt_balance / (lrt_balance + other_token_balance)) * 100
        logger.info("%s ratio is %s%%", pool_name, f"{percentage:.2f}")
        if percentage > peg_threshold:
            message = f"🚨 Fluid Alert! {pool_name} ratio is {percentage:.2f}%"
            send_telegram_message(message, PROTOCOL)


def main():
    logger.info("Checking Fluid pools...")
    process_pools()


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)

from utils.abi import load_abi
from utils.chains import Chain
from utils.logging import get_logger
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager

PROTOCOL = "pegs"
logger = get_logger("lrt-pegs.uniswap")
PEG_THRESHOLD = 80  # Same threshold as other scripts

# Load ABIs
ABI_UNISWAP_V3 = load_abi("protocols/lrt-pegs/abi/UniswapV3.json")
ABI_ERC20 = load_abi("common-abi/ERC20.json")

# Pool configurations
# name, pool address, lrt_is_token0, peg threshold
POOL_CONFIGS = [
    (
        "weETH-WETH Uniswap V3 Pool",
        "0x202A6012894Ae5c288eA824cbc8A9bfb26A49b93",
        True,
        82.0,
    ),
]


def get_token_balances(client, pool_address, token0_address, token1_address):
    """Get actual token balances held by the pool"""
    token0_contract = client.eth.contract(address=token0_address, abi=ABI_ERC20)
    token1_contract = client.eth.contract(address=token1_address, abi=ABI_ERC20)

    balance0 = token0_contract.functions.balanceOf(pool_address).call()
    balance1 = token1_contract.functions.balanceOf(pool_address).call()

    return balance0, balance1


def process_pools(chain: Chain = Chain.MAINNET):
    client = ChainManager.get_client(chain)
    contracts = []

    # Prepare batch calls to get token addresses and balances
    with client.batch_requests() as batch:
        for _, pool_address, _, _ in POOL_CONFIGS:
            pool = client.eth.contract(address=pool_address, abi=ABI_UNISWAP_V3)
            contracts.append(pool)

            # Add batch calls for token addresses
            batch.add(pool.functions.token0())
            batch.add(pool.functions.token1())

        responses = client.execute_batch(batch)
        if len(responses) != len(POOL_CONFIGS) * 2:
            raise ValueError(f"Expected {len(POOL_CONFIGS) * 2} responses from batch, got: {len(responses)}")

    # Get token balances for each pool
    pool_balances = []
    for i, (pool_name, pool_address, _, _) in enumerate(POOL_CONFIGS):
        token0_address = responses[i * 2]
        token1_address = responses[i * 2 + 1]
        balance0, balance1 = get_token_balances(client, pool_address, token0_address, token1_address)
        pool_balances.append((balance0, balance1))

    # Process results
    for i, (pool_name, _, lrt_is_token0, peg_threshold) in enumerate(POOL_CONFIGS):
        balance0, balance1 = pool_balances[i]
        total = balance0 + balance1

        if lrt_is_token0:
            percentage = (balance0 / total) * 100
        else:
            percentage = (balance1 / total) * 100

        logger.info("%s LRT ratio is %s%%", pool_name, f"{percentage:.2f}")

        if percentage > peg_threshold:
            message = f"🚨 Uniswap V3 Alert! {pool_name} LRT ratio is {percentage:.2f}%"
            send_telegram_message(message, PROTOCOL, True)


def main():
    logger.info("Checking Uniswap V3 pools...")
    process_pools()


if __name__ == "__main__":
    main()

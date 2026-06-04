from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.chains import Chain
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

CHANNEL = "pegs"
logger = get_logger("lrt-pegs.curve")

# Load Balancer Vault ABI
ABI_CURVE_POOL = load_abi("protocols/lrt-pegs/abi/CurvePool.json")
THRESHOLD_RATIO = 90.0

# Pool configurations
POOL_CONFIGS = [
    # name, pool address, index of lrt, index of other asset, peg threshold, protocol
    ("ETH+/WETH Curve Pool", "0x2c683fAd51da2cd17793219CC86439C1875c353e", 0, 1, THRESHOLD_RATIO, "ethplus"),
    ("OETH/ETH Curve Pool", "0xcc7d5785AD5755B6164e21495E07aDb0Ff11C2A8", 0, 1, THRESHOLD_RATIO, "origin"),
    # NOTE: bool is unbalanced, whole liquidity is moved to univ3: https://app.uniswap.org/explore/pools/ethereum/0x202a6012894ae5c288ea824cbc8a9bfb26a49b93
    ("weETH-WETH Curve Pool", "0xDB74dfDD3BB46bE8Ce6C33dC9D82777BCFc3dEd5", 1, 0, THRESHOLD_RATIO, "weeth"),
]


def process_pools(chain: Chain = Chain.MAINNET):
    client = ChainManager.get_client(chain)
    contracts = []

    # Prepare batch calls
    with client.batch_requests() as batch:
        for _, pool_address, _, _, _, _ in POOL_CONFIGS:
            pool = client.eth.contract(address=pool_address, abi=ABI_CURVE_POOL)
            contracts.append(pool)

            batch.add(pool.functions.get_balances())

        responses = client.execute_batch(batch)
        if len(responses) != len(POOL_CONFIGS):
            raise ValueError(f"Expected {len(POOL_CONFIGS)} responses from batch, got: {len(responses)}")

    # Process results
    for (pool_name, _, idx_lrt, idx_other_token, peg_threshold, protocol), balances in zip(POOL_CONFIGS, responses):
        percentage = (balances[idx_lrt] / (balances[idx_lrt] + balances[idx_other_token])) * 100
        logger.info("%s ratio is %s%%", pool_name, f"{percentage:.2f}")
        if percentage > peg_threshold:
            message = f"🚨 Curve Alert! {pool_name} ratio is {percentage:.2f}%"
            send_alert(Alert(AlertSeverity.HIGH, message, protocol, channel=CHANNEL))


def main():
    logger.info("Checking Curve pools...")
    process_pools()


if __name__ == "__main__":
    from utils.runner import run_with_alert

    # Multi-pool script with per-pool protocol routing; crash alerts go to the pegs channel.
    run_with_alert(main, "pegs")

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.chains import Chain
from utils.logger import get_logger
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
    # Lido stETH/ETH — deepest stETH<>ETH venue and the canonical wstETH depeg
    # gauge (wstETH deterministically wraps stETH). Legacy pool: exposes
    # balances(i) but not get_balances(). idx 0 = ETH, idx 1 = stETH.
    ("stETH/ETH Curve Pool", "0xDC24316b9AE028F1497c275EB9192a3Ea0f67022", 1, 0, THRESHOLD_RATIO, "wsteth"),
]


def process_pools(chain: Chain = Chain.MAINNET):
    client = ChainManager.get_client(chain)

    # Read each pool's two relevant coin balances. Using ``balances(i)`` (instead
    # of ``get_balances()``) keeps a single code path for both modern pools and
    # legacy pools like Lido stETH/ETH that don't expose ``get_balances()``.
    with client.batch_requests() as batch:
        for _, pool_address, idx_lrt, idx_other_token, _, _ in POOL_CONFIGS:
            pool = client.eth.contract(address=pool_address, abi=ABI_CURVE_POOL)
            batch.add(pool.functions.balances(idx_lrt))
            batch.add(pool.functions.balances(idx_other_token))

        responses = client.execute_batch(batch)
        if len(responses) != len(POOL_CONFIGS) * 2:
            raise ValueError(f"Expected {len(POOL_CONFIGS) * 2} responses from batch, got: {len(responses)}")

    # Process results
    for i, (pool_name, _, _, _, peg_threshold, protocol) in enumerate(POOL_CONFIGS):
        lrt_balance = responses[i * 2]
        other_balance = responses[i * 2 + 1]
        percentage = (lrt_balance / (lrt_balance + other_balance)) * 100
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

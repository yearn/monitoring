import datetime

from utils.abi import load_abi
from utils.chains import Chain
from utils.logging import get_logger
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager

# Constants
DURATION = 1800  # 30 minutes
THRESHOLD_RATIO = 0.95
PROTOCOL = "pendle"
logger = get_logger(PROTOCOL)

# Oracle address
ORACLE_ADDRESS = "0x14418800E0B4C971905423aa873e83355922428c"

# Map vaults by chain
VAULTS_BY_CHAIN = {
    Chain.ARBITRUM: [
        "0x044E75fCbF7BD3f8f4577FF317554e9c0037F145",  # weeth
        "0x1Dd930ADD968ff5913C3627dAA1e6e6FCC9dc544",  # kelp dao eth
        "0x34a2b066AF16409648eF15d239E656edB8790ca0",  # usde
    ],
    Chain.MAINNET: [
        "0xe5175a2EB7C40bC5f0E9DE4152caA14eab0fFCb7",  # weeth symbiotic
        "0xDDa02A2FA0bb0ee45Ba9179a3fd7e65E5D3B2C90",  # ageth
        "0x2F2BBc50DB252eeADD2c9B9197beb6e5Aef87e48",  # ena
        "0x57a8b4061AA598d2Bb5f70C5F931a75C9F511fc8",  # lbtc corn
        "0x57fC2D9809F777Cd5c8C433442264B6E8bE7Fce4",  # sUSDe
    ],
}

ABI_ORACLE = load_abi("protocols/pendle/abi/PendleYPLpOracle.json")
ABI_VAULT = load_abi("common-abi/YearnV3Vault.json")
ABI_STRATEGY = load_abi("protocols/pendle/abi/PendleStrategy.json")
ABI_MARKET = load_abi("protocols/pendle/abi/PendleMarket.json")


def process_assets(chain: Chain):
    client = ChainManager.get_client(chain)
    vaults = VAULTS_BY_CHAIN[chain]
    oracle = client.eth.contract(address=ORACLE_ADDRESS, abi=ABI_ORACLE)

    # First batch: Get strategies and names for all vaults
    strategies_data = []
    with client.batch_requests() as batch:
        for vault_address in vaults:
            vault = client.eth.contract(address=vault_address, abi=ABI_VAULT)
            batch.add(vault.functions.get_default_queue())
            batch.add(vault.functions.name())
        responses = client.execute_batch(batch)

    # Process vault responses and prepare strategy checks
    for i in range(0, len(responses), 2):
        strategies = responses[i]
        vault_name = responses[i + 1]
        strategies_data.append((strategies, vault_name))

    # Second batch: Check strategy assets and expiry
    with client.batch_requests() as batch:
        for strategies, _ in strategies_data:
            for strategy_address in strategies:
                strategy = client.eth.contract(address=strategy_address, abi=ABI_STRATEGY)
                batch.add(strategy.functions.totalAssets())
                batch.add(strategy.functions.isExpired())
                batch.add(strategy.functions.market())
        responses = client.execute_batch(batch)

    # Process strategy responses and prepare market checks
    active_markets = []
    idx = 0
    for strategies, vault_name in strategies_data:
        for strategy_address in strategies:
            total_assets = responses[idx]
            is_expired = responses[idx + 1]
            market_address = responses[idx + 2]

            if total_assets > 1e9 and not is_expired:
                market = client.eth.contract(address=market_address, abi=ABI_MARKET)
                active_markets.append((market_address, strategy_address, vault_name))
            idx += 3

    # Final batch: Check market expiry and get PT ratios
    with client.batch_requests() as batch:
        for market_address, _, _ in active_markets:
            market = client.eth.contract(address=market_address, abi=ABI_MARKET)
            batch.add(market.functions.isExpired())
            batch.add(market.functions.expiry())
            batch.add(oracle.functions.getPtToAssetRate(market_address, DURATION))
        responses = client.execute_batch(batch)

    # Process final results
    idx = 0
    for market_address, strategy_address, vault_name in active_markets:
        is_expired = responses[idx]
        expiry = responses[idx + 1]
        pt_ratio = responses[idx + 2] / 1e18

        if not is_expired:
            expiry_date = datetime.datetime.fromtimestamp(expiry).strftime("%Y-%m-%d %H:%M:%S")
            if pt_ratio < THRESHOLD_RATIO:
                message = (
                    "🚨 **Pendle PT Ratio** 🚨\n"
                    f"💎 Market: {market_address}\n"
                    f"📊 PT to Asset Ratio: {pt_ratio:.2f}\n"
                    f"🌐 Chain: {chain.name}\n"
                    f"🕒 Expiry: {expiry_date}\n"
                    f"🏦 Vault: {vault_name}"
                )
                send_telegram_message(message, PROTOCOL, False)
        idx += 3


def main():
    for chain in [Chain.MAINNET, Chain.ARBITRUM]:
        logger.info("Processing %s assets...", chain.name)
        process_assets(chain)


if __name__ == "__main__":
    main()

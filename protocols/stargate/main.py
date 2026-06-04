from utils.abi import load_abi
from utils.chains import Chain
from utils.logging import get_logger
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager

PROTOCOL = "stargate"
logger = get_logger(PROTOCOL)

ABI_STRATEGY = load_abi("common-abi/Strategy.json")

# Map addresses by chain
STRATEGIES_BY_CHAIN = {
    Chain.POLYGON: [
        "0x8BBa7AFd0f9B1b664C161EC31d812a8Ec15f7e1a",  # stargate staker usdc.e strategy
        "0x2c5d0c3DB75D2f8A4957c74BE09194a9271Cf28D",  # stargate staker usdt strategy
    ],
    Chain.ARBITRUM: [
        # Add arbitrum strategies here
    ],
}

BUFFER = 0.1


def print_stuff(
    total_debt: int,
    net_room: int,
    total_idle: int,
    strategy_name: str,
    underlying_token_decimals: int,
    chain_name: str,
) -> None:
    total_debt /= 10**underlying_token_decimals
    net_room /= 10**underlying_token_decimals
    total_idle /= 10**underlying_token_decimals

    message = (
        "🚨 **DANGER** 🚨\n"
        f"🌟 Stargate withdraw room: {net_room:.3f}\n"
        f"💰 Total assets idle in strategy: {total_idle:.3f}\n"
        f"📈 Total assets deposited in stargate: {total_debt:.3f}\n"
        f"🔍 Strategy name: {strategy_name}\n"
        f"🌐 Chain: {chain_name}"
    )
    send_telegram_message(message, PROTOCOL)


def process_assets(chain: Chain):
    client = ChainManager.get_client(chain)
    strategies = STRATEGIES_BY_CHAIN[chain]

    for strategy_address in strategies:
        # Build contracts
        strategy = client.eth.contract(address=strategy_address, abi=ABI_STRATEGY)

        # Batch all the calls
        with client.batch_requests() as batch:
            batch.add(strategy.functions.asset())
            batch.add(strategy.functions.availableWithdrawLimit(client.eth.address_zero))
            batch.add(strategy.functions.totalAssets())
            batch.add(strategy.functions.name())
            batch.add(strategy.functions.decimals())

        responses = client.execute_batch(batch)

        underlying_token_address = responses[0]
        withdraw_room = responses[1]
        total_assets = responses[2]
        strategy_name = responses[3]
        underlying_token_decimals = responses[4]

        # Get underlying token balance
        underlying_token = client.eth.contract(address=underlying_token_address, abi=ABI_STRATEGY)
        total_idle = underlying_token.functions.balanceOf(strategy_address).call()

        net_room = withdraw_room - total_idle
        total_debt = total_assets - total_idle

        if total_debt * (1 + BUFFER) > net_room:
            print_stuff(
                int(total_debt),
                int(net_room),
                int(total_idle),
                strategy_name,
                int(underlying_token_decimals),
                chain.name,
            )


def main():
    for chain in [Chain.POLYGON]:
        if STRATEGIES_BY_CHAIN[chain]:  # Only process chains with defined strategies
            logger.info("Processing %s assets...", chain.name)
            process_assets(chain)


if __name__ == "__main__":
    main()

from utils.abi import load_abi
from utils.chains import Chain
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager

PROTOCOL = "lido"
PEG_THRESHOLD = 0.02  # 2%
ASSET_BONDS_EXCEEDED = "GYR#357"

# Load all required ABIs
ABI_STMATIC = load_abi("lido/stmatic/abi/StMatic.json")
ABI_BALANCER_QUERY = load_abi("common-abi/BalancerQuery.json")
ABI_BALANCER_VAULT = load_abi("common-abi/BalancerVault.json")

# Contract addresses
ADDRESSES = {
    Chain.MAINNET: {
        "stmatic": "0x9ee91F9f426fA633d227f7a9b000E28b9dfd8599",
    },
    Chain.POLYGON: {
        "balancer_query": "0xE39B5e3B6D74016b2F6A9673D7d7493B6DF549d5",
        "balancer_vault": "0xBA12222222228d8Ba445958a75a0704d566BF2C8",
    },
}

BALANCER_POOL_ID = "0x8159462d255c1d24915cb51ec361f700174cd99400000000000000000000075d"

# ... rest of your swap templates remain the same ...
single_swap_template = {
    "poolId": BALANCER_POOL_ID,
    "kind": 0,
    "assetIn": "0x3A58a54C066FdC0f2D55FC9C89F0415C92eBf3C4",
    "assetOut": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
    "amount": 0,
    "userData": b"",
}

fund_management = {
    "sender": "0xBA12222222228d8Ba445958a75a0704d566BF2C8",
    "fromInternalBalance": False,
    "recipient": "0xBA12222222228d8Ba445958a75a0704d566BF2C8",
    "toInternalBalance": False,
}


def query_swap(balancer_query, balancer_vault, single_swap, fund_management):
    try:
        swap_res = balancer_query.functions.querySwap(single_swap, fund_management).call()
        return swap_res
    except Exception as e:
        message = ""
        if ASSET_BONDS_EXCEEDED in str(e):
            message = "⚠️ Asset bonds exceeded in balancer pool ⚠️ \n"
        else:
            message = f"Error calling query in balancer pool: {e}\n"

        try:
            amounts = balancer_vault.functions.getPoolTokens(BALANCER_POOL_ID).call()[1]
            matic_balance = amounts[0] / 1e18
            stmatic_balance = amounts[1] / 1e18
            total_balance = matic_balance + stmatic_balance
            matic_percentage = round(matic_balance / total_balance * 100, 2)
            message += (
                f"MATIC balance in the balancer pool: {int(matic_balance)} ({matic_percentage}%)\n"
                f"stMATIC balance in the balancer pool: {int(stmatic_balance)} ({100 - matic_percentage}%)\n"
            )
        except Exception as e:
            message += f"Error querying balances in pool: {e}"
        send_telegram_message(message, PROTOCOL, True)
        return None


def check_peg(validator_rate, balancer_rate):
    if balancer_rate == 0:
        return False
    difference = abs(validator_rate - balancer_rate)
    percentage_diff = difference / validator_rate
    return percentage_diff >= PEG_THRESHOLD


def main():
    # Initialize web3 clients
    mainnet_client = ChainManager.get_client(Chain.MAINNET)
    polygon_client = ChainManager.get_client(Chain.POLYGON)

    # Initialize contracts
    stmatic = mainnet_client.eth.contract(address=ADDRESSES[Chain.MAINNET]["stmatic"], abi=ABI_STMATIC)
    balancer_query = polygon_client.eth.contract(
        address=ADDRESSES[Chain.POLYGON]["balancer_query"], abi=ABI_BALANCER_QUERY
    )
    balancer_vault = polygon_client.eth.contract(
        address=ADDRESSES[Chain.POLYGON]["balancer_vault"], abi=ABI_BALANCER_VAULT
    )

    validator_rate = int(stmatic.functions.convertStMaticToMatic(10**18).call()[0])
    human_readable_res = validator_rate / 1e18
    message = f"🔄 1 StMATIC is: {human_readable_res:.5f} MATIC in Lido\n"

    amounts = [1e18, 100e18, 10_000e18]

    for amount in amounts:
        single_swap = single_swap_template.copy()
        single_swap["amount"] = int(amount)
        validator_rate = human_readable_res * amount
        balancer_rate = query_swap(balancer_query, balancer_vault, single_swap, fund_management)
        if balancer_rate is None:
            break
        if check_peg(validator_rate, balancer_rate):
            human_readable_amount = amount / 1e18
            human_readable_result = balancer_rate / 1e18
            message += f"📊 Swap result for amount {human_readable_amount:.5f}: {human_readable_result:.5f}"
            send_telegram_message(message)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)

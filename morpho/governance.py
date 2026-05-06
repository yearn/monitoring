from datetime import datetime

from web3 import Web3

from utils.abi import load_abi
from utils.cache import (
    get_last_executed_morpho_from_file,
    write_last_executed_morpho_to_file,
)
from utils.chains import Chain
from utils.http import request_with_retry
from utils.logging import get_logger
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager

PROTOCOL = "morpho"
logger = get_logger("morpho.governance")
MORPHO_URL = "https://app.morpho.org"
COMPOUND_URL = "https://compound.blue"
API_URL = "https://api.morpho.org/graphql"

PENDING_CAP_TYPE = "pending_cap"
REMOVABLE_AT_TYPE = "removable_at"

# Map vaults by chain
VAULTS_BY_CHAIN = {
    Chain.MAINNET: [
        ["Steakhouse USDC", "0xBEEF01735c132Ada46AA9aA4c54623cAA92A64CB"],
        ["Steakhouse USDT", "0xbEef047a543E45807105E51A8BBEFCc5950fcfBa"],
        ["Gauntlet WETH Prime", "0x2371e134e3455e0593363cBF89d3b6cf53740618"],
        ["Gauntlet USDC Prime", "0xdd0f28e19C1780eb6396170735D45153D261490d"],
        ["Gauntlet USDT Prime", "0x8CB3649114051cA5119141a34C200D65dc0Faa73"],
        ["Gauntlet WETH Core", "0x4881Ef0BF6d2365D3dd6499ccd7532bcdBCE0658"],
        ["Gauntlet USDC Core", "0x8eB67A509616cd6A7c1B3c8C21D48FF57df3d458"],
        ["VaultBridge USDC", "0xBEefb9f61CC44895d8AEc381373555a64191A9c4"],
        ["VaultBridge USDT", "0xc54b4E08C1Dcc199fdd35c6b5Ab589ffD3428a8d"],
        ["VaultBridge WETH", "0x31A5684983EeE865d943A696AAC155363bA024f9"],
        ["VaultBridge WBTC", "0x812B2C6Ab3f4471c0E43D4BB61098a9211017427"],
        ["Sentora PYUSD", "0x19b3cD7032B8C062E8d44EaCad661a0970DD8c55"],
        ["Sentora RLUSD", "0x71cb2F8038B2C5D65ddc740B2F3268890CD2A89C"],
        ["Yearn OG WETH", "0xE89371eAaAC6D46d4C3ED23453241987916224FC"],
        ["Yearn OG USDC", "0xF9bdDd4A9b3A45f980e11fDDE96e16364dDBEc49"],
        ["Yearn USDT", "0x0963232eB842BAF53E8e517691f81745C1F228a0"],
        ["Yearn WBTC", "0x2bB005127069A0F0325Fb7370967E8A2b64FB77E"],
        ["Yearn USDC", "0x68Aea7b82Df6CcdF76235D46445Ed83f85F845A3"],
    ],
    Chain.BASE: [
        ["Moonwell Flagship USDC", "0xc1256Ae5FF1cf2719D4937adb3bbCCab2E00A2Ca"],
        ["Yearn OG USDC", "0xef417a2512C5a41f69AE4e021648b69a7CdE5D03"],
        ["Yearn OG WETH", "0x1D795E29044A62Da42D927c4b179269139A28A6B"],
    ],
    Chain.KATANA: [
        ["Gauntlet WBTC", "0xf243523996ADbb273F0B237B53f30017C4364bBC"],
        ["Gauntlet USDC", "0xE4248e2105508FcBad3fe95691551d1AF14015f7"],
        ["Gauntlet USDT", "0x1ecDC3F2B5E90bfB55fF45a7476FF98A8957388E"],
        ["Gauntlet WETH", "0xC5e7AB07030305fc925175b25B93b285d40dCdFf"],
        ["Steakhouse Prime USDC", "0x61D4F9D3797BA4dA152238c53a6f93Fb665C3c1d"],
        ["Steakhouse High Yield USDC", "0x1445A01a57D7B7663CfD7B4EE0a8Ec03B379aabD"],
        ["Yearn OG WETH", "0xFaDe0C546f44e33C134c4036207B314AC643dc2E"],
        ["Yearn OG USDC", "0xCE2b8e464Fc7b5E58710C24b7e5EBFB6027f29D7"],
        ["Yearn OG USDT", "0x8ED68f91AfbE5871dCE31ae007a936ebE8511d47"],
        ["Yearn OG WBTC", "0xe107cCdeb8e20E499545C813f98Cc90619b29859"],
    ],
}


ABI_MORPHO = load_abi("morpho/abi/morpho.json")


def get_chain_name(chain: Chain):
    if chain == Chain.MAINNET:
        return "ethereum"
    else:
        return chain.name.lower()


def get_market_url(market, chain: Chain):
    if chain == Chain.POLYGON:
        return f"{COMPOUND_URL}/borrow/{market}"
    else:
        return f"{MORPHO_URL}/{get_chain_name(chain)}/market/{market}"


def get_vault_url_by_name(vault_name, chain: Chain):
    vaults = VAULTS_BY_CHAIN[chain]
    for name, address in vaults:
        if name == vault_name:
            if chain == Chain.POLYGON:
                return f"{COMPOUND_URL}/{address}"
            else:
                return f"{MORPHO_URL}/{get_chain_name(chain)}/vault/{address}"
    return None


def fetch_pending_cap_market_ids(vault_address: str, chain: Chain) -> list[str]:
    """Fetch market unique keys with pending cap submissions for a vault from Morpho GraphQL API.

    Catches brand-new markets where submitCap has been called but acceptCap has not run yet —
    those markets are not yet in supplyQueue or withdrawQueue, so the on-chain queue iteration
    misses them.

    Returns a list of hex-encoded market IDs, or an empty list on failure.
    """
    query = """
    query GetVaultPendingCaps($address: String!, $chainId: Int!) {
        vaultByAddress(address: $address, chainId: $chainId) {
            state {
                pendingConfigs {
                    items {
                        functionName
                        decodedData {
                            __typename
                            ... on VaultSetCapPendingData {
                                market { marketId }
                            }
                        }
                    }
                }
            }
        }
    }
    """
    try:
        response = request_with_retry(
            "post",
            API_URL,
            json={"query": query, "variables": {"address": vault_address, "chainId": chain.chain_id}},
        )
        data = response.json()
        items = (
            (((data.get("data") or {}).get("vaultByAddress") or {}).get("state") or {}).get("pendingConfigs") or {}
        ).get("items") or []
        market_ids = []
        for item in items:
            if item.get("functionName") != "SetCap":
                continue
            decoded = item.get("decodedData") or {}
            market = decoded.get("market") or {}
            marketId = market.get("marketId")
            if marketId:
                market_ids.append(marketId)
        return market_ids
    except Exception as e:
        logger.warning("Failed to fetch pending caps for vault %s: %s", vault_address, e)
        return []


def fetch_market_name(market_id: str, chain: Chain) -> str:
    """Fetch market name from Morpho GraphQL API.

    Returns a human-readable name like 'WBTC/USDC (86.00%)' or falls back to the market ID.
    """
    query = """
    query GetMarket($uniqueKey: String!, $chainId: Int!) {
        marketByUniqueKey(uniqueKey: $uniqueKey, chainId: $chainId) {
            lltv
            loanAsset { symbol, decimals }
            collateralAsset { symbol }
        }
    }
    """
    try:
        response = request_with_retry(
            "post",
            API_URL,
            json={"query": query, "variables": {"uniqueKey": market_id, "chainId": chain.chain_id}},
        )
        data = response.json()
        market = data["data"]["marketByUniqueKey"]
        collateral_symbol = market["collateralAsset"]["symbol"] if market.get("collateralAsset") else "idle"
        loan_symbol = market["loanAsset"]["symbol"]
        lltv_pct = int(market["lltv"]) / 1e18 * 100
        return f"{collateral_symbol}/{loan_symbol} ({lltv_pct:.2f}%)"
    except Exception as e:
        logger.warning("Failed to fetch market name for %s: %s", market_id, e)
        return market_id


def check_markets_pending_cap(name, morpho_contract, chain, w3):
    with w3.batch_requests() as batch:
        batch.add(morpho_contract.functions.supplyQueueLength())
        batch.add(morpho_contract.functions.withdrawQueueLength())

        length_responses = w3.execute_batch(batch)
        if len(length_responses) != 2:
            raise ValueError(
                "Expected 2 responses from batch(supplyQueueLength+withdrawQueueLength), got: ",
                len(length_responses),
            )
        length_of_supply_queue = length_responses[0]
        length_of_withdraw_queue = length_responses[1]

    vault_address = morpho_contract.address
    with w3.batch_requests() as batch:
        for i in range(length_of_supply_queue):
            batch.add(morpho_contract.functions.supplyQueue(i))
        for i in range(length_of_withdraw_queue):
            batch.add(morpho_contract.functions.withdrawQueue(i))
        market_responses = w3.execute_batch(batch)
        if len(market_responses) != length_of_supply_queue + length_of_withdraw_queue:
            raise ValueError(
                "Expected ",
                length_of_supply_queue + length_of_withdraw_queue,
                " responses from batch(supplyQueue+withdrawQueue), got: ",
                len(market_responses),
            )

    # supplyQueue/withdrawQueue only contain markets that have been accepted at least once.
    # Brand-new markets with a pending cap (submitCap called, acceptCap not yet run) are not
    # in any queue, so the GraphQL pendingCaps lookup is needed to catch them.
    pending_cap_market_ids = {
        bytes.fromhex(market_id.removeprefix("0x")) for market_id in fetch_pending_cap_market_ids(vault_address, chain)
    }
    markets = list(set(market_responses) | pending_cap_market_ids)

    with w3.batch_requests() as batch:
        for market in markets:
            batch.add(morpho_contract.functions.pendingCap(market))
            batch.add(morpho_contract.functions.config(market))
        pending_cap_and_config_responses = w3.execute_batch(batch)
        if len(pending_cap_and_config_responses) != len(markets) * 2:
            raise ValueError(
                "Expected ",
                len(markets) * 2,
                " responses from batch(pedningCap+config), got: ",
                len(pending_cap_and_config_responses),
            )

    for i in range(0, len(markets)):
        market_id = markets[i]
        market = Web3.to_hex(market_id)

        # Multiply by 2 because there were 2 responses per market and get
        pending_value = pending_cap_and_config_responses[i * 2]
        pending_cap_value = pending_value[0]
        pending_cap_timestamp = pending_value[1]

        # get the current config of the market
        config = pending_cap_and_config_responses[i * 2 + 1]  # Use i * 2 + 1 for config
        current_cap = config[0]  # current cap value is at index 0 in config struct

        # generat urls
        market_url = get_market_url(market, chain)
        vault_url = get_vault_url_by_name(name, chain)

        # pending_cap check
        # Don't skip past timestamps: a pending cap whose timelock has expired but hasn't been
        # accepted yet is still pending action, and may have been missed by earlier runs (e.g.,
        # if the market was brand-new and not yet visible to the on-chain queue iteration).
        # The cache check below dedupes so we only alert once per unique timestamp.
        if pending_cap_timestamp > 0:
            last_executed_morpho = get_last_executed_morpho_from_file(vault_address, market, PENDING_CAP_TYPE)

            if pending_cap_timestamp > last_executed_morpho:
                time = datetime.fromtimestamp(pending_cap_timestamp).strftime("%Y-%m-%d %H:%M:%S")
                market_name = fetch_market_name(market, chain)
                if current_cap == 0:
                    message = (
                        f"Adding new market [{market_name}]({market_url}) with cap {pending_cap_value} "
                        f"to vault [{name}]({vault_url}) on {chain.name}. "
                        f"Queued for {time}"
                    )
                else:
                    difference_in_percentage = ((pending_cap_value - current_cap) / current_cap) * 100
                    message = (
                        f"Updating cap to new cap {pending_cap_value}, current cap {current_cap}, "
                        f"difference: {difference_in_percentage:.2f}%. \n"
                        f"For vault [{name}]({vault_url}) for market: [{market_name}]({market_url}) on {chain.name}. "
                        f"Queued for {time}"
                    )
                send_telegram_message(message, PROTOCOL)
                write_last_executed_morpho_to_file(vault_address, market, PENDING_CAP_TYPE, pending_cap_timestamp)
            else:
                logger.info(
                    "Skipping pending cap update for vault %s(%s) for market: %s because it was already executed",
                    name,
                    vault_url,
                    market_url,
                )

        # removable_at check
        removable_at = config[2]  # removable_at value is at index 2 in config struct
        if removable_at > 0:
            if removable_at > get_last_executed_morpho_from_file(vault_address, market, REMOVABLE_AT_TYPE):
                time = datetime.fromtimestamp(removable_at).strftime("%Y-%m-%d %H:%M:%S")
                market_name = fetch_market_name(market, chain)
                send_telegram_message(
                    f"Vault [{name}]({vault_url}) queued to remove market: [{market_name}]({market_url}) at {time}",
                    PROTOCOL,
                )
                write_last_executed_morpho_to_file(vault_address, market, REMOVABLE_AT_TYPE, removable_at)
            else:
                logger.info(
                    "Skipping removable_at update for vault %s(%s) for market: %s because it was already executed",
                    name,
                    vault_url,
                    market_url,
                )


def check_pending_role_change(name, morpho_contract, role_type, timestamp, chain):
    market_id = ""  # use empty string for all markets because the value is used per vault
    if timestamp > get_last_executed_morpho_from_file(morpho_contract.address, market_id, role_type):
        vault_url = get_vault_url_by_name(name, chain)
        send_telegram_message(
            f"{role_type.capitalize()} is changing for vault [{name}]({vault_url})",
            PROTOCOL,
        )
        write_last_executed_morpho_to_file(morpho_contract.address, market_id, role_type, timestamp)


def check_timelock_and_guardian(name, morpho_contract, chain, client):
    with morpho_contract.w3.batch_requests() as batch:
        batch.add(morpho_contract.functions.pendingTimelock())
        batch.add(morpho_contract.functions.pendingGuardian())
        responses = client.execute_batch(batch)
        if len(responses) != 2:
            raise ValueError("Expected 2 responses from batch, got: ", len(responses))

        timelock = responses[0][1]  # [1] to get the timestamp
        guardian = responses[1][1]  # [1] to get the timestamp

    check_pending_role_change(name, morpho_contract, "timelock", timelock, chain)
    check_pending_role_change(name, morpho_contract, "guardian", guardian, chain)


def get_data_for_chain(chain: Chain):
    client = ChainManager.get_client(chain)
    vaults = VAULTS_BY_CHAIN[chain]

    logger.info("Processing Morpho Vaults on %s ...", chain.name)
    logger.debug("Vaults: %s", vaults)

    for vault in vaults:
        morpho_contract = client.eth.contract(address=vault[1], abi=ABI_MORPHO)
        check_markets_pending_cap(vault[0], morpho_contract, chain, client)
        check_timelock_and_guardian(vault[0], morpho_contract, chain, client)


def main():
    get_data_for_chain(Chain.MAINNET)
    get_data_for_chain(Chain.KATANA)
    get_data_for_chain(Chain.BASE)


if __name__ == "__main__":
    main()

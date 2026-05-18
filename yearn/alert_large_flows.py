#!/usr/bin/env python3
import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, getcontext

from dotenv import load_dotenv

from utils.abi import load_abi
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import EXPLORER_URLS, Chain
from utils.defillama import fetch_prices
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager

load_dotenv()

getcontext().prec = 40

ENVIO_GRAPHQL_URL = os.getenv("ENVIO_GRAPHQL_URL")
DEFAULT_LOG_LEVEL = os.getenv("ALERT_LARGE_FLOWS_LOG_LEVEL", "WARNING")
IGNORED_FROM_ADDRESS = "0x283132390ea87d6ecc20255b59ba94329ee17961"
PROTOCOL = "yearn"
CACHE_KEY_LAST_ALERT_TX = f"{PROTOCOL}_LARGE_FLOW_LAST_TX"

FALLBACK_LARGE_FLOW_RATIO = Decimal("0.1")

ERC20_ABI = load_abi("common-abi/ERC20.json")
_total_supply_cache: dict[tuple[int, str], Decimal] = {}

VAULTS = {
    "0xbe53a109b494e5c9f97b9cd39fe969be68bf6204": {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": 1,
        "token_address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    },
    "0x028ec7330ff87667b6dfb0d94b954c820195336c": {
        "symbol": "DAI",
        "decimals": 18,
        "chain_id": 1,
        "token_address": "0x6b175474e89094c44da98b954eedeac495271d0f",
    },
    "0x310b7ea7475a0b449cfd73be81522f1b88efafaa": {
        "symbol": "USDT",
        "decimals": 6,
        "chain_id": 1,
        "token_address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
    },
    "0x182863131f9a4630ff9e27830d945b1413e347e8": {
        "symbol": "USDS",
        "decimals": 18,
        "chain_id": 1,
        "token_address": "0xdc035d45d973e3ec169d2276ddab16f1e407384f",
    },
    "0xbf319ddc2edc1eb6fdf9910e39b37be221c8805f": {
        "symbol": "CRVUSD",
        "decimals": 18,
        "chain_id": 1,
        "token_address": "0xf939e0a03fb07f59a73314e73794be0e57ac1b4e",
    },
    "0xc56413869c6cdf96496f2b1ef801fedbdfa7ddb0": {
        "symbol": "WETH",
        "decimals": 18,
        "chain_id": 1,
        "token_address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    },
    "0xb13cf163d916917d9cd6e836905ca5f12a1def4b": {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": 8453,
        "token_address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    },
    "0xc3bd0a2193c8f027b82dde3611d18589ef3f62a9": {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": 8453,
        "token_address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    },
    "0x4d81c7d534d703e0a0aecadf668c0e0253e1f1c3": {
        "symbol": "WETH",
        "decimals": 18,
        "chain_id": 8453,
        "token_address": "0x4200000000000000000000000000000000000006",
    },
    "0x25f32ec89ce7732a4e9f8f3340a09259f823b7d3": {
        "symbol": "cbBTC",
        "decimals": 8,
        "chain_id": 8453,
        "token_address": "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
    },
    "0x989381f7efb45f97e46be9f390a69c5d94bf9e17": {
        "symbol": "cbETH",
        "decimals": 18,
        "chain_id": 8453,
        "token_address": "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",
    },
    # Arbitrum (chain_id: 42161)
    "0x252b965400862d94bda35fecf7ee0f204a53cc36": {
        "symbol": "USND",
        "decimals": 18,
        "chain_id": 42161,
        "token_address": "0x4ecf61a6c2fab8a047ceb3b3b263b401763e9d49",
    },
    "0xb739ae19620f7ecb4fb84727f205453aa5bc1ad2": {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": 42161,
        "token_address": "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",
    },
    "0x9fa306b1f4a6a83fec98d8ebbabedff78c407f6b": {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": 42161,
        "token_address": "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",
    },
    "0x6faf8b7ffee3306efcfc2ba9fec912b4d49834c1": {
        "symbol": "USDC",
        "decimals": 6,
        "chain_id": 42161,
        "token_address": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
    },
    "0xc0ba9bfed28ab46da48d2b69316a3838698ef3f5": {
        "symbol": "USDT",
        "decimals": 6,
        "chain_id": 42161,
        "token_address": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
    },
    "0x7deb119b92b76f78c212bc54fbbb34cea75f4d4a": {
        "symbol": "ARB",
        "decimals": 18,
        "chain_id": 42161,
        "token_address": "0x912ce59144191c1204e64559fe8253a0e49e6548",
    },
    # Katana (chain_id: 747474)
    "0x80c34bd3a3569e126e7055831036aa7b212cb159": {
        "symbol": "vbUSDC",
        "decimals": 6,
        "chain_id": 747474,
        "token_address": "0x203a662b0bd271a6ed5a60edfbd04bfce608fd36",
    },
    "0xe007ca01894c863d7898045ed5a3b4abf0b18f37": {
        "symbol": "vbETH",
        "decimals": 18,
        "chain_id": 747474,
        "token_address": "0xee7d8bcfb72bc1880d0cf19822eb0a2e6577ab62",
    },
    "0x9a6bd7b6fd5c4f87eb66356441502fc7dcdd185b": {
        "symbol": "vbUSDT",
        "decimals": 6,
        "chain_id": 747474,
        "token_address": "0x2dca96907fde857dd3d816880a0df407eeb2d2f2",
    },
    "0xaa0362ecc584b985056e47812931270b99c91f9d": {
        "symbol": "vbWBTC",
        "decimals": 8,
        "chain_id": 747474,
        "token_address": "0x0913da6da4b42f538b445599b46bb4622342cf52",
    },
}

DEFILLAMA_CHAIN = {
    1: "ethereum",
    8453: "base",
    42161: "arbitrum",
    747474: "katana",
}

STABLES = {"USDC", "USDT", "DAI", "USDS", "CRVUSD"}

_price_cache: dict[tuple[int, str], tuple[float, Decimal]] = {}
_logger = logging.getLogger("alert_large_flows")


def http_json(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None):
    _logger.info("http_json %s %s", method, url)
    data = None
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        _logger.info("http_json status=%s", resp.status)
        return payload


def gql_request(query: str, variables: dict) -> dict | None:
    if not ENVIO_GRAPHQL_URL:
        raise RuntimeError(
            "ENVIO_GRAPHQL_URL is not set. Set it to the Envio GraphQL endpoint, "
            "e.g. export ENVIO_GRAPHQL_URL='https://.../graphql'."
        )
    _logger.info("gql_request")
    payload = {"query": query, "variables": variables}

    try:
        return http_json(ENVIO_GRAPHQL_URL, method="POST", body=payload)
    except urllib.error.HTTPError as exc:
        send_telegram_message(
            f"⚠️ {PROTOCOL} Large Flow Alert: Envio GraphQL error (HTTP {exc.code}). Skipping this run.",
            PROTOCOL,
        )
        _logger.error("Envio request failed with HTTP %d", exc.code)
        return None


def format_units(value: str, decimals: int) -> Decimal:
    return Decimal(value) / (Decimal(10) ** Decimal(decimals))


def prefetch_prices(events: list[dict]) -> None:
    """Pre-fetch all non-stable token prices in a single DeFiLlama batch call.

    Populates _price_cache for all unique non-stable tokens referenced in events,
    so subsequent per-event lookups are served from cache without extra API calls.

    Args:
        events: List of deposit/withdraw event dicts.
    """
    token_keys: list[str] = []
    key_to_cache_key: dict[str, tuple[int, str]] = {}
    now = time.time()

    for event in events:
        vault = VAULTS.get(event["vaultAddress"].lower())
        if not vault or vault["symbol"] in STABLES:
            continue
        chain_id = vault["chain_id"]
        token_address = vault["token_address"].lower()
        chain = DEFILLAMA_CHAIN.get(chain_id)
        if not chain:
            continue
        cache_key = (chain_id, token_address)
        cached = _price_cache.get(cache_key)
        if cached and now - cached[0] < 60:
            continue
        token_key = f"{chain}:{token_address}"
        if token_key not in key_to_cache_key:
            token_keys.append(token_key)
            key_to_cache_key[token_key] = cache_key

    if not token_keys:
        return

    try:
        prices = fetch_prices(token_keys)
        fetched_now = time.time()
        for token_key, price in prices.items():
            cache_key = key_to_cache_key.get(token_key)
            if cache_key:
                _price_cache[cache_key] = (fetched_now, price)
        _logger.info("prefetched %d/%d prices from DeFiLlama", len(prices), len(token_keys))
    except Exception as exc:
        _logger.warning("prefetch_prices failed: %s", exc)


def get_token_price_usd(chain_id: int, token_address: str, symbol: str) -> Decimal:
    """Get the current USD price for a token, using cache when fresh.

    Args:
        chain_id: EVM chain ID.
        token_address: Token contract address.
        symbol: Token symbol (stablecoins return 1 without an API call).

    Returns:
        Price in USD as Decimal.
    """
    if symbol in STABLES:
        return Decimal("1")

    cache_key = (chain_id, token_address.lower())
    cached = _price_cache.get(cache_key)
    if cached and time.time() - cached[0] < 60:
        return cached[1]

    chain = DEFILLAMA_CHAIN.get(chain_id)
    if not chain:
        raise RuntimeError(f"Unsupported chain for pricing: {chain_id}")
    token_key = f"{chain}:{token_address.lower()}"

    prices = fetch_prices([token_key])
    price = prices.get(token_key)
    if price is None:
        raise RuntimeError(f"Missing price for {token_key}")
    _price_cache[cache_key] = (time.time(), price)
    return price


def load_events(limit: int, chain_ids: list[int], since_ts: int | None):
    _logger.info(
        "load_events limit=%s chain_ids=%s since_ts=%s",
        limit,
        chain_ids,
        since_ts,
    )
    query = """
    query GetRecentFlows($limit: Int!, $chainIds: [Int!]!, $sinceTs: Int) {
      deposits: Deposit(
        where: { chainId: { _in: $chainIds }, blockTimestamp: { _gte: $sinceTs } }
        order_by: { blockTimestamp: desc, blockNumber: desc, logIndex: desc }
        limit: $limit
      ) {
        id
        assets
        vaultAddress
        chainId
        blockNumber
        blockTimestamp
        transactionHash
        transactionFrom
      }
      withdrawals: Withdraw(
        where: { chainId: { _in: $chainIds }, blockTimestamp: { _gte: $sinceTs } }
        order_by: { blockTimestamp: desc, blockNumber: desc, logIndex: desc }
        limit: $limit
      ) {
        id
        assets
        vaultAddress
        chainId
        blockNumber
        blockTimestamp
        transactionHash
        transactionFrom
      }
    }
    """
    variables = {"limit": limit, "chainIds": chain_ids, "sinceTs": since_ts or 0}
    return gql_request(query, variables)


def filter_events_since_last_alert(events: list[dict], use_cache: bool) -> list[dict]:
    if not use_cache:
        return events
    last_alerted_tx = str(get_last_value_for_key_from_file(cache_filename, CACHE_KEY_LAST_ALERT_TX))
    if not last_alerted_tx or last_alerted_tx == "0":
        return events

    events_sorted = sorted(
        events,
        key=lambda e: int(e.get("blockNumber", 0)),
        reverse=True,
    )
    for idx, event in enumerate(events_sorted):
        if event.get("transactionHash") == last_alerted_tx:
            return events_sorted[:idx]

    return events_sorted


def get_vault_total_supply(chain_id: int, vault_address: str, decimals: int) -> Decimal | None:
    cache_key = (chain_id, vault_address.lower())
    cached = _total_supply_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        chain = Chain.from_chain_id(chain_id)
        client = ChainManager.get_client(chain)
        contract = client.get_contract(vault_address, ERC20_ABI)
        total_supply_raw = client.execute(contract.functions.totalSupply().call)
    except Exception as exc:
        _logger.warning("failed to fetch totalSupply for %s: %s", vault_address, exc)
        return None

    total_supply = format_units(str(total_supply_raw), decimals)
    _total_supply_cache[cache_key] = total_supply
    return total_supply


def send_large_flow_alert(
    event: dict,
    vault: dict,
    amount: Decimal,
    value: Decimal | None,
) -> str:
    chain = Chain.from_chain_id(vault["chain_id"])
    explorer_base = EXPLORER_URLS.get(vault["chain_id"])
    tx_hash = event["transactionHash"]
    tx_url = f"{explorer_base}/tx/{tx_hash}" if explorer_base else None
    vault_address = event["vaultAddress"]
    vault_url = f"{explorer_base}/address/{vault_address}" if explorer_base else None
    from_address = event.get("transactionFrom")
    from_url = f"{explorer_base}/address/{from_address}" if explorer_base and from_address else None

    flow_type = event["type"].capitalize()
    message_lines = [
        f"🚨 Large {flow_type} detected",
        f"🏦 Vault: [{vault_address}]({vault_url}) ({vault['symbol']}) on {chain.name}",
        f"💰 Amount: {amount:,.6f} {vault['symbol']}",
        f"💵 Value: ${value:,.2f}" if value is not None else None,
        f"🔗 Tx: [{tx_hash}]({tx_url})" if tx_url else f"🔗 Tx: {tx_hash}",
    ]
    message_lines = [line for line in message_lines if line]
    if from_address:
        message_lines.append(f"👤 From: [{from_address}]({from_url})" if from_url else f"👤 From: {from_address}")

    message = "\n".join(message_lines)
    send_telegram_message(message, PROTOCOL)
    return tx_hash


def alert_on_large_flows(events: list[dict], threshold_usd: Decimal, use_cache: bool):
    _logger.info("evaluating %s events", len(events))
    prefetch_prices(events)
    events_to_alert = filter_events_since_last_alert(events, use_cache)
    events_sorted = sorted(
        events_to_alert,
        key=lambda e: int(e.get("blockNumber", 0)),
    )
    cached_tx_to_write = None
    for event in events_sorted:
        tx_from = event.get("transactionFrom") or ""
        tx_from = tx_from.lower()
        if tx_from == IGNORED_FROM_ADDRESS:
            _logger.info("skip ignored tx.from %s", tx_from)
            continue
        vault = VAULTS.get(event["vaultAddress"].lower())
        if not vault:
            _logger.warning("skip unknown vault %s", event["vaultAddress"])
            continue
        amount = format_units(event["assets"], vault["decimals"])
        if vault["symbol"] in STABLES:
            value = amount
            if value >= threshold_usd:
                cached_tx_to_write = send_large_flow_alert(event, vault, amount, value)
            continue

        price = None
        try:
            price = get_token_price_usd(vault["chain_id"], vault["token_address"], vault["symbol"])
        except Exception as exc:
            _logger.warning("price unavailable for %s on %s: %s", vault["symbol"], vault["chain_id"], exc)

        if price is not None:
            value = amount * price
            if value >= threshold_usd:
                cached_tx_to_write = send_large_flow_alert(event, vault, amount, value)
            continue

        total_supply = get_vault_total_supply(vault["chain_id"], event["vaultAddress"], vault["decimals"])
        if total_supply is None or total_supply == 0:
            continue
        ratio = amount / total_supply
        ratio_threshold = FALLBACK_LARGE_FLOW_RATIO
        if ratio >= ratio_threshold:
            cached_tx_to_write = send_large_flow_alert(
                event,
                vault,
                amount,
                None,
            )
    if use_cache and cached_tx_to_write:
        write_last_value_to_file(cache_filename, CACHE_KEY_LAST_ALERT_TX, cached_tx_to_write)


def main():
    parser = argparse.ArgumentParser(description="Alert on large deposit/withdraw events.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--threshold-usd", type=Decimal, default=Decimal("1000000"))  # 1M USD
    parser.add_argument("--since-seconds", type=int, default=7200)  # 2 hours is default
    default_chain_ids = ",".join(str(c) for c in sorted({v["chain_id"] for v in VAULTS.values()}))
    parser.add_argument("--chain-ids", type=str, default=default_chain_ids)
    parser.add_argument("--no-cache", action="store_true", help="Disable caching of last alerted tx")
    parser.add_argument(
        "--log-level",
        type=str,
        default=DEFAULT_LOG_LEVEL,
        help="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="[%(name)s] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    _logger.info(
        "start limit=%s threshold_usd=%s since_seconds=%s chain_ids=%s",
        args.limit,
        args.threshold_usd,
        args.since_seconds,
        args.chain_ids,
    )

    since_ts = None
    if args.since_seconds:
        since_ts = int(time.time()) - args.since_seconds

    chain_ids = [int(x.strip()) for x in args.chain_ids.split(",") if x.strip()]

    response = load_events(args.limit, chain_ids, since_ts)
    if response is None or "errors" in response:
        _logger.warning("no response from load_events or gql errors: %s", response.get("errors"))
        return

    data = response.get("data", {})
    deposits = data.get("deposits", [])
    withdrawals = data.get("withdrawals", [])
    _logger.info(
        "fetched deposits=%s withdrawals=%s",
        len(deposits),
        len(withdrawals),
    )

    for event in deposits:
        event["type"] = "deposit"
    for event in withdrawals:
        event["type"] = "withdraw"

    alert_on_large_flows(deposits + withdrawals, args.threshold_usd, not args.no_cache)


if __name__ == "__main__":
    main()

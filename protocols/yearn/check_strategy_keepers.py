#!/usr/bin/env python3
"""Monitor ETH balances of direct keeper wallets for funded Yearn v2 strategies.

V3 keeper gas checks are handled in the vaults repo. Kong discovers strategies
of non-retired Yearn v2 vaults; on-chain estimatedTotalAssets() skips empty
strategies. Keeper contracts are logged and excluded: a contract's ETH balance
is not the gas balance of the external automation network that calls it.

Keeper and asset reads, code checks, and wallet balances each use one batch per
chain. Mainnet alerts below $10; Optimism, Base, and Arbitrum alert below $2.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from dotenv import load_dotenv
from web3 import Web3

from protocols.yearn.kong import fetch_kong_v2_strategies
from utils import store
from utils.alert import Alert, AlertSeverity, send_alert
from utils.chains import Chain
from utils.defillama import fetch_prices
from utils.logger import get_logger
from utils.telegram import YEARN_MAINTENANCE_CHANNEL, resolve_channel
from utils.web3_wrapper import ChainManager, Web3Client

load_dotenv()

logger = get_logger("yearn.check_strategy_keepers")

PROTOCOL = "yearn"
# Alert-history key: internal-only, so these alerts stay off the public Yearn page.
ALERT_PROTOCOL = "yearn-internal"

MIN_BALANCE_USD_MAINNET = Decimal("10")
MIN_BALANCE_USD_L2 = Decimal("2")
WEI_PER_NATIVE = Decimal(10) ** 18

# Native gas token symbol and DeFiLlama price key per chain.
NATIVE_TOKENS: dict[Chain, tuple[str, str]] = {
    Chain.MAINNET: ("ETH", "coingecko:ethereum"),
    Chain.BASE: ("ETH", "coingecko:ethereum"),
    Chain.OPTIMISM: ("ETH", "coingecko:ethereum"),
    Chain.ARBITRUM: ("ETH", "coingecko:ethereum"),
}

STRATEGY_ABI = [
    {
        "inputs": [],
        "name": "keeper",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "estimatedTotalAssets",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ALERT_STATE_NAMESPACE = "yearn_v2_keepers_alerts"
ALERT_STATE_KEY = "all"
ALERT_REMINDER_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class KeeperStrategy:
    """A Yearn v2 strategy discovered from a non-retired vault in Kong."""

    chain: Chain
    address: str
    name: str


@dataclass
class KeeperWallet:
    """A wallet that pays gas for one or more strategies."""

    address: str
    strategies: set[str] = field(default_factory=set)
    balance_wei: int = 0


@dataclass
class ChainResult:
    """Direct keeper wallets found on one chain."""

    chain: Chain
    wallets: dict[str, KeeperWallet]


@dataclass(frozen=True)
class Issue:
    """One alertable finding, with a stable code for deduplication."""

    code: str
    message: str


def select_keeper_strategies(strategies: Iterable[dict[str, Any]]) -> dict[Chain, list[KeeperStrategy]]:
    """Group discovered Yearn v2 strategies by supported ETH-gas chain.

    Args:
        strategies: Rows from fetch_kong_v2_strategies, already filtered to v2 vaults.

    Returns:
        Strategies keyed by chain and deduplicated by address.
    """
    chains_by_id = {chain.chain_id: chain for chain in NATIVE_TOKENS}
    selected: dict[Chain, dict[str, KeeperStrategy]] = {}
    for strategy in strategies:
        chain = chains_by_id.get(int(strategy["chain_id"]))
        if chain is None:
            logger.info(
                "Skipping v2 strategy %s (%s) on unmonitored chain %s",
                strategy["name"],
                strategy["address"],
                strategy["chain_id"],
            )
            continue
        address = Web3.to_checksum_address(str(strategy["address"]))
        selected.setdefault(chain, {})[address] = KeeperStrategy(chain, address, str(strategy["name"]))
    return {chain: list(by_address.values()) for chain, by_address in selected.items()}


def min_balance_usd(chain: Chain) -> Decimal:
    """Return the minimum USD value of native gas token a keeper wallet must hold on ``chain``."""
    return MIN_BALANCE_USD_MAINNET if chain == Chain.MAINNET else MIN_BALANCE_USD_L2


def _batch(client: Web3Client, calls: Iterable[Any]) -> list[Any]:
    """Execute the given web3 calls as one JSON-RPC batch.

    ``calls`` must be a lazy iterable (e.g. a generator): web3 only defers a call that is
    built inside ``batch_requests()``, so calls built beforehand run one by one.
    """
    with client.batch_requests() as batch:
        for call in calls:
            batch.add(call)
        return list(client.execute_batch(batch))


def _is_contract_map(client: Web3Client, addresses: list[str]) -> dict[str, bool]:
    """Return whether each address has code, using one batched ``eth_getCode``."""
    if not addresses:
        return {}
    codes = _batch(client, (client.eth.get_code(address) for address in addresses))
    return {address: len(code) > 0 for address, code in zip(addresses, codes)}


def collect_keeper_wallets(chain: Chain, strategies: list[KeeperStrategy]) -> ChainResult:
    """Collect direct keeper wallets for v2 strategies holding assets.

    Args:
        chain: Chain the strategies live on.
        strategies: V2 strategies discovered from non-retired vaults.

    Returns:
        Deduplicated direct keeper wallets with balances. Contract keepers are
        excluded because their own balances do not fund their callers' gas.
    """
    client = ChainManager.get_client(chain)
    contracts = [client.get_contract(strategy.address, STRATEGY_ABI) for strategy in strategies]
    results = _batch(
        client,
        (call for c in contracts for call in (c.functions.keeper().call(), c.functions.estimatedTotalAssets().call())),
    )
    strategies_by_keeper: dict[str, set[str]] = {}
    for strategy, keeper, total_assets in zip(strategies, results[0::2], results[1::2]):
        if int(total_assets) == 0:
            logger.debug("Skipping %s (%s): no assets", strategy.name, strategy.address)
            continue
        strategies_by_keeper.setdefault(Web3.to_checksum_address(keeper), set()).add(strategy.name)

    keeper_is_contract = _is_contract_map(client, list(strategies_by_keeper))
    wallets: dict[str, KeeperWallet] = {}
    for keeper, is_contract in keeper_is_contract.items():
        if is_contract:
            logger.info(
                "Skipping v2 keeper contract %s: %s",
                _address_url(chain, keeper),
                _strategy_list(strategies_by_keeper[keeper]),
            )
            continue
        wallets[keeper] = KeeperWallet(keeper, strategies_by_keeper[keeper])

    addresses = list(wallets)
    if addresses:
        for address, balance in zip(addresses, _batch(client, (client.eth.get_balance(a) for a in addresses))):
            wallets[address].balance_wei = int(balance)
    return ChainResult(chain, wallets)


def _address_url(chain: Chain, address: str) -> str:
    return f"{chain.explorer_url}/address/{address}"


def _strategy_list(strategies: set[str]) -> str:
    return ", ".join(sorted(strategies))


def _low_balance_issues(result: ChainResult, symbol: str, price: Decimal) -> list[Issue]:
    """Return an issue for every wallet on the chain holding less than its USD minimum."""
    chain = result.chain
    minimum = min_balance_usd(chain)
    issues: list[Issue] = []
    for wallet in sorted(result.wallets.values(), key=lambda w: w.address):
        balance = Decimal(wallet.balance_wei) / WEI_PER_NATIVE
        balance_usd = balance * price
        if balance_usd >= minimum:
            logger.info("%s keeper %s holds %.6f %s ($%.2f)", chain.name, wallet.address, balance, symbol, balance_usd)
            continue
        issues.append(
            Issue(
                f"low:{chain.chain_id}:{wallet.address.lower()}",
                f"{chain.name}: {_address_url(chain, wallet.address)} holds {balance:.6f} {symbol} "
                f"(${balance_usd:.2f} < ${minimum})\n  keeps: {_strategy_list(wallet.strategies)}",
            )
        )
    return issues


def evaluate(results: list[ChainResult], prices: dict[str, Decimal]) -> list[Issue]:
    """Return low-balance issues across all chains.

    Args:
        results: Per-chain keeper wallets with balances.
        prices: DeFiLlama USD prices keyed by price key.

    Returns:
        Issues in chain order; empty when every wallet is funded.
    """
    issues: list[Issue] = []
    for result in results:
        if not result.wallets:
            continue
        chain = result.chain
        symbol, price_key = NATIVE_TOKENS[chain]
        price = prices.get(price_key)
        if price is None or price <= 0:
            issues.append(Issue(f"price:{chain.chain_id}", f"{chain.name}: no {symbol} USD price from DeFiLlama"))
        else:
            issues.extend(_low_balance_issues(result, symbol, price))
    return issues


def should_send_alert(fingerprint: str, previous_raw: str | None, now: int) -> bool:
    """Return whether the current issue set is new or due for its daily reminder."""
    previous: dict[str, Any] = {}
    if previous_raw:
        try:
            previous = json.loads(previous_raw)
        except TypeError, json.JSONDecodeError:
            previous = {}
    if not fingerprint:
        return False
    last_alert = int(previous.get("last_alert", 0))
    return previous.get("fingerprint") != fingerprint or now - last_alert >= ALERT_REMINDER_SECONDS


def build_message(issues: list[Issue]) -> str:
    """Build the plain-text Telegram message for the given issues."""
    lines = ["Yearn V2 Keeper Gas Warning"]
    lines.extend(f"- {issue.message}" for issue in issues)
    return "\n".join(lines)


def main() -> None:
    """Check direct keeper ETH balances for funded Yearn v2 strategies."""
    parser = argparse.ArgumentParser(description="Check ETH balances of Yearn v2 strategy keeper wallets")
    parser.add_argument("--dry-run", action="store_true", help="Read and evaluate without storing state or alerting")
    args = parser.parse_args()

    strategies_by_chain = select_keeper_strategies(fetch_kong_v2_strategies())
    logger.info("Found %d Yearn v2 strategies", sum(len(items) for items in strategies_by_chain.values()))

    results: list[ChainResult] = []
    issues: list[Issue] = []
    for chain, strategies in strategies_by_chain.items():
        try:
            results.append(collect_keeper_wallets(chain, strategies))
        except Exception as exc:  # noqa: BLE001 - one broken chain must not hide the others
            logger.exception("Failed to resolve strategy keepers on %s", chain.name)
            issues.append(Issue(f"error:{chain.chain_id}:{type(exc).__name__}", f"{chain.name}: monitor error {exc}"))

    price_keys = sorted({NATIVE_TOKENS[result.chain][1] for result in results if result.wallets})
    issues = evaluate(results, fetch_prices(price_keys)) + issues

    fingerprint = "|".join(sorted(issue.code for issue in issues))
    now = int(time.time())
    if not issues:
        logger.info("All strategy keeper wallets are funded")
        if not args.dry_run and store.state_get(ALERT_STATE_NAMESPACE, ALERT_STATE_KEY):
            store.state_set(ALERT_STATE_NAMESPACE, ALERT_STATE_KEY, json.dumps({"fingerprint": "", "last_alert": now}))
        return

    message = build_message(issues)
    if args.dry_run:
        logger.warning("Dry run would alert: %s", message.replace("\n", " | "))
        return
    if not should_send_alert(fingerprint, store.state_get(ALERT_STATE_NAMESPACE, ALERT_STATE_KEY), now):
        logger.info("Issues unchanged since last alert; skipping until reminder is due")
        return

    send_alert(
        Alert(
            AlertSeverity.MEDIUM,
            message,
            ALERT_PROTOCOL,
            channel=resolve_channel(YEARN_MAINTENANCE_CHANNEL, PROTOCOL),
        ),
        plain_text=True,
    )
    store.state_set(ALERT_STATE_NAMESPACE, ALERT_STATE_KEY, json.dumps({"fingerprint": fingerprint, "last_alert": now}))


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL, alert_protocol=ALERT_PROTOCOL)

#!/usr/bin/env python3
"""Monitor native gas balances of the wallets that keep Yearn looper and lender-borrower strategies running.

Strategies are discovered from Kong (name contains "Looper" or "Lender Borrower", not
shut down) and skipped when ``totalAssets()`` is zero. Each strategy's ``keeper()`` is
read on-chain. A keeper that is a wallet pays gas itself. A keeper that is a contract
(LooperKeeper, PublicAllocatorTendExecutor, yHaaSRelayer, TKSRelayer) holds no ETH: its
allow-listed wallets send ``msg.value`` and pay gas, so those wallets are resolved from
``AllowedSet`` events plus ``EXTRA_KEEPER_CALLERS``.

Every address is collected into a deduplicated map first, then each RPC step
(``keeper()`` with ``totalAssets()``, ``eth_getCode``, ``eth_getBalance``) runs as a
single batch per chain.
Only mainnet, Base and Katana are monitored. An alert fires when a wallet holds less
than ``$10`` of the native token on mainnet or ``$2`` on Base/Katana, or when a keeper
contract has no known wallet caller.
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

from protocols.yearn.kong import fetch_kong_strategies
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

# Lower-cased Kong strategy-name fragments of strategies that depend on a keeper.
KEEPER_STRATEGY_MARKERS = ("looper", "lender borrower")
MIN_BALANCE_USD_MAINNET = Decimal("10")
MIN_BALANCE_USD_L2 = Decimal("2")
WEI_PER_NATIVE = Decimal(10) ** 18

# Native gas token symbol and DeFiLlama price key per chain.
NATIVE_TOKENS: dict[Chain, tuple[str, str]] = {
    Chain.MAINNET: ("ETH", "coingecko:ethereum"),
    Chain.BASE: ("ETH", "coingecko:ethereum"),
    Chain.KATANA: ("ETH", "coingecko:ethereum"),
}

# AllowedSet(address indexed, bool indexed), emitted by LooperKeeper and PublicAllocatorTendExecutor.
ALLOWED_SET_TOPIC = "0x" + Web3.keccak(text="AllowedSet(address,bool)").hex().removeprefix("0x")

# Wallets that call a keeper contract but cannot be found from AllowedSet events:
# yHaaSRelayer and TKSRelayer emit no events, and the LooperKeeper-style constructors
# allow-list governance without emitting one. Contract entries (multisigs) are dropped on-chain.
EXTRA_KEEPER_CALLERS: dict[Chain, dict[str, tuple[str, ...]]] = {
    Chain.MAINNET: {
        # yHaaSRelayer
        "0x604e586F17cE106B64185A7a0d2c1Da5bAce711E": (
            "0x283132390eA87D6ecc20255B59Ba94329eE17961",
            "0x420ACF637D662b80cca8bEfb327AA24039E7e0Fa",
        ),
        # PublicAllocatorTendExecutor (governance)
        "0xb86c97f61DB0b339D4fFe7F39f7725B80a121D5D": ("0x1b5f15DCb82d25f91c65b53CEe151E8b9fBdD271",),
    },
    Chain.KATANA: {
        # TKSRelayer (same yHaaS bots as mainnet)
        "0xC29cbdcf5843f8550530cc5d627e1dd3007EF231": (
            "0x283132390eA87D6ecc20255B59Ba94329eE17961",
            "0x420ACF637D662b80cca8bEfb327AA24039E7e0Fa",
        ),
    },
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
        "name": "totalAssets",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ALERT_STATE_NAMESPACE = "yearn_strategy_keepers_alerts"
ALERT_STATE_KEY = "all"
ALERT_REMINDER_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class KeeperStrategy:
    """A keeper-run strategy discovered from Kong."""

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
    """Keeper wallets and unresolved keeper contracts found on one chain."""

    chain: Chain
    wallets: dict[str, KeeperWallet]
    keepers_without_callers: dict[str, set[str]]


@dataclass(frozen=True)
class Issue:
    """One alertable finding, with a stable code for deduplication."""

    code: str
    message: str


def select_keeper_strategies(strategies: Iterable[dict[str, Any]]) -> dict[Chain, list[KeeperStrategy]]:
    """Group live looper and lender-borrower strategies by chain, skipping chains we cannot monitor.

    Args:
        strategies: Strategy rows from ``fetch_kong_strategies``.

    Returns:
        Strategies keyed by chain, deduplicated by address.
    """
    chains_by_id = {chain.chain_id: chain for chain in NATIVE_TOKENS}
    selected: dict[Chain, dict[str, KeeperStrategy]] = {}
    for strategy in strategies:
        name = str(strategy["name"])
        if strategy["is_shutdown"] or not any(marker in name.lower() for marker in KEEPER_STRATEGY_MARKERS):
            continue
        chain = chains_by_id.get(int(strategy["chain_id"]))
        if chain is None:
            logger.info(
                "Skipping strategy %s (%s) on unmonitored chain %s",
                strategy["name"],
                strategy["address"],
                strategy["chain_id"],
            )
            continue
        address = Web3.to_checksum_address(str(strategy["address"]))
        selected.setdefault(chain, {})[address] = KeeperStrategy(chain, address, name)
    return {chain: list(by_address.values()) for chain, by_address in selected.items()}


def latest_allowed_callers(logs: Iterable[Any]) -> dict[str, set[str]]:
    """Replay ``AllowedSet`` logs in order and return the callers still allowed per keeper.

    Args:
        logs: ``eth_getLogs`` entries for the ``AllowedSet`` topic.

    Returns:
        Currently allowed caller addresses keyed by checksummed keeper contract.
    """
    state: dict[str, dict[str, bool]] = {}
    for log in sorted(logs, key=lambda entry: (entry["blockNumber"], entry["logIndex"])):
        keeper = Web3.to_checksum_address(log["address"])
        caller = Web3.to_checksum_address("0x" + bytes(log["topics"][1])[-20:].hex())
        state.setdefault(keeper, {})[caller] = int.from_bytes(bytes(log["topics"][2]), "big") != 0
    return {keeper: {caller for caller, allowed in callers.items() if allowed} for keeper, callers in state.items()}


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
    """Resolve every funded strategy's keeper to the deduplicated set of wallets that pay its gas.

    Args:
        chain: Chain the strategies live on.
        strategies: Keeper-run strategies on ``chain``.

    Returns:
        Keeper wallets (with balances) and keeper contracts that resolved to no wallet.
    """
    client = ChainManager.get_client(chain)

    # 1. keeper() and totalAssets() for every strategy, one batch. Empty strategies need no keeper.
    contracts = [client.get_contract(strategy.address, STRATEGY_ABI) for strategy in strategies]
    results = _batch(
        client, (call for c in contracts for call in (c.functions.keeper().call(), c.functions.totalAssets().call()))
    )
    strategies_by_keeper: dict[str, set[str]] = {}
    for strategy, keeper, total_assets in zip(strategies, results[0::2], results[1::2]):
        if int(total_assets) == 0:
            logger.debug("Skipping %s (%s): no assets", strategy.name, strategy.address)
            continue
        strategies_by_keeper.setdefault(Web3.to_checksum_address(keeper), set()).add(strategy.name)

    # 2. Split unique keepers into wallets and contracts, one batch.
    keeper_is_contract = _is_contract_map(client, list(strategies_by_keeper))
    wallets: dict[str, KeeperWallet] = {}
    contract_keepers = [keeper for keeper, is_contract in keeper_is_contract.items() if is_contract]
    for keeper, is_contract in keeper_is_contract.items():
        if not is_contract:
            wallets.setdefault(keeper, KeeperWallet(keeper)).strategies.update(strategies_by_keeper[keeper])

    # 3. Callers of keeper contracts: one getLogs across all of them, plus configured extras.
    callers_by_keeper: dict[str, set[str]] = {keeper: set() for keeper in contract_keepers}
    if contract_keepers:
        logs = client.execute(
            client.eth.get_logs,
            {"address": contract_keepers, "topics": [ALLOWED_SET_TOPIC], "fromBlock": 0, "toBlock": "latest"},
        )
        for keeper, callers in latest_allowed_callers(logs).items():
            callers_by_keeper.setdefault(keeper, set()).update(callers)
        for keeper, extras in EXTRA_KEEPER_CALLERS.get(chain, {}).items():
            keeper = Web3.to_checksum_address(keeper)
            if keeper in callers_by_keeper:
                callers_by_keeper[keeper].update(Web3.to_checksum_address(extra) for extra in extras)

    # 4. Keep only wallet callers (drop multisigs), one batch over the deduplicated set.
    unique_callers = sorted({caller for callers in callers_by_keeper.values() for caller in callers})
    caller_is_contract = _is_contract_map(client, unique_callers)
    keepers_without_callers: dict[str, set[str]] = {}
    for keeper, callers in callers_by_keeper.items():
        wallet_callers = [caller for caller in callers if not caller_is_contract[caller]]
        if not wallet_callers:
            keepers_without_callers[keeper] = strategies_by_keeper[keeper]
        for caller in wallet_callers:
            wallets.setdefault(caller, KeeperWallet(caller)).strategies.update(strategies_by_keeper[keeper])

    # 5. Native balances for every unique wallet, one batch.
    addresses = list(wallets)
    if addresses:
        for address, balance in zip(addresses, _batch(client, (client.eth.get_balance(a) for a in addresses))):
            wallets[address].balance_wei = int(balance)

    return ChainResult(chain, wallets, keepers_without_callers)


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
    """Return low-balance and missing-caller issues across all chains.

    Args:
        results: Per-chain keeper wallets with balances.
        prices: DeFiLlama USD prices keyed by price key.

    Returns:
        Issues in chain order; empty when every wallet is funded.
    """
    issues: list[Issue] = []
    for result in results:
        chain = result.chain
        symbol, price_key = NATIVE_TOKENS[chain]
        price = prices.get(price_key)
        if price is None or price <= 0:
            issues.append(Issue(f"price:{chain.chain_id}", f"{chain.name}: no {symbol} USD price from DeFiLlama"))
        else:
            issues.extend(_low_balance_issues(result, symbol, price))
        # No-caller issues need no price, so a price outage must not hide them.
        for keeper, strategies in sorted(result.keepers_without_callers.items()):
            issues.append(
                Issue(
                    f"nocaller:{chain.chain_id}:{keeper.lower()}",
                    f"{chain.name}: keeper contract {_address_url(chain, keeper)} has no known wallet caller; "
                    f"add it to EXTRA_KEEPER_CALLERS\n  keeps: {_strategy_list(strategies)}",
                )
            )
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
    lines = ["Strategy Keeper Gas Warning"]
    lines.extend(f"- {issue.message}" for issue in issues)
    return "\n".join(lines)


def main() -> None:
    """Check keeper wallet balances for every live Yearn looper and lender-borrower strategy."""
    parser = argparse.ArgumentParser(description="Check native gas balances of Yearn strategy keeper wallets")
    parser.add_argument("--dry-run", action="store_true", help="Read and evaluate without storing state or alerting")
    args = parser.parse_args()

    strategies_by_chain = select_keeper_strategies(fetch_kong_strategies())
    logger.info("Found %d keeper-run strategies", sum(len(items) for items in strategies_by_chain.values()))

    results: list[ChainResult] = []
    issues: list[Issue] = []
    for chain, strategies in strategies_by_chain.items():
        try:
            results.append(collect_keeper_wallets(chain, strategies))
        except Exception as exc:  # noqa: BLE001 - one broken chain must not hide the others
            logger.exception("Failed to resolve strategy keepers on %s", chain.name)
            issues.append(Issue(f"error:{chain.chain_id}:{type(exc).__name__}", f"{chain.name}: monitor error {exc}"))

    price_keys = sorted({NATIVE_TOKENS[result.chain][1] for result in results})
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

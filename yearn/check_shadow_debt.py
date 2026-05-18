#!/usr/bin/env python3
"""Monitor Yearn v3 vaults for shadow debt issues.

A "shadow debt" issue occurs when a strategy has allocated debt (current_debt > 0)
but is NOT in the vault's default queue. This causes:
  - getWeightedAverageApr() to miss these strategies
  - Reported APR to be incomplete (likely understated)
  - Vault depositors to see inaccurate APR

This script fetches all vaults from yDaemon, queries their default queues and
strategy debt allocations, and alerts if any strategies with debt are missing
from the default queue.
"""

import argparse
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Set

import requests
from dotenv import load_dotenv
from web3 import Web3

from utils.abi import load_abi
from utils.chains import Chain
from utils.logging import get_logger
from utils.telegram import send_telegram_message_with_fallback
from utils.web3_wrapper import ChainManager

load_dotenv()

logger = get_logger("yearn.check_shadow_debt")

PROTOCOL = "yearn"

YDAEMON_BASE_URL = "https://ydaemon.yearn.fi/vaults/v3"
# Get all strategies regardless of queue status - we'll check queue membership on-chain
YDAEMON_PARAMS = "hideAlways=true&strategiesDetails=withDetails"

VAULT_ABI = load_abi("common-abi/YearnV3Vault.json")

# Chains to monitor
CHAINS = [Chain.MAINNET, Chain.POLYGON, Chain.BASE, Chain.ARBITRUM, Chain.KATANA]

# Minimum debt threshold (in tokens) to alert on - ignore dust amounts
# This will be scaled per vault based on decimals
MIN_DEBT_THRESHOLD_TOKENS = Decimal("1")  # 1 token, regardless of decimals

# Strategies allowed to have shadow debt for specific vaults (keyed by chain_id -> vault_address -> set of strategy addresses)
# These strategies intentionally operate outside the default queue and should not trigger alerts
SHADOW_DEBT_WHITELIST: Dict[int, Dict[str, Set[str]]] = {
    Chain.MAINNET.chain_id: {
        "0x696d02db93291651ed510704c9b286841d506987": {  # yvUSD
            "0xf28dc8b6ded7e45f8cf84b9972487c8e1857a442",  # syrupUSDC/USDC Morpho Looper
            "0xb73a2f9f57aaa125ade3a11a1e661d28a919c66d",  # PT siUSD March 25 Morpho Looper
            "0x2f56d106c6df739bdbb777c2fee79ffaed88d179",  # Arbitrum syrupUSDC/USDC Morpho Looper
            "0x7bf1d269bf2cb79e628f51b93763b342fd059d1d",  # PT stcUSD Jul 23 Morpho Looper
            "0x4c0e4d3cb62b91afbbf1fe8e830f98a513c7234b",  # USD3 Pendle PT Maxi
        },
    },
}


@dataclass
class StrategyInfo:
    """Strategy information from vault."""

    address: str
    activation: int
    current_debt: int
    max_debt: int
    in_default_queue: bool


@dataclass
class ShadowDebtIssue:
    """Represents a shadow debt issue for a vault."""

    vault_address: str
    vault_symbol: str
    chain: Chain
    strategies_with_shadow_debt: List[StrategyInfo]
    total_shadow_debt: int
    total_vault_debt: int
    vault_decimals: int


def fetch_ydaemon_vaults(chain: Chain) -> List[Dict]:
    """Fetch vault data from yDaemon API for a given chain.

    Args:
        chain: The chain to fetch vaults for.

    Returns:
        List of vault data dicts with address, symbol, decimals, and strategies.
    """
    url = f"{YDAEMON_BASE_URL}?{YDAEMON_PARAMS}&chainIDs={chain.chain_id}"
    logger.info("Fetching vaults from yDaemon for %s", chain.name)
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    vaults = response.json()

    result = []
    for vault in vaults:
        if "address" not in vault:
            continue

        # Extract strategy addresses from yDaemon data
        # yDaemon returns strategies as an array of strategy objects
        strategies = vault.get("strategies", [])
        all_strategies = [s["address"] for s in strategies if isinstance(s, dict) and "address" in s]

        result.append(
            {
                "address": vault["address"].lower(),
                "symbol": vault.get("symbol", "UNKNOWN"),
                "decimals": vault.get("decimals"),  # Will be validated/fetched on-chain if missing
                "known_strategies": [s.lower() for s in all_strategies],
            }
        )

    logger.info("Found %d vaults on %s", len(result), chain.name)
    return result


def get_vault_decimals_onchain(chain: Chain, vault_address: str) -> int:
    """Query vault decimals on-chain.

    Args:
        chain: The chain to query.
        vault_address: The vault address.

    Returns:
        Number of decimals for the vault token.
    """
    client = ChainManager.get_client(chain)
    vault = client.get_contract(Web3.to_checksum_address(vault_address), VAULT_ABI)
    decimals: int = client.execute(vault.functions.decimals().call)
    return decimals


def get_vault_strategies_onchain(
    chain: Chain, vault_address: str, known_strategies: List[str]
) -> Dict[str, StrategyInfo]:
    """Query on-chain data for all known strategies of a vault.

    Args:
        chain: The chain to query.
        vault_address: The vault address.
        known_strategies: List of strategy addresses to check.

    Returns:
        Dict mapping strategy address to StrategyInfo.
    """
    client = ChainManager.get_client(chain)
    vault = client.get_contract(Web3.to_checksum_address(vault_address), VAULT_ABI)

    # Get default queue
    default_queue_raw = client.execute(vault.functions.get_default_queue().call)
    default_queue: Set[str] = {addr.lower() for addr in default_queue_raw}

    logger.debug(
        "Vault %s has default queue with %d strategies",
        vault_address,
        len(default_queue),
    )

    # Batch query strategy details
    strategies_info: Dict[str, StrategyInfo] = {}

    with client.batch_requests() as batch:
        for strategy_addr in known_strategies:
            batch.add(vault.functions.strategies(Web3.to_checksum_address(strategy_addr)))
        results = batch.execute()

    for strategy_addr, result in zip(known_strategies, results):
        activation, _, current_debt, max_debt = result

        strategies_info[strategy_addr] = StrategyInfo(
            address=strategy_addr,
            activation=activation,
            current_debt=current_debt,
            max_debt=max_debt,
            in_default_queue=(strategy_addr in default_queue),
        )

    return strategies_info


def detect_shadow_debt(
    chain: Chain,
    vault_address: str,
    vault_symbol: str,
    vault_decimals: int,
    strategies_info: Dict[str, StrategyInfo],
    min_debt_threshold: Decimal,
) -> ShadowDebtIssue | None:
    """Detect if a vault has shadow debt issues.

    Args:
        chain: The chain.
        vault_address: Vault address.
        vault_symbol: Vault symbol.
        vault_decimals: Vault decimals.
        strategies_info: Strategy information dict.
        min_debt_threshold: Minimum debt threshold in tokens (will be scaled by vault decimals).

    Returns:
        ShadowDebtIssue if found, None otherwise.
    """
    strategies_with_shadow_debt: List[StrategyInfo] = []
    total_shadow_debt = 0
    total_vault_debt = 0

    # Scale threshold to vault's decimal precision
    threshold_in_wei = min_debt_threshold * Decimal(10) ** vault_decimals

    # Get whitelisted strategies for this vault
    whitelisted_strategies = SHADOW_DEBT_WHITELIST.get(chain.chain_id, {}).get(vault_address, set())

    for strategy_info in strategies_info.values():
        # Only count activated strategies
        if strategy_info.activation == 0:
            continue

        total_vault_debt += strategy_info.current_debt

        # Check if strategy has debt but is not in default queue
        if strategy_info.current_debt > 0 and not strategy_info.in_default_queue:
            # Skip strategies that are whitelisted for shadow debt
            if strategy_info.address in whitelisted_strategies:
                logger.debug(
                    "Skipping whitelisted shadow debt: vault=%s strategy=%s debt=%d",
                    vault_address,
                    strategy_info.address,
                    strategy_info.current_debt,
                )
                continue

            # Apply minimum threshold to avoid alerting on dust
            if Decimal(strategy_info.current_debt) >= threshold_in_wei:
                strategies_with_shadow_debt.append(strategy_info)
                total_shadow_debt += strategy_info.current_debt
                logger.warning(
                    "Shadow debt detected: vault=%s strategy=%s debt=%d",
                    vault_address,
                    strategy_info.address,
                    strategy_info.current_debt,
                )

    if not strategies_with_shadow_debt:
        return None

    return ShadowDebtIssue(
        vault_address=vault_address,
        vault_symbol=vault_symbol,
        chain=chain,
        strategies_with_shadow_debt=strategies_with_shadow_debt,
        total_shadow_debt=total_shadow_debt,
        total_vault_debt=total_vault_debt,
        vault_decimals=vault_decimals,
    )


def format_amount(amount: int, decimals: int) -> str:
    """Format token amount with decimals for display.

    Args:
        amount: Raw token amount.
        decimals: Token decimals.

    Returns:
        Formatted string.
    """
    value = Decimal(amount) / (Decimal(10) ** decimals)
    if value >= 1_000_000:
        return f"{value / 1_000_000:,.2f}M"
    elif value >= 1_000:
        return f"{value / 1_000:,.2f}K"
    else:
        return f"{value:,.4f}"


def print_summary(issues: List[ShadowDebtIssue]) -> None:
    """Print console summary of shadow debt issues.

    Args:
        issues: List of shadow debt issues.
    """
    logger.info("=" * 80)
    logger.info("SHADOW DEBT SUMMARY")
    logger.info("=" * 80)

    # Group by chain
    issues_by_chain: Dict[Chain, List[ShadowDebtIssue]] = {}
    for issue in issues:
        if issue.chain not in issues_by_chain:
            issues_by_chain[issue.chain] = []
        issues_by_chain[issue.chain].append(issue)

    for chain, chain_issues in sorted(issues_by_chain.items(), key=lambda x: x[0].name):
        logger.info("%s:", chain.name)
        logger.info("-" * 80)

        for issue in chain_issues:
            shadow_debt_pct = (
                (issue.total_shadow_debt * 100 / issue.total_vault_debt) if issue.total_vault_debt > 0 else 0
            )

            # Extract token name from vault symbol (e.g., "yvWETH" -> "WETH", "yvUSDC" -> "USDC")
            token_symbol = issue.vault_symbol.replace("yv", "").replace("yvault-", "")

            logger.info("  Vault: %s (%s)", issue.vault_symbol, issue.vault_address)
            logger.info(
                "  Shadow Debt: %s %s (%s%% of total vault debt)",
                format_amount(issue.total_shadow_debt, issue.vault_decimals),
                token_symbol,
                f"{shadow_debt_pct:.1f}",
            )
            logger.info("  Affected Strategies: %d", len(issue.strategies_with_shadow_debt))

            for strategy in issue.strategies_with_shadow_debt:
                logger.info(
                    "    - %s: %s %s",
                    strategy.address,
                    format_amount(strategy.current_debt, issue.vault_decimals),
                    token_symbol,
                )

            logger.info("")

    logger.info("=" * 80)
    logger.info("Total: %d vault(s) with shadow debt issues", len(issues))
    logger.info("=" * 80)


def build_alert_message(issues: List[ShadowDebtIssue]) -> str:
    """Build Telegram alert message for shadow debt issues.

    Args:
        issues: List of shadow debt issues.

    Returns:
        Formatted alert message.
    """
    total_issues = len(issues)
    total_affected_strategies = sum(len(issue.strategies_with_shadow_debt) for issue in issues)

    lines = [
        "🌑 *Shadow Debt Alert*",
        f"Found {total_issues} vault(s) with shadow debt affecting {total_affected_strategies} strateg(ies)\n",
    ]

    # Group by chain
    issues_by_chain: Dict[Chain, List[ShadowDebtIssue]] = {}
    for issue in issues:
        if issue.chain not in issues_by_chain:
            issues_by_chain[issue.chain] = []
        issues_by_chain[issue.chain].append(issue)

    for chain, chain_issues in sorted(issues_by_chain.items(), key=lambda x: x[0].name):
        lines.append(f"*{chain.name}*")

        for issue in chain_issues:
            shadow_debt_pct = (
                (issue.total_shadow_debt * 100 / issue.total_vault_debt) if issue.total_vault_debt > 0 else 0
            )

            explorer_url = chain.explorer_url
            vault_link = f"[{issue.vault_address}]({explorer_url}/address/{issue.vault_address})"

            lines.append(
                f"  • {vault_link} ({issue.vault_symbol}): "
                f"{len(issue.strategies_with_shadow_debt)} strateg(ies) with "
                f"{format_amount(issue.total_shadow_debt, issue.vault_decimals)} debt "
                f"({shadow_debt_pct:.1f}% of total)"
            )

            # List each strategy
            for strategy in issue.strategies_with_shadow_debt:
                strategy_link = f"[{strategy.address}]({explorer_url}/address/{strategy.address})"
                lines.append(f"    - {strategy_link}: {format_amount(strategy.current_debt, issue.vault_decimals)}")

        lines.append("")

    lines.append("⚠️ *Impact:* APR oracle calculations will be incomplete for these vaults")

    return "\n".join(lines)


def check_vault_shadow_debt(chain: Chain, vault_data: Dict, min_debt_threshold: Decimal) -> ShadowDebtIssue | None:
    """Check a single vault for shadow debt issues.

    Args:
        chain: The chain.
        vault_data: Vault data from yDaemon.
        min_debt_threshold: Minimum debt threshold in tokens.

    Returns:
        ShadowDebtIssue if found, None otherwise.
    """
    vault_address = vault_data["address"]
    vault_symbol = vault_data["symbol"]
    vault_decimals = vault_data.get("decimals")
    known_strategies = vault_data["known_strategies"]

    # yvcrvUSD-2 uses shadow debt in normal operation due to high number of strategies, so can ignore
    if chain.chain_id == 1 and vault_address == "0xbf319ddc2edc1eb6fdf9910e39b37be221c8805f":
        logger.debug("Skipping vault %s (yvcrvUSD-2) - uses shadow debt in normal operation", vault_address)
        return None

    if not known_strategies:
        logger.debug("No strategies known for vault %s, skipping", vault_address)
        return None

    try:
        # Verify decimals or fetch from on-chain if missing/invalid
        if vault_decimals is None or not isinstance(vault_decimals, int) or vault_decimals < 0 or vault_decimals > 77:
            logger.warning(
                "Invalid or missing decimals (%s) for vault %s, querying on-chain",
                vault_decimals,
                vault_address,
            )
            vault_decimals = get_vault_decimals_onchain(chain, vault_address)
            logger.info("Vault %s has %d decimals (from on-chain)", vault_address, vault_decimals)

        strategies_info = get_vault_strategies_onchain(chain, vault_address, known_strategies)

        return detect_shadow_debt(
            chain=chain,
            vault_address=vault_address,
            vault_symbol=vault_symbol,
            vault_decimals=vault_decimals,
            strategies_info=strategies_info,
            min_debt_threshold=min_debt_threshold,
        )
    except Exception as e:
        logger.error("Failed to check vault %s on %s: %s", vault_address, chain.name, e)
        return None


def main() -> None:
    """Run shadow debt check across all configured chains."""
    parser = argparse.ArgumentParser(description="Check for shadow debt in Yearn v3 vaults")
    parser.add_argument(
        "--chains",
        type=str,
        default=",".join(c.name for c in CHAINS),
        help="Comma-separated chain names (e.g., MAINNET,BASE)",
    )
    parser.add_argument(
        "--min-debt-threshold",
        type=Decimal,
        default=MIN_DEBT_THRESHOLD_TOKENS,
        help="Minimum debt threshold in tokens to alert on (scaled per vault by decimals)",
    )
    args = parser.parse_args()

    logger.info("Starting shadow debt check")

    # Parse chains
    chain_names = [name.strip().lower() for name in args.chains.split(",")]
    chains_to_check = [Chain.from_name(name) for name in chain_names]

    min_debt_threshold = args.min_debt_threshold

    all_issues: List[ShadowDebtIssue] = []

    for chain in chains_to_check:
        logger.info("Checking chain %s", chain.name)

        try:
            vaults = fetch_ydaemon_vaults(chain)
        except requests.RequestException as e:
            logger.error("Failed to fetch yDaemon data for %s: %s", chain.name, e)
            continue

        if not vaults:
            logger.info("No vaults found on %s", chain.name)
            continue

        for vault_data in vaults:
            issue = check_vault_shadow_debt(chain, vault_data, min_debt_threshold)
            if issue:
                all_issues.append(issue)

    total_issues = len(all_issues)
    logger.info("Check complete. Found %d vault(s) with shadow debt issues", total_issues)

    if not all_issues:
        logger.info("No shadow debt issues detected")
        return

    # Print console summary
    print_summary(all_issues)

    # Send alert
    message = build_alert_message(all_issues)
    total_strategies = sum(len(issue.strategies_with_shadow_debt) for issue in all_issues)
    fallback = (
        f"🌑 *Shadow Debt Alert*\n"
        f"Found {total_issues} vault(s) with shadow debt affecting {total_strategies} strateg(ies).\n"
        f"Too many to list here."
    )
    send_telegram_message_with_fallback(message, PROTOCOL, fallback)


if __name__ == "__main__":
    main()

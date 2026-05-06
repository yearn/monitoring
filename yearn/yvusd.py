"""
yvUSD vault monitoring script.

Monitors:
- APY anomalies: unlocked APY > locked APY inversion, negative strategy APR
- CCTP bridging delays: stale or out-of-sync cross-chain strategy reports
- Flashloan liquidity: available liquidity for looper strategy unwinding (mainnet + cross-chain)
- Large cooldown requests: significant LockedyvUSD cooldown events
"""

import time
from dataclasses import dataclass

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.formatting import format_usd
from utils.http import fetch_json
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager, Web3Client

PROTOCOL = "yearn"
logger = get_logger("yvusd")

CACHE_FILENAME = "cache-id.txt"

# --- ABIs ---
ABI_VAULT = load_abi("yearn/abi/YearnV3Vault.json")
ABI_MORPHO_BLUE = load_abi("morpho/abi/morpho_blue.json")
ABI_LOCKED = load_abi("yearn/abi/LockedYvUSD.json")

# --- Contract Addresses ---
YVUSD_VAULT = "0x696d02Db93291651ED510704c9b286841d506987"
LOCKED_YVUSD = "0xAaaFEa48472f77563961Cdb53291DEDfB46F9040"

# Per-chain Morpho Blue + flashloan source addresses for looper unwinding checks.
LOOPER_CHAIN_CONFIG: dict[Chain, dict[str, str]] = {
    Chain.MAINNET: {
        "morpho": "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb",
        "balancer_vault": "0xBA12222222228d8Ba445958a75a0704d566BF2C8",
        "usdc": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    },
    Chain.ARBITRUM: {
        "morpho": "0x6c247b1F6182318877311737BaC0844bAa518F5e",
        "balancer_vault": "0xBA12222222228d8Ba445958a75a0704d566BF2C8",
        "usdc": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    },
}

# --- API ---
YVUSD_API_URL = "https://yvusd-api.yearn.fi/api/aprs"

# --- Thresholds ---
APY_INVERSION_HOURS = 6  # Alert after this many hours of unlocked APY > locked APY
CCTP_REPORT_STALENESS_HOURS = 48  # Report freshness threshold
CCTP_REPORT_SKEW_HOURS = 6  # Max allowed skew between local and remote reports
LARGE_COOLDOWN_THRESHOLD = 100_000  # $100K in USD

USDC_DECIMALS = 6
ONE_USDC = 10**USDC_DECIMALS

# --- Cache Keys ---
CACHE_KEY_APY_INVERSION_START = "YVUSD_APY_INVERSION_START"
CACHE_KEY_APY_INVERSION_ALERTED = "YVUSD_APY_INVERSION_ALERTED"
CACHE_KEY_LAST_BLOCK = "YVUSD_LAST_BLOCK"

# Number of blocks to scan per run (~1 hour at 12s/block)
BLOCKS_PER_HOUR = 300
MAX_SCAN_BLOCKS = 5000

# Minimal ERC20 ABI for balanceOf
ABI_ERC20_BALANCE = [
    {
        "type": "function",
        "name": "balanceOf",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    }
]

# Minimal V3 tokenized strategy ABI used for remote-side health checks.
# Remote "vaults" exposed by the cross-chain strategy metadata are actually V3
# tokenized strategies (no strategies() mapping); they expose lastReport() and
# totalAssets() directly.
ABI_TOKENIZED_STRATEGY = [
    {
        "type": "function",
        "name": "lastReport",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "totalAssets",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]

# Strategy types that use Morpho leverage and need flashloans to unwind
LOOPER_STRATEGY_TYPES = ("morpho-looper", "pt-morpho-looper")


@dataclass(frozen=True)
class LooperPosition:
    """A Morpho-looper borrow position to monitor for flashloan unwind capacity."""

    chain: Chain
    market_id: str  # 0x-prefixed hex
    borrower: str  # address of the contract holding the Morpho borrow
    name: str  # human-readable label
    mainnet_strategy: str  # mainnet yvUSD strategy that owns this position (for explorer link)


def get_cache_value(key: str) -> float:
    """Read a cached float value, returns 0 if not found."""
    val = get_last_value_for_key_from_file(CACHE_FILENAME, key)
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def set_cache_value(key: str, value: float) -> None:
    """Write a float value to cache."""
    write_last_value_to_file(CACHE_FILENAME, key, value)


def check_apy_anomalies(api_data: dict) -> None:
    """Check for APY anomalies using the yvUSD API.

    Alerts when:
    - Unlocked APY > locked APY for more than APY_INVERSION_HOURS
    - Any active strategy has negative APR
    """
    yvusd_data = api_data.get(YVUSD_VAULT)
    locked_data = api_data.get(LOCKED_YVUSD)

    if not yvusd_data or not locked_data:
        logger.error("Missing vault data in API response")
        send_alert(Alert(AlertSeverity.MEDIUM, "Missing vault data in yvUSD API response", PROTOCOL))
        return

    unlocked_apy = yvusd_data.get("apy", 0)
    locked_apy = locked_data.get("apy", 0)
    logger.info("APY — Unlocked: %.2f%%, Locked: %.2f%%", unlocked_apy * 100, locked_apy * 100)

    _check_apy_inversion(unlocked_apy, locked_apy)
    _check_negative_strategy_apr(yvusd_data)


def _check_apy_inversion(unlocked_apy: float, locked_apy: float) -> None:
    """Alert if unlocked APY exceeds locked APY for more than APY_INVERSION_HOURS."""
    now = time.time()

    if unlocked_apy > locked_apy:
        inversion_start = get_cache_value(CACHE_KEY_APY_INVERSION_START)
        if inversion_start == 0:
            set_cache_value(CACHE_KEY_APY_INVERSION_START, now)
            logger.warning(
                "APY inversion detected: unlocked (%.2f%%) > locked (%.2f%%)",
                unlocked_apy * 100,
                locked_apy * 100,
            )
        else:
            hours_inverted = (now - inversion_start) / 3600
            already_alerted = get_cache_value(CACHE_KEY_APY_INVERSION_ALERTED)
            if hours_inverted >= APY_INVERSION_HOURS and not already_alerted:
                message = (
                    f"*yvUSD APY Inversion Alert*\n"
                    f"Unlocked APY ({unlocked_apy:.2%}) > Locked APY ({locked_apy:.2%})\n"
                    f"Inverted for {hours_inverted:.1f} hours\n"
                    f"Locked users are earning less than unlocked — incentive misalignment\n"
                    f"[yvUSD Vault](https://etherscan.io/address/{YVUSD_VAULT})"
                )
                send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))
                set_cache_value(CACHE_KEY_APY_INVERSION_ALERTED, 1)
    else:
        # Inversion resolved — reset tracking
        if get_cache_value(CACHE_KEY_APY_INVERSION_START) > 0:
            set_cache_value(CACHE_KEY_APY_INVERSION_START, 0)
            set_cache_value(CACHE_KEY_APY_INVERSION_ALERTED, 0)
            logger.info("APY inversion resolved")


def _check_negative_strategy_apr(yvusd_data: dict) -> None:
    """Alert if any active strategy has a negative APR."""
    strategies = yvusd_data.get("meta", {}).get("strategies", [])

    for strategy in strategies:
        apr_raw = int(strategy.get("apr_raw", "0"))
        debt = int(strategy.get("debt", "0"))
        name = strategy.get("meta", {}).get("name", strategy.get("address", "unknown"))
        address = strategy.get("address", "unknown")

        if debt > 0 and apr_raw < 0:
            apr_pct = apr_raw / 1e18 * 100
            debt_usd = debt / ONE_USDC
            message = (
                f"*yvUSD Negative Strategy APR*\n"
                f"{name}: {apr_pct:.2f}% APR\n"
                f"Debt: {format_usd(debt_usd)}\n"
                f"Strategy is losing money\n"
                f"[Strategy](https://etherscan.io/address/{address})"
            )
            send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))


def check_strategy_staleness(client: Web3Client, api_data: dict) -> None:
    """Check cross-chain strategy report freshness.

    Alerts when a CCTP cross-chain strategy or its remote counterpart:
    - has not reported in more than CCTP_REPORT_STALENESS_HOURS, or
    - is out of sync with the other side by more than CCTP_REPORT_SKEW_HOURS
    """
    strategies = api_data.get(YVUSD_VAULT, {}).get("meta", {}).get("strategies", [])
    cross_chain = [s for s in strategies if s.get("meta", {}).get("type") == "cross-chain"]

    if not cross_chain:
        logger.info("No cross-chain strategies found")
        return

    vault = client.eth.contract(address=YVUSD_VAULT, abi=ABI_VAULT)

    with client.batch_requests() as batch:
        for strategy in cross_chain:
            batch.add(vault.functions.strategies(strategy["address"]))
        responses = client.execute_batch(batch)

    if len(responses) != len(cross_chain):
        logger.error("Unexpected batch response count for strategy staleness check")
        return

    now = int(time.time())

    for strategy, local_state in zip(cross_chain, responses, strict=False):
        activation, local_last_report, local_debt, _ = local_state
        name = strategy.get("meta", {}).get("name", strategy["address"])
        address = strategy["address"]
        meta = strategy.get("meta", {})
        remote_chain_id = meta.get("remote_chain_id")
        remote_vault = meta.get("remote_vault")

        if activation == 0 or not remote_chain_id or not remote_vault:
            continue

        try:
            remote_chain = Chain.from_chain_id(remote_chain_id)
        except ValueError:
            logger.error("Unknown remote chain_id %s for strategy %s", remote_chain_id, name)
            send_alert(
                Alert(
                    AlertSeverity.MEDIUM,
                    f"yvUSD CCTP: unknown remote chain_id {remote_chain_id} for {name}",
                    PROTOCOL,
                )
            )
            continue

        remote_state = _fetch_remote_strategy_state(remote_chain, remote_vault, name)
        if remote_state is None:
            continue
        remote_last_report, remote_debt = remote_state

        local_hours_since = (now - local_last_report) / 3600
        remote_hours_since = (now - remote_last_report) / 3600
        report_skew_hours = abs(local_last_report - remote_last_report) / 3600
        local_debt_usd = local_debt / ONE_USDC
        remote_debt_usd = remote_debt / ONE_USDC

        logger.info(
            "CCTP strategy %s — local report: %.1fh, remote report: %.1fh, skew: %.1fh, local debt: %s, remote debt: %s",
            name,
            local_hours_since,
            remote_hours_since,
            report_skew_hours,
            format_usd(local_debt_usd),
            format_usd(remote_debt_usd),
        )

        alert_lines = _build_cctp_alert_lines(
            name=name,
            local_chain=Chain.MAINNET,
            local_last_report=local_last_report,
            local_debt=local_debt,
            remote_chain=remote_chain,
            remote_last_report=remote_last_report,
            remote_debt=remote_debt,
            now=now,
        )
        if alert_lines:
            message = (
                "*yvUSD CCTP Bridge Health Alert*\n" + "\n".join(alert_lines) + "\n"
                f"[Strategy](https://etherscan.io/address/{address})"
            )
            send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))


def _fetch_remote_strategy_state(remote_chain: Chain, remote_vault: str, name: str) -> tuple[int, int] | None:
    """Fetch (lastReport, totalAssets) for a remote V3 tokenized strategy.

    Returns None and surfaces a MEDIUM alert if the lookup fails — silent
    skips would let the CCTP health check disable itself for misconfigured
    strategies without anyone noticing.
    """
    try:
        remote_client = ChainManager.get_client(remote_chain)
        remote_strategy = remote_client.eth.contract(address=remote_vault, abi=ABI_TOKENIZED_STRATEGY)
        last_report = remote_strategy.functions.lastReport().call()
        total_assets = remote_strategy.functions.totalAssets().call()
        return int(last_report), int(total_assets)
    except Exception as e:
        explorer = remote_chain.explorer_url or ""
        link = f"{explorer}/address/{remote_vault}" if explorer else remote_vault
        logger.error("Failed to fetch remote state for %s on %s: %s", name, remote_chain.network_name, e)
        send_alert(
            Alert(
                AlertSeverity.MEDIUM,
                (
                    f"*yvUSD CCTP Remote Lookup Failed*\n"
                    f"{name} on {remote_chain.network_name}\n"
                    f"Remote vault: {link}\n"
                    f"Error: {e}\n"
                    f"CCTP health check is unable to verify this strategy"
                ),
                PROTOCOL,
            )
        )
        return None


def _build_cctp_alert_lines(
    *,
    name: str,
    local_chain: Chain,
    local_last_report: int,
    local_debt: int,
    remote_chain: Chain,
    remote_last_report: int,
    remote_debt: int,
    now: int,
) -> list[str]:
    local_hours_since = (now - local_last_report) / 3600
    remote_hours_since = (now - remote_last_report) / 3600
    report_skew_hours = abs(local_last_report - remote_last_report) / 3600
    has_position = local_debt > 0 or remote_debt > 0
    if not has_position:
        return []

    problems = []
    if local_debt > 0 and local_hours_since > CCTP_REPORT_STALENESS_HOURS:
        problems.append(f"{local_chain.network_name} report stale: {local_hours_since:.1f}h")
    if remote_debt > 0 and remote_hours_since > CCTP_REPORT_STALENESS_HOURS:
        problems.append(f"{remote_chain.network_name} report stale: {remote_hours_since:.1f}h")
    if report_skew_hours > CCTP_REPORT_SKEW_HOURS:
        newer_chain = local_chain if local_last_report >= remote_last_report else remote_chain
        problems.append(
            f"report skew {report_skew_hours:.1f}h ({newer_chain.network_name} is newer than the other side)"
        )

    if not problems:
        return []

    return [
        name,
        *problems,
        f"Mainnet last report: {local_hours_since:.1f}h ago, debt: {format_usd(local_debt / ONE_USDC)}",
        f"{remote_chain.network_name.title()} last report: {remote_hours_since:.1f}h ago, debt: {format_usd(remote_debt / ONE_USDC)}",
        "Bridge accounting may be delayed or unsynced",
    ]


def _collect_looper_positions(strategies: list[dict]) -> list[LooperPosition]:
    """Discover all active Morpho-looper borrow positions from the API metadata.

    Includes both:
    - Direct mainnet looper strategies (type in LOOPER_STRATEGY_TYPES)
    - Cross-chain wrappers where the remote side is itself a looper
      (remote_vault_type in LOOPER_STRATEGY_TYPES); the borrower on Morpho is
      the remote tokenized strategy (`remote_vault`).
    """
    positions: list[LooperPosition] = []

    for s in strategies:
        meta = s.get("meta", {}) or {}
        type_ = meta.get("type")
        debt = int(s.get("debt", "0"))
        name = meta.get("name", s.get("address", "unknown"))
        address = s.get("address", "")

        if debt <= 0:
            continue

        if type_ in LOOPER_STRATEGY_TYPES:
            market_id = meta.get("market_id")
            if not market_id:
                continue
            positions.append(
                LooperPosition(
                    chain=Chain.MAINNET,
                    market_id=market_id,
                    borrower=address,
                    name=name,
                    mainnet_strategy=address,
                )
            )
            continue

        if type_ != "cross-chain":
            continue

        if meta.get("remote_vault_type") not in LOOPER_STRATEGY_TYPES:
            continue

        remote_chain_id = meta.get("remote_chain_id")
        remote_vault = meta.get("remote_vault")
        remote_meta = meta.get("remote_meta") or {}
        market_id = remote_meta.get("market_id")

        if not (remote_chain_id and remote_vault and market_id):
            logger.warning("Cross-chain looper %s missing remote market metadata; skipping", name)
            continue

        try:
            remote_chain = Chain.from_chain_id(remote_chain_id)
        except ValueError:
            logger.error("Cross-chain looper %s on unknown chain_id %s; skipping", name, remote_chain_id)
            send_alert(
                Alert(
                    AlertSeverity.MEDIUM,
                    f"yvUSD: cross-chain looper {name} references unknown chain_id {remote_chain_id}",
                    PROTOCOL,
                )
            )
            continue

        positions.append(
            LooperPosition(
                chain=remote_chain,
                market_id=market_id,
                borrower=remote_vault,
                name=name,
                mainnet_strategy=address,
            )
        )

    return positions


def check_flashloan_liquidity(api_data: dict) -> None:
    """Check available flashloan liquidity for looper strategy unwinding.

    Compares each looper strategy's Morpho borrow position against available
    flashloan liquidity from the chain's Balancer vault and the Morpho market.
    Cross-chain loopers are checked on their remote chain (where the actual
    leverage and unwind liquidity live).
    """
    strategies = api_data.get(YVUSD_VAULT, {}).get("meta", {}).get("strategies", [])
    positions = _collect_looper_positions(strategies)

    if not positions:
        logger.info("No active Morpho looper positions found")
        return

    by_chain: dict[Chain, list[LooperPosition]] = {}
    for p in positions:
        by_chain.setdefault(p.chain, []).append(p)

    for chain, chain_positions in by_chain.items():
        config = LOOPER_CHAIN_CONFIG.get(chain)
        if not config:
            logger.error("No looper config for chain %s; %d positions uncovered", chain.name, len(chain_positions))
            send_alert(
                Alert(
                    AlertSeverity.MEDIUM,
                    (
                        f"yvUSD: flashloan liquidity check unsupported on {chain.network_name} "
                        f"({len(chain_positions)} looper position(s) uncovered)"
                    ),
                    PROTOCOL,
                )
            )
            continue

        try:
            _check_chain_flashloan_liquidity(chain, chain_positions, config)
        except Exception as e:
            logger.error("Flashloan liquidity check failed on %s: %s", chain.name, e)
            send_alert(
                Alert(
                    AlertSeverity.MEDIUM,
                    f"yvUSD flashloan liquidity check failed on {chain.network_name}: {e}",
                    PROTOCOL,
                )
            )


def _check_chain_flashloan_liquidity(chain: Chain, positions: list[LooperPosition], config: dict[str, str]) -> None:
    """Run the flashloan liquidity check for all positions on a single chain."""
    client = ChainManager.get_client(chain)
    morpho = client.eth.contract(address=config["morpho"], abi=ABI_MORPHO_BLUE)
    usdc = client.eth.contract(address=config["usdc"], abi=ABI_ERC20_BALANCE)

    with client.batch_requests() as batch:
        for p in positions:
            market_id = bytes.fromhex(p.market_id[2:])
            batch.add(morpho.functions.market(market_id))
            batch.add(morpho.functions.position(market_id, p.borrower))
        batch.add(usdc.functions.balanceOf(config["balancer_vault"]))
        responses = client.execute_batch(batch)

    expected = len(positions) * 2 + 1
    if len(responses) != expected:
        logger.error("Unexpected batch response count on %s: got %d, expected %d", chain.name, len(responses), expected)
        return

    balancer_usdc = responses[-1] / ONE_USDC
    logger.info("[%s] Balancer vault USDC balance: %s", chain.name, format_usd(balancer_usdc))

    explorer = chain.explorer_url or "https://etherscan.io"

    for i, p in enumerate(positions):
        market_data = responses[i * 2]
        position_data = responses[i * 2 + 1]

        total_supply_assets = market_data[0]
        total_borrow_assets = market_data[2]
        total_borrow_shares = market_data[3]
        borrow_shares = position_data[1]

        # Convert borrow shares to assets
        if total_borrow_shares > 0 and borrow_shares > 0:
            borrow_assets = borrow_shares * total_borrow_assets // total_borrow_shares
        else:
            borrow_assets = 0

        borrow_usd = borrow_assets / ONE_USDC
        market_liquidity = (total_supply_assets - total_borrow_assets) / ONE_USDC

        logger.info(
            "[%s] Looper %s — borrow: %s, market liquidity: %s",
            chain.name,
            p.name,
            format_usd(borrow_usd),
            format_usd(market_liquidity),
        )

        if borrow_assets == 0:
            continue

        # Strategy needs to flashloan approximately borrow_assets to unwind.
        # Alert if neither Balancer vault nor Morpho market has sufficient liquidity.
        if balancer_usdc < borrow_usd and market_liquidity < borrow_usd:
            links = [f"[Strategy](https://etherscan.io/address/{p.mainnet_strategy})"]
            if p.chain != Chain.MAINNET:
                links.append(f"[Borrower on {p.chain.network_name}]({explorer}/address/{p.borrower})")
            message = (
                f"*yvUSD Flashloan Liquidity Warning*\n"
                f"{p.name} ({p.chain.network_name})\n"
                f"Borrow position: {format_usd(borrow_usd)}\n"
                f"Balancer flashloan available: {format_usd(balancer_usdc)}\n"
                f"Morpho market liquidity: {format_usd(market_liquidity)}\n"
                f"Insufficient flashloan liquidity for strategy unwinding\n" + " | ".join(links)
            )
            send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))


def check_large_cooldowns(client: Web3Client) -> None:
    """Check for large cooldown requests on LockedyvUSD.

    Scans recent blocks for CooldownStarted events exceeding LARGE_COOLDOWN_THRESHOLD.
    """
    locked = client.eth.contract(address=LOCKED_YVUSD, abi=ABI_LOCKED)

    current_block = client.eth.block_number
    last_block = int(get_cache_value(CACHE_KEY_LAST_BLOCK))

    if last_block == 0:
        from_block = current_block - BLOCKS_PER_HOUR
    else:
        from_block = last_block + 1

    if from_block >= current_block:
        logger.info("No new blocks to scan for cooldown events")
        set_cache_value(CACHE_KEY_LAST_BLOCK, current_block)
        return

    # Cap scan range to avoid hitting RPC limits
    if current_block - from_block > MAX_SCAN_BLOCKS:
        from_block = current_block - MAX_SCAN_BLOCKS
        logger.warning("Capped scan range to last %d blocks", MAX_SCAN_BLOCKS)

    logger.info("Scanning blocks %d to %d for cooldown events", from_block, current_block)

    try:
        events = locked.events.CooldownStarted.get_logs(from_block=from_block, to_block=current_block)
    except Exception as e:
        logger.warning("Could not fetch CooldownStarted events: %s", e)
        return

    large_count = 0
    for event in events:
        shares = event["args"]["shares"]
        owner = event["args"]["user"]
        # yvUSD shares are roughly 1:1 with USDC (PPS ~ 1.004)
        shares_usd = shares / ONE_USDC

        if shares_usd >= LARGE_COOLDOWN_THRESHOLD:
            large_count += 1
            message = (
                f"*yvUSD Large Cooldown Request*\n"
                f"{format_usd(shares_usd)} cooldown requested\n"
                f"Owner: [{owner}](https://etherscan.io/address/{owner})\n"
                f"Cooldown period: 14 days\n"
                f"Large withdrawal incoming — may impact vault liquidity\n"
                f"[LockedyvUSD](https://etherscan.io/address/{LOCKED_YVUSD})"
            )
            send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL))

    logger.info("Found %d cooldown events (%d large)", len(events), large_count)
    set_cache_value(CACHE_KEY_LAST_BLOCK, current_block)


def main() -> None:
    """Run all yvUSD monitoring checks."""
    logger.info("Starting yvUSD monitoring...")

    client = ChainManager.get_client(Chain.MAINNET)

    try:
        api_data = fetch_json(YVUSD_API_URL)
        if api_data:
            check_apy_anomalies(api_data)
            check_strategy_staleness(client, api_data)
            check_flashloan_liquidity(api_data)
        else:
            send_alert(Alert(AlertSeverity.MEDIUM, "Failed to fetch yvUSD API data", PROTOCOL))
    except Exception as e:
        logger.error("Error during yvUSD API checks: %s", e)
        send_alert(Alert(AlertSeverity.MEDIUM, f"yvUSD API checks failed: {e}", PROTOCOL))

    try:
        check_large_cooldowns(client)
    except Exception as e:
        logger.error("Error during cooldown check: %s", e)
        send_alert(Alert(AlertSeverity.MEDIUM, f"yvUSD cooldown check failed: {e}", PROTOCOL))

    logger.info("yvUSD monitoring complete")


if __name__ == "__main__":
    main()

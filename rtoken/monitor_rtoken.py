from dataclasses import dataclass

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import get_last_queued_id_from_file, write_last_queued_id_to_file
from utils.chains import Chain
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

PROTOCOL = "ethplus"
CHANNEL = "rtoken"
logger = get_logger(CHANNEL)

# Load ABIs once
ABI_RTOKEN = load_abi("rtoken/abi/rtoken.json")
ABI_STRSR = load_abi("rtoken/abi/strsr.json")


@dataclass
class RTokenConfig:
    """Configuration for RToken monitoring on a specific chain."""

    rtoken_address: str
    strsr_address: str
    coverage_threshold: float
    redemption_threshold: int  # in wei

    def get_cache_key(self, chain: Chain) -> str:
        """Get cache key for StRSR rate on this chain."""
        # if there are more protocols on the same chain, add the protocol name to the cache key
        return f"rtoken+strsr+rate+{chain.network_name}+{PROTOCOL}"


def get_rtoken_config(chain: Chain) -> RTokenConfig:
    """
    Get RToken configuration for the specified chain.

    Args:
        chain: The blockchain to get configuration for

    Returns:
        RTokenConfig containing addresses and thresholds for the chain
    """
    if chain == Chain.MAINNET:
        # https://app.reserve.org/ethereum/token/0xe72b141df173b999ae7c1adcbf60cc9833ce56a8/overview
        return RTokenConfig(
            rtoken_address="0xE72B141DF173b999AE7c1aDcbF60Cc9833Ce56a8",
            strsr_address="0xffa151Ad0A0e2e40F39f9e5E9F87cF9E45e819dd",
            coverage_threshold=1.05,
            redemption_threshold=1000 * 10**18,
        )
    elif chain == Chain.BASE:
        # https://app.reserve.org/base/token/0xCb327b99fF831bF8223cCEd12B1338FF3aA322Ff/overview
        return RTokenConfig(
            rtoken_address="0xCb327b99fF831bF8223cCEd12B1338FF3aA322Ff",
            strsr_address="0x3D190D968a8985673285B3B9cD5f5BDC12c9b368",
            coverage_threshold=1.03,
            redemption_threshold=600 * 10**18,
        )
    else:
        raise ValueError(f"RToken monitoring not supported for chain: {chain.network_name}")


def monitor_rtoken_on_chain(chain: Chain):
    """
    Monitor RToken metrics on the specified chain.

    Args:
        chain: The blockchain to monitor
    """
    logger.info("Monitoring RToken on %s", chain.network_name)

    # NOTE: propagate errors to the caller
    config = get_rtoken_config(chain)
    client = ChainManager.get_client(chain)
    rtoken = client.eth.contract(address=config.rtoken_address, abi=ABI_RTOKEN)
    strsr = client.eth.contract(address=config.strsr_address, abi=ABI_STRSR)

    basket_needed = None
    total_supply = None
    current_rate = None
    redemption_available = None

    # --- Combined Blockchain Calls ---
    with client.batch_requests() as batch:
        # Add RToken calls
        batch.add(rtoken.functions.basketsNeeded())
        batch.add(rtoken.functions.totalSupply())
        batch.add(rtoken.functions.redemptionAvailable())
        # Add StRSR call
        batch.add(strsr.functions.exchangeRate())

        responses = client.execute_batch(batch)

        if len(responses) == 4:
            basket_needed, total_supply, redemption_available, current_rate = responses
            logger.info(
                "[%s] Raw Data - Basket Needed: %s, Total Supply: %s, Redemption Available: %s, StRSR Rate: %s",
                chain.network_name,
                basket_needed,
                total_supply,
                redemption_available,
                current_rate,
            )

            # Validate response types
            if not isinstance(basket_needed, int) or not isinstance(total_supply, int):
                logger.warning(
                    "[%s] Received non-integer values from RToken contract. Basket: %s, Supply: %s",
                    chain.network_name,
                    basket_needed,
                    total_supply,
                )
                basket_needed = None
                total_supply = None
            if not isinstance(redemption_available, int):
                logger.warning(
                    "[%s] Received non-integer value from RToken contract redemptionAvailable: %s",
                    chain.network_name,
                    redemption_available,
                )
                redemption_available = None
            if not isinstance(current_rate, int):
                logger.warning(
                    "[%s] Received non-integer value from StRSR contract exchangeRate: %s",
                    chain.network_name,
                    current_rate,
                )
                current_rate = None

        else:
            error_message = f"[{chain.network_name}] Batch Call: Expected 4 responses, got {len(responses)}"
            logger.error("%s", error_message)
            send_alert(Alert(AlertSeverity.LOW, error_message, PROTOCOL, channel=CHANNEL))
            return

    # --- RToken Coverage Check ---
    if basket_needed is not None and total_supply is not None:
        if total_supply == 0:
            send_alert(
                Alert(
                    AlertSeverity.LOW,
                    f"⚠️ Warning: totalSupply is zero on {chain.network_name}.",
                    PROTOCOL,
                    channel=CHANNEL,
                )
            )
        else:
            coverage = basket_needed / total_supply
            logger.info("[%s] RToken Coverage: %s", chain.network_name, f"{coverage:.4f}")
            if coverage < config.coverage_threshold:
                message = (
                    f"🚨 *{PROTOCOL} {chain.network_name.upper()} Alert* 🚨\\n"
                    f"RToken coverage below threshold!\\n"
                    f"Current Coverage: {coverage:.4f}\\n"
                    f"Threshold: {config.coverage_threshold:.2f}\\n"
                    f"Total Supply: {total_supply / 1e18:.4f}\\n"
                    f"Basket Needed: {basket_needed / 1e18:.4f}"
                )
                send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL, channel=CHANNEL))
    else:
        send_alert(
            Alert(
                AlertSeverity.LOW,
                f"Skipping RToken coverage check due to invalid data from batch on {chain.network_name}.",
                PROTOCOL,
                channel=CHANNEL,
            )
        )

    # --- StRSR Exchange Rate Check ---
    if current_rate is not None:
        logger.info("[%s] StRSR Current Exchange Rate: %s", chain.network_name, current_rate)
        cache_key = config.get_cache_key(chain)
        initial_rate = get_last_queued_id_from_file(cache_key)

        if initial_rate == 0:
            logger.info("[%s] Saving initial StRSR exchange rate to cache: %s", chain.network_name, current_rate)
        elif current_rate < initial_rate:
            message = (
                f"🚨 *{PROTOCOL} {chain.network_name.upper()} Alert* 🚨\\n"
                f"StRSR exchange rate dropped below initial value!\\n"
                f"Current Rate: {current_rate}\\n"
                f"Initial Rate (from cache): {initial_rate}"
            )
            send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL, channel=CHANNEL))
        write_last_queued_id_to_file(cache_key, current_rate)
    else:
        send_alert(
            Alert(
                AlertSeverity.LOW,
                f"Skipping StRSR exchange rate check due to invalid data from batch on {chain.network_name}.",
                PROTOCOL,
                channel=CHANNEL,
            )
        )

    # --- RToken Redemption Available Check ---
    if redemption_available is not None:
        logger.info("[%s] RToken Redemption Available: %s", chain.network_name, redemption_available)
        if redemption_available < config.redemption_threshold:
            threshold_eth = config.redemption_threshold / 1e18
            logger.warning("[%s] redemptionAvailable is less than %s ETH.", chain.network_name, f"{threshold_eth:.0f}")
            message = (
                f"🚨 *{PROTOCOL} {chain.network_name.upper()} Alert* 🚨\\n"
                f"RToken redemptionAvailable is less than {threshold_eth:.0f} ETH!\\n"
                f"Redemption Available: {redemption_available / 1e18:.4f} ETH\\n"
                f"Threshold: {threshold_eth:.0f} ETH\\n"
            )
            send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL, channel=CHANNEL))
    else:
        send_alert(
            Alert(
                AlertSeverity.LOW,
                f"Skipping RToken redemptionAvailable check due to invalid data from batch on {chain.network_name}.",
                PROTOCOL,
                channel=CHANNEL,
            )
        )


def main():
    """
    Main function to monitor RToken on multiple chains.
    """
    # Define chains to monitor
    chains_to_monitor = [Chain.MAINNET]

    for chain in chains_to_monitor:
        try:
            monitor_rtoken_on_chain(chain)
        except Exception as e:
            error_message = f"Critical error monitoring on {chain.network_name}. Check the logs."
            logger.error("%s\n%s", error_message, e)
            send_alert(Alert(AlertSeverity.LOW, error_message, PROTOCOL, channel=CHANNEL))


if __name__ == "__main__":
    main()

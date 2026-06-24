from dataclasses import dataclass

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import get_last_queued_id_from_file, write_last_queued_id_to_file
from utils.chains import Chain
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager

REDEEM_VALUE = int(1e18)
PROTOCOL = "origin"
CHANNEL = "pegs"
logger = get_logger("lrt-pegs.origin")

# Load Origin Vault ABI
ABI_ORIGIN_VAULT = load_abi("protocols/lrt-pegs/abi/OriginVault.json")
ABI_ORIGIN_VAULT_BASE = load_abi("protocols/lrt-pegs/abi/OriginVaultBase.json")
ABI_ORIGIN_WRAPPED_OETH = load_abi("common-abi/YearnV3Vault.json")  # ERC4626
ABI_ERC20 = load_abi("common-abi/ERC20.json")

ORIGIN_CONFIGS = {
    Chain.BASE: {
        "vault_address": "0x98a0CbeF61bD2D21435f433bE4CD42B56B38CC93",
        "oeth_address": "0xDBFeFD2e8460a6Ee4955A68582F85708BAEA60A3",
        "wrapped_oeth_address": "0x7FcD174E80f264448ebeE8c88a7C4476AAF58Ea6",
    },
    Chain.MAINNET: {
        "vault_address": "0x39254033945AA2E4809Cc2977E7087BEE48bd7Ab",
        "oeth_address": "0x856c4Efb76C1D1AE02e20CEB03A2A6a08b0b8dC3",
        "wrapped_oeth_address": "0xDcEe70654261AF21C44c093C300eD3Bb97b78192",
    },
}


@dataclass
class OriginMetrics:
    redeem_value: int
    wrapped_oeth_redeem_value: int
    backing_ratio: int
    total_value: int
    total_supply: int


def get_cache_key(chain: Chain) -> str:
    """Get cache key for redeem value on this chain."""
    return f"origin_redeem_{chain.network_name}"


# Mainnet and Base use different vault implementations/ABIs:
# - Mainnet vault exposes priceUnitRedeem() for an explicit redeem quote.
# - Base vault does not expose priceUnitRedeem(), so we use fixed 1e18 redeem value
#   and validate health via totalValue/totalSupply backing ratio instead.
def _fetch_base_metrics(client, chain: Chain, config: dict, wrapped_oeth):
    vault = client.eth.contract(address=config["vault_address"], abi=ABI_ORIGIN_VAULT_BASE)
    o_token = client.eth.contract(address=config["oeth_address"], abi=ABI_ERC20)
    with client.batch_requests() as batch:
        batch.add(vault.functions.totalValue())
        batch.add(wrapped_oeth.functions.convertToAssets(REDEEM_VALUE))
        batch.add(o_token.functions.totalSupply())
        responses = client.execute_batch(batch)
        if not len(responses) == 3:
            logger.error("Failed to fetch metrics on %s", chain.name)
            return None
        total_value = responses[0]
        wrapped_oeth_redeem_value = responses[1]
        total_supply = responses[2]

    backing_ratio = REDEEM_VALUE if total_supply == 0 else (total_value * REDEEM_VALUE) // total_supply
    return OriginMetrics(
        redeem_value=REDEEM_VALUE,
        wrapped_oeth_redeem_value=wrapped_oeth_redeem_value,
        backing_ratio=backing_ratio,
        total_value=total_value,
        total_supply=total_supply,
    )


def _fetch_mainnet_metrics(client, chain: Chain, config: dict, wrapped_oeth):
    # NOTE: priceUnitRedeem was removed from the mainnet vault contract.
    # Use the same approach as Base: fixed 1e18 redeem value + backing ratio.
    vault = client.eth.contract(address=config["vault_address"], abi=ABI_ORIGIN_VAULT)
    o_token = client.eth.contract(address=config["oeth_address"], abi=ABI_ERC20)
    with client.batch_requests() as batch:
        batch.add(vault.functions.totalValue())
        batch.add(wrapped_oeth.functions.convertToAssets(REDEEM_VALUE))
        batch.add(o_token.functions.totalSupply())
        responses = client.execute_batch(batch)
        if not len(responses) == 3:
            logger.error("Failed to fetch metrics on %s", chain.name)
            return None
        total_value = responses[0]
        wrapped_oeth_redeem_value = responses[1]
        total_supply = responses[2]

    backing_ratio = REDEEM_VALUE if total_supply == 0 else (total_value * REDEEM_VALUE) // total_supply
    return OriginMetrics(
        redeem_value=REDEEM_VALUE,
        wrapped_oeth_redeem_value=wrapped_oeth_redeem_value,
        backing_ratio=backing_ratio,
        total_value=total_value,
        total_supply=total_supply,
    )


def process_origin(chain: Chain):
    """
    Monitor Origin Protocol metrics on the specified chain.

    OETH redeem value is 1e18, 1-to-1 with ETH.
    Wrapped OETH redeem value should only go up, if it goes down, it is a sign of protocol health issue.

    Args:
        chain: The blockchain to monitor (BASE or MAINNET)
    """
    config = ORIGIN_CONFIGS[chain]
    client = ChainManager.get_client(chain)
    wrapped_oeth = client.eth.contract(address=config["wrapped_oeth_address"], abi=ABI_ORIGIN_WRAPPED_OETH)
    metrics = (
        _fetch_base_metrics(client, chain, config, wrapped_oeth)
        if chain == Chain.BASE
        else _fetch_mainnet_metrics(client, chain, config, wrapped_oeth)
    )
    if metrics is None:
        logger.error("Failed to fetch metrics on %s", chain.name)
        send_alert(
            Alert(
                AlertSeverity.LOW,
                f"🚨 Origin Protocol Alert! Failed to fetch metrics on {chain.name}",
                PROTOCOL,
                channel=CHANNEL,
            )
        )
        return

    redeem_value = metrics.redeem_value
    wrapped_oeth_redeem_value = metrics.wrapped_oeth_redeem_value

    logger.info(
        "Redeem value for OETH on %s: %s\nWrapped OETH redeem value on %s: %s\nVault backing ratio on %s: %s",
        chain.name,
        f"{redeem_value / 1e18:.6f}",
        chain.name,
        f"{wrapped_oeth_redeem_value / 1e18:.6f}",
        chain.name,
        f"{metrics.backing_ratio / 1e18:.6f}",
    )

    # Caching for wrapped OETH redeem value, if value is lower, send telegram message
    cache_key = get_cache_key(chain)
    cached_redeem_value = get_last_queued_id_from_file(cache_key)

    if wrapped_oeth_redeem_value == 0:
        logger.info(
            "[%s] Saving initial redeem value to cache: %s",
            chain.network_name,
            f"{wrapped_oeth_redeem_value / 1e18:.2f}",
        )
    elif wrapped_oeth_redeem_value < cached_redeem_value:
        message = (
            f"🚨 *Origin Protocol {chain.network_name.upper()} Alert* 🚨\n"
            f"🔍 Redeem value dropped!\n"
            f"Current Redeem Value: {wrapped_oeth_redeem_value / 1e18:.2f}\n"
            f"Previous Redeem Value: {cached_redeem_value / 1e18:.2f}\n"
            f"Difference: {cached_redeem_value - wrapped_oeth_redeem_value} ({((cached_redeem_value - wrapped_oeth_redeem_value) / cached_redeem_value) * 100:.2f}%)"
        )
        send_alert(Alert(AlertSeverity.HIGH, message, PROTOCOL, channel=CHANNEL))

    write_last_queued_id_to_file(cache_key, wrapped_oeth_redeem_value)

    if metrics.backing_ratio < REDEEM_VALUE:
        message = (
            f"🚨 *Origin Protocol on {chain.network_name.upper()} Alert* 🚨\n"
            f"🔍 Vault backing ratio below threshold!\n"
            f"Backing Ratio: {metrics.backing_ratio / 1e18:.6f}\n"
            f"Threshold: {REDEEM_VALUE / 1e18:.6f}\n"
            f"Total Value: {metrics.total_value / 1e18:.6f}\n"
            f"Total Supply: {metrics.total_supply / 1e18:.6f}"
        )
        send_alert(Alert(AlertSeverity.CRITICAL, message, PROTOCOL, channel=CHANNEL))


def main():
    logger.info("Checking Origin Protocol on Mainnet...")
    process_origin(Chain.MAINNET)
    logger.info("Checking Origin Protocol on Base...")
    process_origin(Chain.BASE)
    # NOTE: no need to check liquidity on Base because pool is concentrated around 1 tick and by pool with the highest liquidity on Base
    # docs: https://docs.originprotocol.com/yield-bearing-tokens/core-concepts/amo


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)

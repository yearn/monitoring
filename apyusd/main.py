from web3 import Web3

from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.config import Config
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

PROTOCOL = "apyusd"
logger = get_logger(PROTOCOL)

APXUSD_RATE_ORACLE = Web3.to_checksum_address("0xa2ef2e7bf32248083e514a737259f3785ea8d37d")
APXUSD_RATE_ORACLE_IMPLEMENTATION = Web3.to_checksum_address("0x26ea4a9099b4da41b2d0e7e9874a29104d8bb17f")
RATE_PRECISION = 10**18
RATE_DELTA_ALERT_THRESHOLD = Config.get_env_float("APYUSD_RATE_DELTA_ALERT_THRESHOLD", 0.10)
CACHE_KEY_LAST_BLOCK = f"{PROTOCOL}_last_processed_block"

RATE_ORACLE_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": False, "internalType": "uint256", "name": "oldRate", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "newRate", "type": "uint256"},
        ],
        "name": "RateUpdated",
        "type": "event",
    }
]


def _get_cached_last_block() -> int | None:
    cached = get_last_value_for_key_from_file(cache_filename, CACHE_KEY_LAST_BLOCK)
    if cached == 0:
        return None
    try:
        return int(str(cached))
    except ValueError:
        logger.warning("Ignoring invalid cached last block value: %s", cached)
        return None


def _set_cached_last_block(block_number: int) -> None:
    write_last_value_to_file(cache_filename, CACHE_KEY_LAST_BLOCK, block_number)


def get_rate_delta(previous_rate: int | None, current_rate: int) -> float | None:
    if previous_rate is None or previous_rate <= 0:
        return None
    return (current_rate - previous_rate) / previous_rate


def should_alert_on_rate_delta(previous_rate: int | None, current_rate: int, threshold: float) -> bool:
    delta = get_rate_delta(previous_rate, current_rate)
    return delta is not None and abs(delta) >= threshold


def _format_rate(raw_rate: int) -> float:
    return raw_rate / RATE_PRECISION


def main() -> None:
    client = ChainManager.get_client(Chain.MAINNET)
    oracle = client.get_contract(APXUSD_RATE_ORACLE, RATE_ORACLE_ABI)

    try:
        latest_block = int(client.eth.block_number)
        cached_last_block = _get_cached_last_block()

        if cached_last_block is None:
            logger.info("No cached block found for apxUSD; initializing cursor at block %s", latest_block)
            _set_cached_last_block(latest_block)
            return

        from_block = cached_last_block + 1
        if from_block > latest_block:
            logger.info("apxUSD cursor already at latest block (%s)", latest_block)
            return

        logger.info(
            "Scanning apxUSD RateUpdated events from block %s to %s (implementation=%s)",
            from_block,
            latest_block,
            APXUSD_RATE_ORACLE_IMPLEMENTATION,
        )

        logs = oracle.events.RateUpdated().get_logs(from_block=from_block, to_block=latest_block)
        logger.info("Found %s RateUpdated events", len(logs))

        for log in logs:
            old_rate = int(log["args"]["oldRate"])
            new_rate = int(log["args"]["newRate"])

            if should_alert_on_rate_delta(old_rate, new_rate, RATE_DELTA_ALERT_THRESHOLD):
                delta = get_rate_delta(old_rate, new_rate)
                assert delta is not None
                direction = "increase" if delta > 0 else "decrease"
                tx_hash = log["transactionHash"].hex()
                block_number = int(log["blockNumber"])
                explorer = Chain.MAINNET.explorer_url
                tx_url = f"{explorer}/tx/{tx_hash}" if explorer else tx_hash
                send_alert(
                    Alert(
                        AlertSeverity.HIGH,
                        (
                            "*apxUSD oracle rate delta detected*\n\n"
                            f"Oracle: `{APXUSD_RATE_ORACLE}`\n"
                            f"Previous rate: {_format_rate(old_rate):.8f}\n"
                            f"Current rate: {_format_rate(new_rate):.8f}\n"
                            f"Direction: {direction}\n"
                            f"Delta: {delta:+.2%}\n"
                            f"Threshold: {RATE_DELTA_ALERT_THRESHOLD:.0%}\n"
                            f"Block: {block_number}\n"
                            f"Tx: {tx_url}\n"
                            f"Implementation: `{APXUSD_RATE_ORACLE_IMPLEMENTATION}`"
                        ),
                        PROTOCOL,
                    )
                )

        _set_cached_last_block(latest_block)
    except Exception as e:
        logger.error("Error monitoring apxUSD rate oracle: %s", e)
        send_alert(Alert(AlertSeverity.LOW, f"apxUSD rate oracle monitoring failed: {e}", PROTOCOL), plain_text=True)


if __name__ == "__main__":
    main()

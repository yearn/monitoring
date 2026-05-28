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
CACHE_KEY_RATE = f"{PROTOCOL}_rate"

RATE_ORACLE_ABI = [
    {
        "inputs": [],
        "name": "rate",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def _get_cached_rate() -> int | None:
    cached = get_last_value_for_key_from_file(cache_filename, CACHE_KEY_RATE)
    if cached == 0:
        return None
    try:
        return int(str(cached))
    except ValueError:
        logger.warning("Ignoring invalid cached rate value: %s", cached)
        return None


def _set_cached_rate(rate: int) -> None:
    write_last_value_to_file(cache_filename, CACHE_KEY_RATE, rate)


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
        current_rate = int(oracle.functions.rate().call())
        previous_rate = _get_cached_rate()

        logger.info(
            "apxUSD oracle current_rate=%s formatted=%.8f implementation=%s",
            current_rate,
            _format_rate(current_rate),
            APXUSD_RATE_ORACLE_IMPLEMENTATION,
        )

        if previous_rate is None:
            logger.info("No cached rate found for apxUSD; initializing cache")
            _set_cached_rate(current_rate)
            return

        if should_alert_on_rate_delta(previous_rate, current_rate, RATE_DELTA_ALERT_THRESHOLD):
            delta = get_rate_delta(previous_rate, current_rate)
            assert delta is not None
            direction = "increase" if delta > 0 else "decrease"
            send_alert(
                Alert(
                    AlertSeverity.HIGH,
                    (
                        "*apxUSD oracle rate delta detected*\n\n"
                        f"Oracle: `{APXUSD_RATE_ORACLE}`\n"
                        f"Previous rate: {_format_rate(previous_rate):.8f}\n"
                        f"Current rate: {_format_rate(current_rate):.8f}\n"
                        f"Direction: {direction}\n"
                        f"Delta: {delta:+.2%}\n"
                        f"Threshold: {RATE_DELTA_ALERT_THRESHOLD:.0%}\n"
                        f"Implementation: `{APXUSD_RATE_ORACLE_IMPLEMENTATION}`"
                    ),
                    PROTOCOL,
                )
            )

        _set_cached_rate(current_rate)
    except Exception as e:
        logger.error("Error monitoring apxUSD rate oracle: %s", e)
        send_alert(Alert(AlertSeverity.LOW, f"apxUSD rate oracle monitoring failed: {e}", PROTOCOL), plain_text=True)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)

from web3 import Web3

from utils.abi import load_abi
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chains import Chain
from utils.logging import get_logger
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager

PROTOCOL = "strata"
logger = get_logger(PROTOCOL)

SRUSDE = Web3.to_checksum_address("0x3d7d6fdf07EE548B939A80edbc9B2256d0cdc003")
JRUSDE = Web3.to_checksum_address("0xC58D044404d8B14e953C115E67823784dEA53d8F")
STRATA_CDO = Web3.to_checksum_address("0x908B3921aaE4fC17191D382BB61020f2Ee6C0e20")
SUSDE = Web3.to_checksum_address("0x9D39A5DE30E57443BfF2A8307A4256c8797A3497")
SUSDE_STRATEGY = Web3.to_checksum_address("0xdbf4FB6C310C1C85D0b41B5DbCA06096F2E7099F")

WEI = 10**18

COVERAGE_MIN = 1.05
STRATEGY_RATIO_DROP_ALERT = 0.20
TVL_CHANGE_ALERT_RATIO = 0.15
JR_DRAIN_ALERT_RATIO = 0.15

ERC4626_ABI = load_abi("common-abi/YearnV3Vault.json")
ERC20_ABI = load_abi("common-abi/ERC20.json")
SUSDE_COOLDOWN_ABI = [
    {
        "inputs": [],
        "name": "cooldownDuration",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def _cache_float(key: str) -> float | None:
    value = get_last_value_for_key_from_file(cache_filename, key)
    if value == 0:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _cache_int(key: str) -> int | None:
    value = get_last_value_for_key_from_file(cache_filename, key)
    if value == 0:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _set_cache_float(key: str, value: float) -> None:
    write_last_value_to_file(cache_filename, key, value)


def _breach_once(cache_key: str, condition: bool, message: str, messages: list[str]) -> None:
    raw_state = get_last_value_for_key_from_file(cache_filename, cache_key)
    state = int(float(raw_state)) if raw_state != 0 else 0

    if condition:
        if state == 0:
            messages.append(message)
        write_last_value_to_file(cache_filename, cache_key, 1)
    elif state == 1:
        write_last_value_to_file(cache_filename, cache_key, 0)


def _check_susde_vault(messages: list[str], client, susde_vault, cooldown_contract) -> None:
    try:
        with client.batch_requests() as batch:
            batch.add(susde_vault.functions.convertToAssets(WEI))
            batch.add(cooldown_contract.functions.cooldownDuration())
            responses = client.execute_batch(batch)
    except Exception as e:
        logger.warning("Could not read sUSDe vault metrics: %s", e)
        return

    if len(responses) != 2:
        logger.warning("sUSDe vault batch expected 2 responses, got %s", len(responses))
        return

    susde_rate_raw, cooldown_duration = responses
    susde_rate = float(susde_rate_raw) / WEI

    susde_rate_cache_key = f"{PROTOCOL}_susde_rate"
    previous_susde_rate = _cache_float(susde_rate_cache_key)
    if previous_susde_rate is not None and susde_rate < previous_susde_rate:
        drop_bps = ((previous_susde_rate - susde_rate) / previous_susde_rate) * 10_000
        messages.append(
            "🚨 sUSDe vault share value decreased.\n"
            f"previous: {previous_susde_rate:.8f} current: {susde_rate:.8f} ({drop_bps:.2f} bps drop)"
        )
    _set_cache_float(susde_rate_cache_key, susde_rate)

    cooldown_duration_int = int(cooldown_duration)
    cooldown_cache_key = f"{PROTOCOL}_susde_cooldown_duration"
    previous_cooldown = _cache_int(cooldown_cache_key)
    if previous_cooldown is not None and cooldown_duration_int != previous_cooldown:
        messages.append(
            f"🚨 sUSDe cooldown duration changed.\nprevious: {previous_cooldown}s current: {cooldown_duration_int}s"
        )
    write_last_value_to_file(cache_filename, cooldown_cache_key, cooldown_duration_int)


def _check_daily_tvl(messages: list[str], total_deposits: float) -> None:
    tvl_cache_key = f"{PROTOCOL}_total_deposits"
    previous_total_deposits = _cache_float(tvl_cache_key)
    if previous_total_deposits is not None and previous_total_deposits > 0:
        tvl_change = (total_deposits - previous_total_deposits) / previous_total_deposits
        if abs(tvl_change) >= TVL_CHANGE_ALERT_RATIO:
            messages.append(
                "⚠️ Strata total TVL changed significantly.\n"
                f"previous: ${previous_total_deposits:,.2f} current: ${total_deposits:,.2f} ({tvl_change:.2%})"
            )
    _set_cache_float(tvl_cache_key, total_deposits)


def _check_jr_drain(messages: list[str], jr_assets: float) -> None:
    jr_assets_cache_key = f"{PROTOCOL}_jr_assets"
    previous_jr_assets = _cache_float(jr_assets_cache_key)
    if previous_jr_assets is not None and previous_jr_assets > 0:
        jr_change = (jr_assets - previous_jr_assets) / previous_jr_assets
        if jr_change <= -JR_DRAIN_ALERT_RATIO:
            messages.append(
                "⚠️ jrUSDe totalAssets dropped quickly (junior side draining).\n"
                f"previous: ${previous_jr_assets:,.2f} current: ${jr_assets:,.2f} ({jr_change:.2%})"
            )
    _set_cache_float(jr_assets_cache_key, jr_assets)


def main() -> None:
    client = ChainManager.get_client(Chain.MAINNET)

    sr = client.get_contract(SRUSDE, ERC4626_ABI)
    jr = client.get_contract(JRUSDE, ERC4626_ABI)
    susde = client.get_contract(SUSDE, ERC20_ABI)
    susde_vault = client.get_contract(SUSDE, ERC4626_ABI)
    susde_cooldown = client.get_contract(SUSDE, SUSDE_COOLDOWN_ABI)

    try:
        with client.batch_requests() as batch:
            batch.add(sr.functions.totalAssets())
            batch.add(sr.functions.convertToAssets(WEI))
            batch.add(jr.functions.totalAssets())
            batch.add(susde.functions.balanceOf(SUSDE_STRATEGY))
            responses = client.execute_batch(batch)

        if len(responses) != 4:
            raise ValueError(f"Batch call expected 4 responses, got {len(responses)}")

        sr_total_assets, sr_rate_raw, jr_total_assets, strategy_raw = responses

        sr_assets = float(sr_total_assets) / WEI
        sr_rate = float(sr_rate_raw) / WEI
        jr_assets = float(jr_total_assets) / WEI
        strategy_susde_balance = float(strategy_raw) / WEI

        coverage_ratio = (sr_assets + jr_assets) / sr_assets if sr_assets > 0 else 0.0
        total_deposits = sr_assets + jr_assets
        strategy_ratio = (strategy_susde_balance / total_deposits) if total_deposits > 0 else 0.0

        logger.info(
            "strata_cdo=%s sr_assets=%s sr_rate=%s jr_assets=%s strategy_susde_balance=%s strategy_ratio=%s coverage_ratio=%s",
            STRATA_CDO,
            f"{sr_assets:,.2f}",
            f"{sr_rate:.6f}",
            f"{jr_assets:,.2f}",
            f"{strategy_susde_balance:,.2f}",
            f"{strategy_ratio:.2%}",
            f"{coverage_ratio:.4f}",
        )

        messages: list[str] = []

        _breach_once(
            f"{PROTOCOL}_coverage_below_105",
            coverage_ratio < COVERAGE_MIN,
            (
                "🚨 Strata senior coverage ratio below 105%.\n"
                f"coverage ratio: {coverage_ratio:.4f} (min {COVERAGE_MIN:.2f})\n"
                f"StrataCDO: {STRATA_CDO}"
            ),
            messages,
        )

        sr_rate_cache_key = f"{PROTOCOL}_sr_rate"
        previous_sr_rate = _cache_float(sr_rate_cache_key)
        if previous_sr_rate is not None and sr_rate < previous_sr_rate:
            drop_bps = ((previous_sr_rate - sr_rate) / previous_sr_rate) * 10_000
            messages.append(
                "🚨 srUSDe share value decreased.\n"
                f"previous: {previous_sr_rate:.8f} current: {sr_rate:.8f} ({drop_bps:.2f} bps drop)"
            )
        _set_cache_float(sr_rate_cache_key, sr_rate)

        strategy_ratio_cache_key = f"{PROTOCOL}_strategy_ratio"
        previous_strategy_ratio = _cache_float(strategy_ratio_cache_key)
        if previous_strategy_ratio is not None and previous_strategy_ratio > 0:
            strategy_ratio_drop = (previous_strategy_ratio - strategy_ratio) / previous_strategy_ratio
            if strategy_ratio_drop >= STRATEGY_RATIO_DROP_ALERT:
                messages.append(
                    "⚠️ sUSDe strategy balance dropped relative to total deposits.\n"
                    f"previous ratio: {previous_strategy_ratio:.2%} current ratio: {strategy_ratio:.2%} "
                    f"({strategy_ratio_drop:.2%} drop)"
                )
        _set_cache_float(strategy_ratio_cache_key, strategy_ratio)
        _check_daily_tvl(messages, total_deposits)
        _check_jr_drain(messages, jr_assets)
        _check_susde_vault(messages, client, susde_vault, susde_cooldown)

        if messages:
            send_telegram_message("\n\n".join(messages), PROTOCOL)

    except Exception as e:
        logger.error("Error: %s", e)
        send_telegram_message(f"⚠️ Strata monitoring failed: {e}", PROTOCOL, False, True)


if __name__ == "__main__":
    main()

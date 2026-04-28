#!/usr/bin/env python3
"""Monitor large USDai transfers via incremental Transfer-event scanning."""

from decimal import Decimal

from utils.alert import AlertSeverity
from utils.chains import Chain
from utils.config import Config
from utils.erc20_large_transfer_monitor import ERC20LargeTransferMonitorConfig, run_erc20_large_transfer_monitor

PROTOCOL = "usdai"

USDAI_TOKEN_ADDR = "0x0A1a1A107E45b7Ced86833863f482BC5f4ed82EF"

LARGE_TRANSFER_THRESHOLD_TOKENS = Decimal(Config.get_env("USDAI_LARGE_TRANSFER_THRESHOLD", "100000"))
TRANSFER_SCAN_CHUNK_BLOCKS = Config.get_env_int("USDAI_LARGE_TRANSFER_CHUNK_BLOCKS", 2000)
TRANSFER_FIRST_RUN_LOOKBACK_BLOCKS = Config.get_env_int("USDAI_LARGE_TRANSFER_FIRST_RUN_LOOKBACK_BLOCKS", 2000)


def main() -> None:
    run_erc20_large_transfer_monitor(
        ERC20LargeTransferMonitorConfig(
            protocol=PROTOCOL,
            chain=Chain.ARBITRUM,
            token_address=USDAI_TOKEN_ADDR,
            threshold_tokens=LARGE_TRANSFER_THRESHOLD_TOKENS,
            chunk_size_blocks=TRANSFER_SCAN_CHUNK_BLOCKS,
            first_run_lookback_blocks=TRANSFER_FIRST_RUN_LOOKBACK_BLOCKS,
            cache_suffix="large_transfers",
            alert_severity=AlertSeverity.MEDIUM,
            alert_label="Large Transfer Alert",
            monitor_note="This monitor scans Transfer logs incrementally (cached last block, chunked getLogs).",
        )
    )


if __name__ == "__main__":
    main()

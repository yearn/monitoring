#!/usr/bin/env python3
"""Monitor large USDai mints via totalSupply delta only (no event scanning)."""

from decimal import Decimal

from utils.chains import Chain
from utils.config import Config
from utils.erc20_supply_delta_monitor import ERC20SupplyDeltaMonitorConfig, run_erc20_supply_delta_monitor
from utils.logging import get_logger

PROTOCOL = "usdai"
logger = get_logger(f"{PROTOCOL}.large_mints")

USDAI_TOKEN_ADDR = "0x0A1a1A107E45b7Ced86833863f482BC5f4ed82EF"

MINT_THRESHOLD_PERCENT = Decimal(Config.get_env("USDAI_LARGE_MINT_THRESHOLD_PERCENT", "0.05"))


def main() -> None:
    run_erc20_supply_delta_monitor(
        ERC20SupplyDeltaMonitorConfig(
            protocol=PROTOCOL,
            chain=Chain.ARBITRUM,
            token_address=USDAI_TOKEN_ADDR,
            threshold_percent=MINT_THRESHOLD_PERCENT,
            cache_suffix="large_mints",
            alert_label="Large Mint Alert (Supply Delta)",
            monitor_note="This monitor intentionally uses only totalSupply deltas (no event scanning).",
        )
    )


if __name__ == "__main__":
    main()

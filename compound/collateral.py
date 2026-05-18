"""
On-chain Compound V3 collateral risk monitoring.

Reads collateral data directly from Comet contracts instead of relying on
Gauntlet's dashboard API (which can have stale data 36h+).

For each Compound V3 market:
- Discovers collateral assets and their on-chain balances & prices
- Calculates per-asset allocation ratios and weighted risk levels
- Checks borrow/supply ratio
- Detects bad debt (negative reserves)

Sends Telegram alerts when thresholds are exceeded.
"""

from dataclasses import dataclass

from utils.abi import load_abi
from utils.assets import (
    ALLOCATION_TIERS,
    MAX_RISK_THRESHOLDS,
    SUPPLY_ASSETS_DICT,
    get_market_allocation_threshold,
)
from utils.chains import Chain
from utils.formatting import format_usd
from utils.logging import get_logger
from utils.telegram import send_telegram_message
from utils.web3_wrapper import ChainManager, Web3Client

PROTOCOL = "comp"
logger = get_logger(PROTOCOL)

# Borrow/supply alert threshold (Compound on-chain monitoring only)
DEBT_SUPPLY_RATIO: float = 0.95  # 95%

PRICE_SCALE = 1e8  # Compound V3 prices use 8 decimal places (Chainlink format)

ABI_COMET = load_abi("compound/abi/CTokenV3.json")
ABI_ERC20 = load_abi("common-abi/ERC20.json")

# (address, name, risk_level) per chain
# Risk levels matches strategy risk level
MARKETS_BY_CHAIN: dict[Chain, list[tuple[str, str, int]]] = {
    Chain.MAINNET: [
        ("0xc3d688B66703497DAA19211EEdff47f25384cdc3", "cUSDCv3", 1),
        ("0x3Afdc9BCA9213A35503b077a6072F3D0d5AB0840", "cUSDTv3", 2),
        ("0xA17581A9E3356d9A858b789D68B4d866e593aE94", "cWETHv3", 2),
        ("0x5D409e56D886231aDAf00c8775665AD0f9897b56", "cUSDSv3", 1),
    ],
}


@dataclass
class CollateralAsset:
    """On-chain collateral asset data for a Compound V3 market."""

    symbol: str
    address: str
    price_feed: str
    scale: int
    borrow_collateral_factor: float
    liquidate_collateral_factor: float
    supply_cap: int
    total_supply_raw: int
    price_raw: int

    @property
    def base_value(self) -> float:
        """Calculate value of total collateral supplied in base-asset terms.

        For USD-based markets (cUSDCv3, cUSDTv3) this is USD.
        For non-USD markets (cWETHv3) this is in the base asset (e.g. ETH).
        Multiply by MarketData.base_usd_price to get actual USD value.
        """
        return self.total_supply_raw * self.price_raw / (self.scale * PRICE_SCALE)


@dataclass
class MarketData:
    """Aggregated on-chain data for a Compound V3 market."""

    name: str
    address: str
    risk_level: int
    base_scale: int
    base_price_raw: int
    total_supply_raw: int
    total_borrow_raw: int
    reserves_raw: int  # signed: negative = bad debt
    collaterals: list[CollateralAsset]

    @property
    def total_supply_usd(self) -> float:
        return self.total_supply_raw * self.base_price_raw / (self.base_scale * PRICE_SCALE)

    @property
    def total_borrow_usd(self) -> float:
        return self.total_borrow_raw * self.base_price_raw / (self.base_scale * PRICE_SCALE)

    @property
    def reserves_usd(self) -> float:
        return self.reserves_raw * self.base_price_raw / (self.base_scale * PRICE_SCALE)

    @property
    def base_usd_price(self) -> float:
        """USD price of the base asset (e.g. ~1.0 for USDC, ~1800 for ETH)."""
        return self.base_price_raw / PRICE_SCALE

    @property
    def total_collateral_usd(self) -> float:
        return sum(c.base_value for c in self.collaterals) * self.base_usd_price


def _fetch_single_market(client: Web3Client, address: str, name: str, risk_level: int) -> MarketData:
    """Fetch on-chain data for a single Compound V3 market.

    Uses 3 small batches per market to stay within RPC batch limits
    (Tenderly caps at ~50 per batch).
    """
    comet = client.eth.contract(address=address, abi=ABI_COMET)

    # --- Batch 1: market-level data (6 calls) ---
    with client.batch_requests() as batch:
        batch.add(comet.functions.numAssets())
        batch.add(comet.functions.totalSupply())
        batch.add(comet.functions.totalBorrow())
        batch.add(comet.functions.getReserves())
        batch.add(comet.functions.baseTokenPriceFeed())
        batch.add(comet.functions.baseScale())
        resp_1 = client.execute_batch(batch)

    num_assets = int(resp_1[0])
    total_supply = int(resp_1[1])
    total_borrow = int(resp_1[2])
    reserves = int(resp_1[3])
    base_price_feed = resp_1[4]
    base_scale = int(resp_1[5])

    # --- Batch 2: getAssetInfo for all assets (up to ~16 calls) ---
    with client.batch_requests() as batch:
        for asset_idx in range(num_assets):
            batch.add(comet.functions.getAssetInfo(asset_idx))
        resp_2 = client.execute_batch(batch)

    # --- Batch 3: per-asset data + base price (up to ~49 calls) ---
    with client.batch_requests() as batch:
        batch.add(comet.functions.getPrice(base_price_feed))
        for info in resp_2:
            asset_address = info[1]
            price_feed = info[2]
            batch.add(comet.functions.totalsCollateral(asset_address))
            batch.add(comet.functions.getPrice(price_feed))
            erc20 = client.eth.contract(address=asset_address, abi=ABI_ERC20)
            batch.add(erc20.functions.symbol())
        resp_3 = client.execute_batch(batch)

    # Parse batch 3
    base_price_raw = int(resp_3[0])
    collaterals: list[CollateralAsset] = []
    idx = 1
    for info in resp_2:
        total_supply_asset = int(resp_3[idx][0])
        idx += 1
        price_raw = int(resp_3[idx])
        idx += 1
        symbol = resp_3[idx]
        idx += 1

        collaterals.append(
            CollateralAsset(
                symbol=symbol,
                address=info[1],
                price_feed=info[2],
                scale=int(info[3]),
                borrow_collateral_factor=int(info[4]) / 1e18,
                liquidate_collateral_factor=int(info[5]) / 1e18,
                supply_cap=int(info[7]),
                total_supply_raw=total_supply_asset,
                price_raw=price_raw,
            )
        )

    return MarketData(
        name=name,
        address=address,
        risk_level=risk_level,
        base_scale=base_scale,
        base_price_raw=base_price_raw,
        total_supply_raw=total_supply,
        total_borrow_raw=total_borrow,
        reserves_raw=reserves,
        collaterals=collaterals,
    )


def _fetch_markets_data(chain: Chain) -> list[MarketData]:
    """Fetch on-chain data for all Compound V3 markets on a chain.

    Processes one market at a time (3 batches each) to stay within
    RPC provider batch limits (~50 calls per batch).
    """
    markets = MARKETS_BY_CHAIN.get(chain, [])
    if not markets:
        return []

    client = ChainManager.get_client(chain)
    return [_fetch_single_market(client, address, name, risk_level) for address, name, risk_level in markets]


def _analyze_market(market: MarketData) -> list[str]:
    """Analyze a single market's collateral risk. Returns list of alert messages."""
    alerts: list[str] = []
    total_collateral_usd = market.total_collateral_usd
    base_usd_price = market.base_usd_price
    total_collateral_base = sum(c.base_value for c in market.collaterals)

    if total_collateral_base == 0:
        logger.info("Market %s has no collateral", market.name)
        return alerts

    logger.info("Market: %s (risk level %d)", market.name, market.risk_level)
    logger.info("Total supply: %s", format_usd(market.total_supply_usd))
    logger.info("Total borrow: %s", format_usd(market.total_borrow_usd))
    logger.info("Total collateral: %s", format_usd(total_collateral_usd))
    logger.debug("--------------------------------")
    logger.debug("Asset | Supply | Allocation")

    total_risk_level = 0.0
    unknown_assets: list[str] = []

    for c in sorted(market.collaterals, key=lambda x: x.base_value, reverse=True):
        if c.base_value == 0:
            continue

        c_usd = c.base_value * base_usd_price
        asset_risk_tier: int = int(SUPPLY_ASSETS_DICT.get(c.symbol, 5))  # type: ignore[call-overload]
        if c.symbol not in SUPPLY_ASSETS_DICT:
            unknown_assets.append(c.symbol)

        allocation_ratio = c.base_value / total_collateral_base
        allocation_threshold = get_market_allocation_threshold(asset_risk_tier, market.risk_level)

        if allocation_ratio > allocation_threshold:
            alerts.append(
                f"🔺 High allocation detected for {c.symbol} in market {market.name}\n"
                f"💹 Current allocation: {allocation_ratio:.1%}\n"
                f"📊 Max acceptable allocation: {allocation_threshold:.1%}\n"
                f"💰 Supply amount: {format_usd(c_usd)}"
            )

        risk_multiplier = asset_risk_tier
        total_risk_level += risk_multiplier * allocation_ratio
        logger.debug("%s | %s | %s", c.symbol, format_usd(c_usd), f"{allocation_ratio:.1%}")

    # Check total risk level
    max_risk = MAX_RISK_THRESHOLDS.get(market.risk_level, MAX_RISK_THRESHOLDS[max(ALLOCATION_TIERS.keys())])
    if total_risk_level > max_risk:
        alerts.append(
            f"🔺 High total risk level detected in market {market.name}:\n"
            f"📊 Total risk level: {total_risk_level:.1%}\n"
            f"📈 Max acceptable risk: {max_risk:.1%}\n"
            f"💰 Total collateral: {format_usd(total_collateral_usd)}"
        )

    # Check borrow/supply ratio
    if market.total_supply_usd > 0 and market.total_borrow_usd / market.total_supply_usd > DEBT_SUPPLY_RATIO:
        ratio = market.total_borrow_usd / market.total_supply_usd
        alerts.append(
            f"🔺 High borrow/supply ratio detected in market {market.name}:\n"
            f"📊 Total borrow/supply ratio: {ratio:.1%}\n"
            f"💰 Total supply: {format_usd(market.total_supply_usd)}\n"
            f"💸 Total borrow: {format_usd(market.total_borrow_usd)}"
        )

    # Check bad debt (negative reserves)
    if market.reserves_raw < 0:
        bad_debt_usd = abs(market.reserves_usd)
        alerts.append(
            f"🚨 Bad debt detected in market {market.name}:\n"
            f"💀 Bad debt: {format_usd(bad_debt_usd)}\n"
            f"💰 Total supply: {format_usd(market.total_supply_usd)}\n"
            f"💸 Total borrow: {format_usd(market.total_borrow_usd)}"
        )

    # Alert on unknown assets
    if unknown_assets:
        alerts.append(
            f"⚠️ Unknown collateral assets in market {market.name}: {', '.join(unknown_assets)}\n"
            "Please update SUPPLY\\_ASSETS in utils/assets.py"
        )

    logger.info("Total risk level: %s", f"{total_risk_level:.1%}")
    logger.info("Reserves: %s", format_usd(market.reserves_usd))
    logger.info("================================")

    return alerts


def check_collateral_risk(chain: Chain) -> None:
    """Check on-chain collateral risk for all Compound V3 markets on a chain.

    Fetches live data from Comet contracts and sends Telegram alerts
    if any risk thresholds are exceeded.
    """
    logger.info("Checking on-chain collateral risk for %s...", chain.name)
    markets_data = _fetch_markets_data(chain)

    for market in markets_data:
        alerts = _analyze_market(market)
        if alerts:
            message = "\n\n".join(alerts)
            send_telegram_message(message, PROTOCOL)

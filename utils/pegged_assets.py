"""Single source of truth for pegged-asset peg monitoring.

This registry is consumed by every layer of peg/oracle monitoring:

* L1 — market depeg (DeFiLlama price vs ``peg`` target, deviation > ``depeg_pct``)
* L2 — oracle health (``chainlink_feed`` staleness / round sanity, ``rate_oracle`` drift)
* L3 — event consumers

Peg deviation is expressed relative to a :class:`PegTarget` (``USD`` is the
constant ``1``; ``BTC`` is the live BTC/USD price from DeFiLlama), so a single
entry covers both dollar- and bitcoin-denominated assets. ``depeg_pct`` is a
*deviation* tolerance (fractional distance from the peg), not an absolute floor.

Addresses and Chainlink feeds were verified on Ethereum mainnet.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from utils.defillama import fetch_prices

# DeFiLlama key for the live BTC/USD reference price (BTC peg target).
BTC_USD_DEFILLAMA_KEY = "coingecko:bitcoin"


class PegTarget(Enum):
    """What an asset is pegged to.

    ``USD`` resolves to the constant ``1``; ``BTC`` resolves to the live BTC/USD
    price fetched from DeFiLlama.
    """

    USD = "USD"
    BTC = "BTC"


@dataclass(frozen=True)
class ChainlinkFeed:
    """A Chainlink aggregator backing an asset's price (consumed by L2)."""

    address: str
    heartbeat: int  # max expected seconds between updates (Chainlink mainnet default)
    description: str = ""


@dataclass(frozen=True)
class RateOracle:
    """A continuous / exchange-rate oracle backing a yield-bearing asset (L2/L3).

    Args:
        address: Oracle contract address.
        monotonic: Whether the rate is expected to be non-decreasing; a decrease
            is a loss signal worth alerting on.
        function: View function returning the rate. Defaults to ``"rate"``.
        precision: Fixed-point precision of the returned rate. Defaults to ``1e18``.
    """

    address: str
    monotonic: bool = True
    function: str = "rate"
    precision: int = 10**18
    description: str = ""


@dataclass(frozen=True)
class PeggedAsset:
    """A monitored pegged asset and everything the peg layers need to check it."""

    name: str
    defillama_key: str  # "chain:address"
    channel: str  # Telegram routing channel
    peg: PegTarget
    depeg_pct: Decimal  # deviation tolerance from the peg (e.g. Decimal("0.02") = 2%)
    chainlink_feed: ChainlinkFeed | None = None
    rate_oracle: RateOracle | None = None

    @property
    def address(self) -> str:
        """Token address parsed from ``defillama_key`` ("chain:address")."""
        return self.defillama_key.split(":", 1)[1]

    def deviation(self, price: Decimal, peg_price: Decimal) -> Decimal:
        """Signed fractional deviation of ``price`` from ``peg_price``."""
        return price_deviation(price, peg_price)

    def is_depegged(self, price: Decimal, peg_price: Decimal) -> bool:
        """Return ``True`` if ``price`` deviates from ``peg_price`` beyond ``depeg_pct``."""
        return abs(self.deviation(price, peg_price)) >= self.depeg_pct


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def price_deviation(price: Decimal, peg_price: Decimal) -> Decimal:
    """Signed fractional deviation of ``price`` from ``peg_price``.

    Args:
        price: Observed asset price.
        peg_price: Reference peg price.

    Returns:
        ``(price - peg_price) / peg_price``.

    Raises:
        ValueError: If ``peg_price`` is zero (deviation is undefined).
    """
    if peg_price == 0:
        raise ValueError("peg_price must be non-zero to compute deviation")
    return (price - peg_price) / peg_price


def resolve_peg_prices(pegs: set[PegTarget]) -> dict[PegTarget, Decimal]:
    """Resolve current prices for a set of peg targets.

    ``USD`` is the constant ``1`` and never hits the network; ``BTC`` is fetched
    once from DeFiLlama only when present in ``pegs``.

    Args:
        pegs: The distinct peg targets to resolve.

    Returns:
        Mapping of each requested :class:`PegTarget` to its current price.

    Raises:
        ValueError: If the BTC/USD price is requested but unavailable.
    """
    prices: dict[PegTarget, Decimal] = {}
    if PegTarget.USD in pegs:
        prices[PegTarget.USD] = Decimal(1)
    if PegTarget.BTC in pegs:
        fetched = fetch_prices([BTC_USD_DEFILLAMA_KEY])
        btc_price = fetched.get(BTC_USD_DEFILLAMA_KEY)
        if btc_price is None:
            raise ValueError(f"BTC/USD price unavailable from DeFiLlama key {BTC_USD_DEFILLAMA_KEY}")
        prices[PegTarget.BTC] = btc_price
    return prices


def get_asset(name: str) -> PeggedAsset:
    """Look up a registered asset by name.

    Raises:
        KeyError: If no asset with ``name`` is registered.
    """
    return PEGGED_ASSETS_BY_NAME[name]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Chainlink mainnet stable feeds report 8 decimals with a 24h heartbeat unless
# noted; confirm per feed before tightening staleness thresholds in L2.
_STABLE_HEARTBEAT = 86_400  # 24h

PEGGED_ASSETS: list[PeggedAsset] = [
    # --- USD-pegged blue chips ------------------------------------------------
    PeggedAsset(
        name="USDC",
        defillama_key="ethereum:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        channel="pegs",
        peg=PegTarget.USD,
        depeg_pct=Decimal("0.02"),
        chainlink_feed=ChainlinkFeed("0x8fFfFfd4AfB6115b954Bd326cbe7B4BA576818f6", _STABLE_HEARTBEAT, "USDC/USD"),
    ),
    PeggedAsset(
        name="USDT",
        defillama_key="ethereum:0xdAC17F958D2ee523a2206206994597C13D831ec7",
        channel="pegs",
        peg=PegTarget.USD,
        depeg_pct=Decimal("0.02"),
        chainlink_feed=ChainlinkFeed("0x3E7d1eAB13ad0104d2750B8863b489D65364e32D", _STABLE_HEARTBEAT, "USDT/USD"),
    ),
    PeggedAsset(
        name="USDS",
        defillama_key="ethereum:0xdC035D45d973E3EC169d2276DDab16f1e407384F",
        channel="pegs",
        peg=PegTarget.USD,
        depeg_pct=Decimal("0.02"),
        chainlink_feed=ChainlinkFeed("0xfF30586cD0F29eD462364C7e81375FC0C71219b1", _STABLE_HEARTBEAT, "USDS/USD"),
    ),
    # --- USD-pegged protocol stables ------------------------------------------
    PeggedAsset(
        name="USDe",
        defillama_key="ethereum:0x4c9EDD5852cd905f086C759E8383e09bff1E68B3",
        channel="ethena",
        peg=PegTarget.USD,
        depeg_pct=Decimal("0.03"),
        chainlink_feed=ChainlinkFeed("0xa569d910839Ae8865Da8F8e70FfFb0cBA869F961", _STABLE_HEARTBEAT, "USDe/USD"),
    ),
    PeggedAsset(
        name="cUSD",
        defillama_key="ethereum:0xcccc62962d17b8914c62d74ffb843d73b2a3cccc",
        channel="cap",
        peg=PegTarget.USD,
        depeg_pct=Decimal("0.05"),  # cap cUSD price is more volatile
    ),
    PeggedAsset(
        name="iUSD",
        defillama_key="ethereum:0x48f9e38f3070AD8945DFEae3FA70987722E3D89c",
        channel="infinifi",
        peg=PegTarget.USD,
        depeg_pct=Decimal("0.03"),
    ),
    # --- BTC-pegged -----------------------------------------------------------
    PeggedAsset(
        name="cbBTC",
        defillama_key="ethereum:0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        channel="pegs",
        peg=PegTarget.BTC,
        depeg_pct=Decimal("0.02"),
        chainlink_feed=ChainlinkFeed("0x2665701293fCbEB223D11A08D826563EDcCE423A", _STABLE_HEARTBEAT, "cbBTC/USD"),
    ),
    PeggedAsset(
        name="LBTC",
        defillama_key="ethereum:0x8236a87084f8B84306f72007F36F2618A5634494",
        channel="pegs",
        peg=PegTarget.BTC,
        depeg_pct=Decimal("0.03"),
        # LBTC/BTC market-rate feed (8 decimals); price ~1 BTC per LBTC.
        chainlink_feed=ChainlinkFeed("0x5c29868C58b6e15e2b962943278969Ab6a7D3212", _STABLE_HEARTBEAT, "LBTC/BTC"),
    ),
]

PEGGED_ASSETS_BY_NAME: dict[str, PeggedAsset] = {asset.name: asset for asset in PEGGED_ASSETS}

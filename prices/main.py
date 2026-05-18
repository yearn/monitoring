"""Depeg monitoring via on-chain oracles and DefiLlama market-ratio checks.

Two signal sources, both routed to the owning protocol's Telegram channel:

1. **Oracle check** — reads Redstone fundamental push oracles (AggregatorV3). Each
   asset carries its own threshold; breaching it is CRITICAL.

2. **DefiLlama market check** — fetches market prices and computes a
   ``market_ratio / fair_value`` deviation. ``market_ratio`` is ``price / ETH``
   for LRTs or the USD price itself for stables. ``fair_value`` is a per-asset
   floor (1.0 for stables, > 1 for accruing LRTs) so accruing LRTs are checked
   against their accrued rate rather than a flat 1:1 peg. Deviation below
   ``threshold`` is CRITICAL.
"""

from dataclasses import dataclass
from decimal import Decimal

from utils.abi import load_abi
from utils.alert import Alert, AlertSeverity, send_alert
from utils.chains import Chain
from utils.defillama import fetch_prices
from utils.logging import get_logger
from utils.web3_wrapper import ChainManager

logger = get_logger("prices")

# Reference token for LRT/ETH ratio computation
WETH_KEY = "ethereum:0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

AGGREGATOR_V3_ABI = load_abi("prices/abi/AggregatorV3.json")


@dataclass(frozen=True)
class OracleAsset:
    """Asset monitored via on-chain Redstone fundamental oracle (AggregatorV3)."""

    symbol: str
    oracle_address: str
    decimals: int
    protocol: str
    threshold: Decimal  # asset-specific; matches the Tenderly alert documented in README


@dataclass(frozen=True)
class DefiLlamaAsset:
    """Asset monitored via DefiLlama market price.

    ``fair_value`` is the expected ratio vs the ``underlying`` reference (ETH or USD).
    Accruing LRTs trade above 1 ETH, so a flat 1.0 baseline would miss real depegs
    until parity. Per-asset fair values catch smaller deviations from the accrued
    value but must be bumped periodically — see README.
    """

    symbol: str
    defillama_key: str
    underlying: str  # "ETH" or "USD"
    protocol: str
    fair_value: Decimal = Decimal("1.0")
    threshold: Decimal = Decimal("0.98")  # 2% deviation from fair_value


# Mainnet-only today. If non-mainnet oracle assets are added, group by chain.
ORACLE_ASSETS: list[OracleAsset] = [
    # LBTC/BTC fundamental — Redstone push, 24h heartbeat / 1% deviation
    # Tenderly alert: eca272ef-979a-47b3-a7f0-2e67172889bb
    OracleAsset("LBTC", "0xb415eAA355D8440ac7eCB602D3fb67ccC1f0bc81", 8, "lrt", Decimal("0.998")),
    # cUSD/USD fundamental — Redstone push
    # Tenderly alert: 316f440e-457b-4cfa-a69e-f7f54230bf44 fires at latestAnswer < 0.9998
    OracleAsset("cUSD", "0x9a5a3c3ed0361505cc1d4e824b3854de5724434a", 8, "cap", Decimal("0.9998")),
]

# DefiLlama-monitored assets. No on-chain push oracle available on Ethereum mainnet
# for these; Redstone offers off-chain fundamental feeds for some LRTs but they
# require calldata injection and cannot be read directly on-chain.
DEFILLAMA_ASSETS: list[DefiLlamaAsset] = [
    # ---- LRTs (vs ETH) ----
    DefiLlamaAsset("weETH", "ethereum:0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee", "ETH", "lrt", Decimal("1.07")),
    DefiLlamaAsset("ezETH", "ethereum:0xbf5495Efe5DB9ce00f80364C8B423567e58d2110", "ETH", "lrt", Decimal("1.06")),
    DefiLlamaAsset("rsETH", "ethereum:0xA1290d69c65A6Fe4DF752f95823Fae25cB99e5A7", "ETH", "lrt", Decimal("1.05")),
    DefiLlamaAsset("pufETH", "ethereum:0xD9A442856C234a39a81a089C06451EBAa4306a72", "ETH", "lrt", Decimal("1.05")),
    # No documented off-chain fundamental feed; 1.0 ETH is a catastrophic-depeg floor only.
    DefiLlamaAsset("osETH", "ethereum:0xf1C9acDc66974dFB6dEcB12aA385b9cD01190E38", "ETH", "lrt"),
    DefiLlamaAsset("rswETH", "ethereum:0xFAe103DC9cf190eD75350761e95403b7b8aFa6c0", "ETH", "lrt"),
    DefiLlamaAsset("mETH", "ethereum:0xd5F7838F5C461fefF7FE49ea5ebaF7728bB0ADfa", "ETH", "lrt"),
    # ---- Stables (vs USD) ----
    DefiLlamaAsset("FDUSD", "ethereum:0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409", "USD", "stables"),
    DefiLlamaAsset("deUSD", "ethereum:0x15700B564Ca08D9439C58cA5053166E8317aa138", "USD", "stables"),
    DefiLlamaAsset("USD0", "ethereum:0x73A15FeD60Bf67631dC6cd7Bc5B6e8da8190aCF5", "USD", "stables"),
    # USD0++ is a ~4-year locked bond and legitimately trades at a discount vs USD0;
    # only alert on catastrophic dislocation.
    DefiLlamaAsset(
        "USD0++",
        "ethereum:0x35D8949372D46B7a3D5A56006AE77B215fc69bC0",
        "USD",
        "stables",
        threshold=Decimal("0.90"),
    ),
    # USDe is also covered in stables/main.py with threshold 0.97 — duplicate signal
    # is intentional: this module enforces a tighter 0.98 floor.
    DefiLlamaAsset("USDe", "ethereum:0x4c9EDD5852cd905f086C759E8383e09bff1E68B3", "USD", "stables"),
]


def check_oracle_assets() -> None:
    """Read on-chain fundamental oracles; alert per-protocol on any depeg."""
    if not ORACLE_ASSETS:
        return

    client = ChainManager.get_client(Chain.MAINNET)
    with client.batch_requests() as batch:
        for asset in ORACLE_ASSETS:
            contract = client.eth.contract(
                address=client.w3.to_checksum_address(asset.oracle_address),
                abi=AGGREGATOR_V3_ABI,
            )
            batch.add(contract.functions.latestRoundData())
        responses = client.execute_batch(batch)

    if len(responses) != len(ORACLE_ASSETS):
        logger.error("Expected %d oracle responses, got %d", len(ORACLE_ASSETS), len(responses))
        return

    depegged_by_protocol: dict[str, list[tuple[str, Decimal, Decimal]]] = {}
    for asset, result in zip(ORACLE_ASSETS, responses):
        try:
            # latestRoundData returns (roundId, answer, startedAt, updatedAt, answeredInRound)
            answer = Decimal(str(result[1])) / Decimal(10**asset.decimals)
        except (IndexError, TypeError) as exc:
            logger.error("Failed to parse oracle response for %s: %s", asset.symbol, exc)
            send_alert(Alert(AlertSeverity.MEDIUM, f"Oracle parse failed for {asset.symbol}: {exc}", asset.protocol))
            continue

        logger.info("%s oracle price: %s (threshold: %s)", asset.symbol, answer, asset.threshold)
        if answer < asset.threshold:
            depegged_by_protocol.setdefault(asset.protocol, []).append((asset.symbol, answer, asset.threshold))

    for protocol, depegged in depegged_by_protocol.items():
        _send_depeg_alert(depegged, protocol, "Oracle")


def check_defillama_assets() -> None:
    """Check DefiLlama market prices, normalizing each asset by its fair_value."""
    if not DEFILLAMA_ASSETS:
        return

    needs_eth = any(a.underlying == "ETH" for a in DEFILLAMA_ASSETS)
    token_keys = list({a.defillama_key for a in DEFILLAMA_ASSETS} | ({WETH_KEY} if needs_eth else set()))

    try:
        prices = fetch_prices(token_keys)
    except Exception as exc:
        logger.warning("Failed to fetch DefiLlama prices: %s", exc)
        # Notify every affected protocol so a fetch outage isn't routed to only one channel.
        for protocol in {a.protocol for a in DEFILLAMA_ASSETS}:
            send_alert(Alert(AlertSeverity.LOW, f"Depeg price fetch failed: {exc}", protocol))
        return

    eth_price = prices.get(WETH_KEY) if needs_eth else None
    if needs_eth and not eth_price:
        logger.error("Missing ETH reference price from DefiLlama")
        for protocol in {a.protocol for a in DEFILLAMA_ASSETS if a.underlying == "ETH"}:
            send_alert(Alert(AlertSeverity.MEDIUM, "Missing ETH reference price from DefiLlama", protocol))
        # Don't return — USD-denominated stables can still be checked.

    depegged_by_protocol: dict[str, list[tuple[str, Decimal, Decimal]]] = {}
    missing_by_protocol: dict[str, list[str]] = {}

    for asset in DEFILLAMA_ASSETS:
        if asset.underlying == "ETH" and not eth_price:
            continue  # already alerted above

        price = prices.get(asset.defillama_key)
        if price is None:
            logger.warning("No price returned for %s (%s)", asset.symbol, asset.defillama_key)
            missing_by_protocol.setdefault(asset.protocol, []).append(asset.symbol)
            continue

        market_ratio = price / eth_price if asset.underlying == "ETH" else price
        # Normalize against fair_value so accruing LRTs are checked against accrued rate.
        deviation = market_ratio / asset.fair_value

        logger.info(
            "%s price: $%s, %s ratio: %s, fair: %s, deviation: %s (threshold: %s)",
            asset.symbol,
            price,
            asset.underlying,
            market_ratio,
            asset.fair_value,
            deviation,
            asset.threshold,
        )

        if deviation < asset.threshold:
            depegged_by_protocol.setdefault(asset.protocol, []).append((asset.symbol, deviation, asset.threshold))

    for protocol, symbols in missing_by_protocol.items():
        send_alert(
            Alert(
                AlertSeverity.MEDIUM,
                f"DefiLlama returned no price for: {', '.join(symbols)} — depeg coverage degraded",
                protocol,
            )
        )

    for protocol, depegged in depegged_by_protocol.items():
        _send_depeg_alert(depegged, protocol, "DefiLlama")


def _send_depeg_alert(depegged: list[tuple[str, Decimal, Decimal]], protocol: str, source: str) -> None:
    """Send CRITICAL alert listing all depegged assets with their per-asset thresholds."""
    lines = [f"*{symbol}*: {value:.4f} (threshold {threshold})" for symbol, value, threshold in depegged]
    message = f"Depeg detected ({source}):\n" + "\n".join(lines)
    send_alert(Alert(AlertSeverity.CRITICAL, message, protocol))


def main() -> None:
    """Run depeg monitoring for all tracked assets."""
    logger.info("Starting depeg monitoring...")
    check_oracle_assets()
    check_defillama_assets()
    logger.info("Depeg monitoring complete.")


if __name__ == "__main__":
    main()

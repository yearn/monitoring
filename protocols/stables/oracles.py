"""Layer 2 peg monitoring — on-chain oracle health for pegged assets (hourly).

Where ``protocols/stables/main.py`` watches *market* price (DeFiLlama), this watches
the **on-chain oracles our lending markets actually liquidate on**. Driven by the
shared :data:`PeggedAsset` registry, for every Chainlink-backed asset it checks:

* **staleness** — ``now − updatedAt > heartbeat + buffer``;
* **round sanity / monotonicity** — positive answer, completed round,
  ``answeredInRound ≥ roundId``, and a non-decreasing ``roundId`` vs the cached run;
* **deviation from peg** — oracle price vs the asset's :class:`PegTarget`;
* **oracle ↔ market divergence** — Chainlink vs DeFiLlama, the actual
  liquidation-risk signal (markets liquidate on the oracle, not market price).

For **rate / fundamental oracles** (vault-rate, capped Redstone feeds) it checks
monotonicity + delta-vs-cached (the ``protocols/apyusd/main.py`` approach); any
fundamental-oracle depeg is ``CRITICAL`` (per #196). Fundamental oracles already
covered by a Tenderly alert are listed in :data:`TENDERLY_COVERED` and skipped
here to avoid duplicate alerting.

Runs hourly via ``automation/jobs.yaml``.
"""

from dataclasses import dataclass
from decimal import Decimal

from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import cache_filename, get_last_value_for_key_from_file, write_last_value_to_file
from utils.chainlink import FeedReading, RoundData, is_round_healthy, read_feeds, round_issues
from utils.chains import Chain
from utils.config import Config
from utils.defillama import fetch_prices
from utils.logger import get_logger
from utils.pegged_assets import PEGGED_ASSETS, PeggedAsset, PegTarget, price_deviation, resolve_peg_prices
from utils.web3_wrapper import ChainManager, Web3Client

PROTOCOL = "pegs"
logger = get_logger("stables-oracles")

# Tunables (env-overridable).
STALENESS_BUFFER = Config.get_env_int("PEG_ORACLE_STALENESS_BUFFER", 600)  # 10 min grace on heartbeat
DIVERGENCE_THRESHOLD = Decimal(str(Config.get_env_float("PEG_ORACLE_DIVERGENCE_THRESHOLD", 0.01)))  # 1%
RATE_DELTA_THRESHOLD = Decimal(str(Config.get_env_float("PEG_ORACLE_RATE_DELTA_THRESHOLD", 0.05)))  # 5%

CACHE_FILE = cache_filename


def _round_cache_key(address: str) -> str:
    return f"peg_oracle_round_{address.lower()}"


def _rate_cache_key(address: str) -> str:
    return f"peg_oracle_rate_{address.lower()}"


# Fundamental oracles already covered by an existing Tenderly alert — NOT polled
# here to avoid duplicate alerting. See protocols/lrt-pegs/README.md.
#
# Gap analysis (per #196 step 6): the registry currently exposes no *uncovered*
# fundamental oracle. Adding active polling for a new one needs the oracle
# contract address, its read function + precision, and whether it is monotonic
# (capped) — wire it as a ``rate_oracle`` on the relevant ``PeggedAsset``.
TENDERLY_COVERED: dict[str, str] = {
    # LBTC Redstone fundamental oracle, upper-capped at 1 (healthy == 1).
    "LBTC Redstone (0xb415eAA355D8440ac7eCB602D3fb67ccC1f0bc81)": (
        "https://dashboard.tenderly.co/yearn/sam/alerts/rules/eca272ef-979a-47b3-a7f0-2e67172889bb"
    ),
}


# ---------------------------------------------------------------------------
# Observation + pure per-check helpers (unit tested without a chain)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OracleObservation:
    """Everything needed to evaluate one Chainlink-backed asset for one run."""

    asset: PeggedAsset
    reading: FeedReading
    peg_price_usd: Decimal  # USD price of the asset's peg target
    quote_price_usd: Decimal  # USD price of the feed's quote unit
    now: int
    market_price_usd: Decimal | None = None  # DeFiLlama; None when unavailable
    prev_round_id: int | None = None  # cached roundId from the previous run

    @property
    def oracle_price_usd(self) -> Decimal:
        """Chainlink answer expressed in USD (answer × quote price)."""
        return self.reading.price * self.quote_price_usd


def check_staleness(obs: OracleObservation, buffer: int = STALENESS_BUFFER) -> Alert | None:
    """Alert (HIGH) if the feed has not updated within heartbeat + buffer."""
    feed = obs.asset.chainlink_feed
    assert feed is not None
    updated_at = obs.reading.round_data.updated_at
    age = obs.now - updated_at
    if updated_at <= 0 or age > feed.heartbeat + buffer:
        return Alert(
            AlertSeverity.HIGH,
            f"*{obs.asset.name} oracle stale* ({feed.description})\n"
            f"Age: {age}s — heartbeat {feed.heartbeat}s + buffer {buffer}s\n"
            f"updatedAt: {updated_at}\n"
            f"Feed: `{feed.address}`",
            obs.asset.protocol,
            channel=obs.asset.channel,
        )
    return None


def check_round_health(obs: OracleObservation) -> Alert | None:
    """Alert if round sanity checks fail or ``roundId`` moved backwards.

    A non-positive answer, an incomplete round, or a backwards ``roundId`` is a
    feed malfunction (``CRITICAL``); a stale ``answeredInRound`` is ``HIGH``.
    """
    feed = obs.asset.chainlink_feed
    assert feed is not None
    round_data = obs.reading.round_data
    issues = round_issues(round_data)

    if obs.prev_round_id is not None and round_data.round_id < obs.prev_round_id:
        issues.append(f"roundId went backwards ({obs.prev_round_id} -> {round_data.round_id})")

    if not issues:
        return None

    # Anything beyond a lagging answeredInRound means the feed is broken.
    critical = any("answeredInRound" not in issue for issue in issues)
    severity = AlertSeverity.CRITICAL if critical else AlertSeverity.HIGH
    return Alert(
        severity,
        f"*{obs.asset.name} oracle round unhealthy* ({feed.description})\n"
        + "\n".join(f"- {issue}" for issue in issues)
        + f"\nFeed: `{feed.address}`",
        obs.asset.protocol,
        channel=obs.asset.channel,
    )


def check_peg_deviation(obs: OracleObservation) -> Alert | None:
    """Alert (HIGH) if the oracle price deviates from the peg beyond ``depeg_pct``."""
    if obs.peg_price_usd <= 0:
        return None
    if not obs.asset.is_depegged(obs.oracle_price_usd, obs.peg_price_usd):
        return None
    dev = obs.asset.deviation(obs.oracle_price_usd, obs.peg_price_usd)
    return Alert(
        AlertSeverity.HIGH,
        f"*{obs.asset.name} oracle off peg* ({obs.asset.peg.value})\n"
        f"Oracle: ${obs.oracle_price_usd:.6f}\n"
        f"Peg: ${obs.peg_price_usd:.6f}\n"
        f"Deviation: {dev:+.2%} (tolerance {obs.asset.depeg_pct:.2%})",
        obs.asset.protocol,
        channel=obs.asset.channel,
    )


def check_market_divergence(obs: OracleObservation, threshold: Decimal = DIVERGENCE_THRESHOLD) -> Alert | None:
    """Alert (HIGH) if the oracle and DeFiLlama market price diverge beyond ``threshold``."""
    if obs.market_price_usd is None or obs.market_price_usd <= 0:
        return None
    dev = price_deviation(obs.oracle_price_usd, obs.market_price_usd)
    if abs(dev) < threshold:
        return None
    return Alert(
        AlertSeverity.HIGH,
        f"*{obs.asset.name} oracle ↔ market divergence*\n"
        f"Oracle: ${obs.oracle_price_usd:.6f}\n"
        f"Market (DeFiLlama): ${obs.market_price_usd:.6f}\n"
        f"Divergence: {dev:+.2%} (threshold {threshold:.2%})",
        obs.asset.protocol,
        channel=obs.asset.channel,
    )


def evaluate_chainlink_asset(
    obs: OracleObservation,
    *,
    buffer: int = STALENESS_BUFFER,
    divergence_threshold: Decimal = DIVERGENCE_THRESHOLD,
) -> list[Alert]:
    """Run all Chainlink-asset checks, returning the alerts that fired."""
    candidates = [
        check_staleness(obs, buffer),
        check_round_health(obs),
        check_peg_deviation(obs),
        check_market_divergence(obs, divergence_threshold),
    ]
    return [alert for alert in candidates if alert is not None]


def next_cached_round(prev_round_id: int | None, round_data: RoundData) -> int:
    """High-water-mark ``roundId`` to persist so a regression never poisons the cache.

    The cached ``roundId`` is the baseline the next run compares against for
    monotonicity. Writing a lower or malfunctioning round would make the
    regression the new baseline, so it is flagged only once and a feed stuck at
    (or crawling below) the regressed round looks "monotonic" forever. Only a
    healthy, non-decreasing round advances the mark.
    """
    if not is_round_healthy(round_data):
        return prev_round_id or 0
    if prev_round_id is None:
        return round_data.round_id
    return max(prev_round_id, round_data.round_id)


def check_rate_oracle(
    asset: PeggedAsset,
    current_rate: int,
    prev_rate: int | None,
    threshold: Decimal = RATE_DELTA_THRESHOLD,
) -> list[Alert]:
    """Monotonicity + delta-vs-cached checks for a fundamental / rate oracle.

    A decrease in a monotonic (capped) oracle is a fundamental depeg
    (``CRITICAL``); any delta beyond ``threshold`` is ``HIGH``.
    """
    oracle = asset.rate_oracle
    assert oracle is not None
    if prev_rate is None or prev_rate <= 0:
        return []

    alerts: list[Alert] = []
    delta = Decimal(current_rate - prev_rate) / Decimal(prev_rate)

    if oracle.monotonic and current_rate < prev_rate:
        alerts.append(
            Alert(
                AlertSeverity.CRITICAL,
                f"*{asset.name} fundamental oracle DECREASED* (monotonic/capped)\n"
                f"Previous: {prev_rate}\nCurrent: {current_rate}\nDelta: {delta:+.4%}\n"
                f"Oracle: `{oracle.address}`",
                asset.protocol,
                channel=asset.channel,
            )
        )
    elif abs(delta) >= threshold:
        alerts.append(
            Alert(
                AlertSeverity.HIGH,
                f"*{asset.name} fundamental oracle delta* {delta:+.4%} (threshold {threshold:.2%})\n"
                f"Previous: {prev_rate}\nCurrent: {current_rate}\nOracle: `{oracle.address}`",
                asset.protocol,
                channel=asset.channel,
            )
        )
    return alerts


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _build_rate_oracle_abi(function: str) -> list[dict]:
    return [
        {
            "inputs": [],
            "name": function,
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function",
        }
    ]


def _monitor_chainlink_assets(client: Web3Client) -> None:
    """Check every registry asset that has a Chainlink feed."""
    assets = [a for a in PEGGED_ASSETS if a.chainlink_feed is not None]
    if not assets:
        return

    needed_targets: set[PegTarget] = set()
    for asset in assets:
        assert asset.chainlink_feed is not None
        needed_targets.add(asset.peg)
        needed_targets.add(asset.chainlink_feed.quote)
    peg_prices = resolve_peg_prices(needed_targets)

    market_prices = fetch_prices([a.defillama_key for a in assets])
    readings = read_feeds(client, [a.chainlink_feed.address for a in assets])  # type: ignore[union-attr]
    now = int(client.eth.get_block("latest")["timestamp"])

    for asset in assets:
        feed = asset.chainlink_feed
        assert feed is not None
        reading = readings[feed.address]

        prev_round_raw = get_last_value_for_key_from_file(CACHE_FILE, _round_cache_key(feed.address))
        try:
            prev_round_id: int | None = int(str(prev_round_raw)) if str(prev_round_raw) != "0" else None
        except ValueError:
            prev_round_id = None

        obs = OracleObservation(
            asset=asset,
            reading=reading,
            peg_price_usd=peg_prices[asset.peg],
            quote_price_usd=peg_prices[feed.quote],
            now=now,
            market_price_usd=market_prices.get(asset.defillama_key),
            prev_round_id=prev_round_id,
        )

        alerts = evaluate_chainlink_asset(obs)
        logger.info(
            "%s oracle: $%.6f (peg $%.6f, market %s) — %d alert(s)",
            asset.name,
            obs.oracle_price_usd,
            obs.peg_price_usd,
            f"${obs.market_price_usd:.6f}" if obs.market_price_usd is not None else "n/a",
            len(alerts),
        )
        for alert in alerts:
            send_alert(alert)

        write_last_value_to_file(
            CACHE_FILE, _round_cache_key(feed.address), next_cached_round(prev_round_id, reading.round_data)
        )


def _monitor_rate_oracles(client: Web3Client) -> None:
    """Check every registry asset that has a fundamental / rate oracle."""
    assets = [a for a in PEGGED_ASSETS if a.rate_oracle is not None]
    for asset in assets:
        oracle = asset.rate_oracle
        assert oracle is not None
        contract = client.get_contract(oracle.address, _build_rate_oracle_abi(oracle.function))
        current_rate = int(contract.functions[oracle.function]().call())

        prev_raw = get_last_value_for_key_from_file(CACHE_FILE, _rate_cache_key(oracle.address))
        try:
            prev_rate: int | None = int(str(prev_raw)) if str(prev_raw) != "0" else None
        except ValueError:
            prev_rate = None

        alerts = check_rate_oracle(asset, current_rate, prev_rate)
        logger.info("%s rate oracle %s: %d (%d alert(s))", asset.name, oracle.address, current_rate, len(alerts))
        for alert in alerts:
            send_alert(alert)

        write_last_value_to_file(CACHE_FILE, _rate_cache_key(oracle.address), current_rate)


def main() -> None:
    """Run all L2 oracle-health checks driven by the pegged-asset registry."""
    client = ChainManager.get_client(Chain.MAINNET)
    _monitor_chainlink_assets(client)
    _monitor_rate_oracles(client)
    logger.info("L2 oracle health check complete (%d Tenderly-covered oracle(s) skipped)", len(TENDERLY_COVERED))


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)

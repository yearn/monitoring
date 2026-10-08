"""Resolve what an Infinifi ``Accounting.setOracle(asset, oracle)`` assigns.

The calldata carries an oracle address only. A reviewer needs the price it
reports in the protocol's reference unit and — because Infinifi's
``ChainlinkOracle`` is a thin wrapper — the feed behind it:

- the feed's own contract and ``description()``, since "ChainlinkOracle" names
  the AggregatorV3 interface it reads, not who publishes the price (reUSD's is
  Re Protocol's self-reported "reUSD NAV / USD", not a Chainlink market feed);
- the wrapper's ``heartbeat()`` and the feed's last update: ``price()`` reverts
  once the feed is older than the heartbeat, so every ``Accounting.price(asset)``
  reverts with it.

Reports called the freshness and failure behaviour "not established" when both
are one RPC read away.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from eth_utils import to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.llm.abi_exposure import exposes
from utils.llm.report import address_link
from utils.logger import get_logger
from utils.source_context import fetch_verified_contract
from utils.web3_wrapper import ChainManager

logger = get_logger("utils.llm.infinifi_oracle_context")

PROTOCOL = "infinifi"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# IOracle.price() scale: a whole token's value in the reference unit is
# price * 10**decimals / 1e36 (USDC is quoted at ~1e30 for a 1:1 price).
_ORACLE_PRICE_SCALE = Decimal(10) ** 36


def _view(name: str, outputs: list[str]) -> dict:
    return {
        "name": name,
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": kind} for kind in outputs],
    }


_ORACLE_ABI = [
    _view("price", ["uint256"]),
    _view("feed", ["address"]),
    _view("heartbeat", ["uint256"]),
    _view("decimalNormalization", ["uint256"]),
    _view("divide", ["bool"]),
]
_FEED_ABI = [
    _view("description", ["string"]),
    _view("decimals", ["uint8"]),
    _view("latestRoundData", ["uint80", "int256", "uint256", "uint256", "uint80"]),
]


@dataclass(frozen=True)
class FeedDetails:
    """The AggregatorV3 feed an Infinifi ChainlinkOracle wraps, and its freshness rule."""

    feed: str
    contract_name: str  # verified contract name of the feed, "" when unverified
    description: str
    heartbeat: int  # seconds; price() reverts once the feed is older
    updated_at: int | None
    read_at: int | None  # block timestamp of the read
    # answer → price: 0 = as-is, else multiplied or divided by this factor
    normalization: int = 0
    divide: bool = False

    @property
    def age(self) -> int | None:
        if self.updated_at is None or self.read_at is None:
            return None
        return max(self.read_at - self.updated_at, 0)

    def describe(self) -> str:
        """One sentence: what the feed is, how fresh, and what staleness does."""
        name = f"{self.contract_name}, " if self.contract_name else "unverified, "
        text = f'It reads feed {self.feed} ({name}description "{self.description or "n/a"}")'
        if self.normalization:
            text += f", answer {'divided' if self.divide else 'multiplied'} by {self.normalization:,}"
        if self.age is not None:
            text += f"; last update {_hours(self.age)} before this read"
        text += (
            f". Freshness rule: price() reverts (StalePrice) once the feed is older than the heartbeat of "
            f"{_hours(self.heartbeat)}, so Accounting.price for this asset — and anything that values it — "
            "reverts until the feed updates. The wrapper's name refers to the AggregatorV3 interface it reads; "
            "the feed's contract and description above identify who publishes the price"
        )
        return text + "."


@dataclass(frozen=True)
class OracleAssignmentContext:
    """The price an oracle reports for the asset it is being assigned to."""

    accounting: str
    asset: str
    asset_symbol: str
    asset_decimals: int
    oracle: str
    price_raw: int
    oracle_name: str = ""
    feed: FeedDetails | None = None

    @property
    def addresses(self) -> list[str]:
        return [self.asset, self.oracle] + ([self.feed.feed] if self.feed else [])

    @property
    def labels(self) -> dict[str, str]:
        labels = {self.oracle: self.oracle_name} if self.oracle_name else {}
        if self.feed and self.feed.contract_name:
            labels[self.feed.feed] = self.feed.contract_name
        return labels

    @property
    def unit_price(self) -> Decimal:
        """Reference-unit value of one whole asset token (1 = parity with USDC)."""
        return Decimal(self.price_raw) * (Decimal(10) ** self.asset_decimals) / _ORACLE_PRICE_SCALE

    @property
    def unit_price_text(self) -> str:
        return f"{self.unit_price.normalize():f}"


def _hours(seconds: int) -> str:
    """Seconds as hours with one decimal, whole hours without, e.g. ``48h`` / ``12.4h``."""
    hours = seconds / 3600
    return f"{hours:g}h" if hours == int(hours) else f"{hours:.1f}h"


def _address_param(call: DecodedCall, position: int) -> str | None:
    if len(call.params) <= position:
        return None
    type_str, value = call.params[position]
    if type_str != "address" or not isinstance(value, str):
        return None
    try:
        return to_checksum_address(value)
    except ValueError:
        return None


def _contract_name(chain_id: int, address: str) -> str:
    record = fetch_verified_contract(chain_id, address)
    return record.contract_name if record else ""


def _feed_details(chain_id: int, client: Any, oracle_contract: Any) -> FeedDetails:
    """Read the feed an oracle wraps, with its heartbeat and last update."""
    functions = oracle_contract.functions
    feed = to_checksum_address(functions.feed().call())
    heartbeat = int(functions.heartbeat().call())
    normalization = int(functions.decimalNormalization().call())
    divide = bool(functions.divide().call())
    feed_contract = client.get_contract(feed, _FEED_ABI)
    try:
        description = str(feed_contract.functions.description().call())
    except Exception:  # noqa: BLE001 - optional on some feeds
        description = ""
    updated_at = read_at = None
    try:
        updated_at = int(feed_contract.functions.latestRoundData().call()[3])
        read_at = int(client.w3.eth.get_block("latest")["timestamp"])
    except Exception as error:  # noqa: BLE001 - freshness is enrichment
        logger.info("Feed %s latestRoundData unavailable: %s", feed, error)
    return FeedDetails(
        feed=feed,
        contract_name=_contract_name(chain_id, feed),
        description=description,
        heartbeat=heartbeat,
        updated_at=updated_at,
        read_at=read_at,
        normalization=normalization,
        divide=divide,
    )


def _oracle_context(chain_id: int, target: str, call: DecodedCall) -> OracleAssignmentContext | None:
    """Read the price a newly assigned oracle reports, and the feed behind it."""
    if call.function_name != "setOracle":
        return None
    asset, oracle = _address_param(call, 0), _address_param(call, 1)
    if asset is None or oracle is None or oracle == ZERO_ADDRESS:
        return None
    if not exposes(chain_id, target, {"setOracle", "oracle", "price"}):
        return None
    metadata = fetch_erc20_metadata(chain_id, asset)
    if metadata is None:
        return None
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    contract = client.get_contract(oracle, _ORACLE_ABI)
    price = int(contract.functions.price().call())
    feed = None
    if exposes(chain_id, oracle, {"feed", "heartbeat", "price"}):
        try:
            feed = _feed_details(chain_id, client, contract)
        except Exception as error:  # noqa: BLE001 - the price still renders
            logger.info("Oracle %s feed details unavailable: %s", oracle, error)
    return OracleAssignmentContext(
        accounting=target,
        asset=asset,
        asset_symbol=metadata.symbol,
        asset_decimals=metadata.decimals,
        oracle=oracle,
        price_raw=price,
        oracle_name=_contract_name(chain_id, oracle),
        feed=feed,
    )


def resolve_infinifi_oracle_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[OracleAssignmentContext]:
    """Resolve every ``setOracle`` in an Infinifi alert on Ethereum."""
    if protocol.lower() != PROTOCOL or chain_id != Chain.MAINNET.chain_id:
        return []
    contexts: list[OracleAssignmentContext] = []
    for target, call in targets_and_calls:
        try:
            context = _oracle_context(chain_id, to_checksum_address(target), call)
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Infinifi oracle context failed for %s.%s: %s", target, call.function_name, error)
            continue
        if context is not None:
            contexts.append(context)
    return contexts


def format_infinifi_oracle_prompt(contexts: list[OracleAssignmentContext]) -> str:
    """Render verified oracle-assignment context for the LLM prompt."""
    lines = []
    for context in contexts:
        name = f" ({context.oracle_name})" if context.oracle_name else ""
        line = (
            f"Oracle {context.oracle}{name} assigned to {context.asset} ({context.asset_symbol}, "
            f"{context.asset_decimals} decimals) reports price() = {context.price_raw}, i.e. one whole "
            f"{context.asset_symbol} is valued at {context.unit_price_text} reference units "
            "(IOracle scale: price * 10^decimals / 1e36; USDC is ~1)."
        )
        if context.feed:
            line += " " + context.feed.describe()
        lines.append(line)
    return "\n".join(lines)


def format_infinifi_oracle_report(
    contexts: list[OracleAssignmentContext], chain_id: int, labels: dict[str, str]
) -> str:
    """Render the deterministic oracle section for the gist report."""
    lines = []
    for context in contexts:
        line = (
            f"- **Oracle price:** {address_link(context.oracle, chain_id, labels)} reports `{context.price_raw}` "
            f"→ one whole `{context.asset_symbol}` = `{context.unit_price_text}` reference units (USDC ≈ 1)"
        )
        feed = context.feed
        if feed:
            freshness = f", updated {_hours(feed.age)} before this alert" if feed.age is not None else ""
            line += (
                f"\n  - **Feed:** {address_link(feed.feed, chain_id, labels)} — "
                f'"{feed.description or "n/a"}"; heartbeat `{_hours(feed.heartbeat)}`{freshness}. '
                "A stale feed makes `price()` revert."
            )
        lines.append(line)
    return "\n".join(lines)

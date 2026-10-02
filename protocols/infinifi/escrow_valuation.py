"""Compare each infiniFi RWAEscrowRouter's reported value with the tokens it holds.

A router escrow's ``totalAssets`` is a stored figure. The rate manager raises it
by a governance-set annual rate on every harvest, and nothing ties it to the
tokens the router holds. Those tokens can lose value while the reported figure
keeps growing. On 29/09/2026 the mGLOBAL router's position was converted to mGLO
at Midas's lowered redemption price, leaving it about 1.67M USD above Midas NAV.

For every router registered in the FarmRegistry, this script values what the
router actually holds:

- stablecoins at par;
- Midas mTokens at Midas's NAV feed (the unadjusted feed, not the lowered one the
  redemption vaults use). The router's whitelisted Midas vaults name the mToken
  and its feed;
- Midas requests still pending for the router: redemptions at NAV, deposits at
  their USD amount.

It alerts when the reported value exceeds that by more than the threshold, and
when the router holds a token it cannot price. A gap is MEDIUM (silent) unless it
is also a large share of infiniFi's total assets, when it is HIGH. HIGH infiniFi
alerts trigger the emergency dispatch in ``utils/dispatch.py``. Plain RWAEscrows send their funds
to an off-chain receiver, so there is nothing on-chain to compare them with;
they are skipped.
"""

from dataclasses import dataclass
from decimal import Decimal

from eth_utils import to_checksum_address
from web3.exceptions import BadFunctionCallOutput, ContractLogicError

from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import (
    HOURLY_CACHE_STALE_AFTER_SECONDS,
    cache_filename,
    get_fresh_last_value_for_key_from_file,
    write_last_value_with_timestamp_to_file,
)
from utils.chains import Chain
from utils.config import Config
from utils.infinifi_escrow import fetch_whitelist_targets
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager, Web3Client

PROTOCOL = "infinifi"
logger = get_logger(f"{PROTOCOL}.escrow_valuation")

FARM_REGISTRY = "0xF5f2718708f471e43968271956CC01aaA8c46119"
ACCOUNTING = "0x7A5C5dbA4fbD0e1e1A2eCDBe752fAe55f6E842B3"
EXPLORER = "https://etherscan.io/address"

# Alert when the reported value exceeds the holdings' value by more than this share of the reported value.
GAP_THRESHOLD = Decimal(Config.get_env("INFINIFI_ESCROW_GAP_THRESHOLD", "0.03") or "0.03")
# Escalate to HIGH when the gap is also more than this share of infiniFi's total assets.
GAP_TVL_HIGH_THRESHOLD = Decimal(Config.get_env("INFINIFI_ESCROW_GAP_TVL_HIGH_THRESHOLD", "0.05") or "0.05")

# Stablecoins counted at 1 USD when a router holds them (the escrow's own asset is always at par).
PAR_TOKENS = {
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
    "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
    "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
}

# Midas RequestStatus.Pending
_PENDING = 0
_CALL_ERRORS = (ContractLogicError, BadFunctionCallOutput, ValueError)


def _view(name: str, outputs: list[str], inputs: tuple[str, ...] = ()) -> dict:
    return {
        "name": name,
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": kind} for kind in inputs],
        "outputs": [{"name": "", "type": kind} for kind in outputs],
    }


_REGISTRY_ABI = [_view("getFarms", ["address[]"])]
_ACCOUNTING_ABI = [_view("totalAssetsValue", ["uint256"])]
_FARM_ABI = [_view("escrow", ["address"])]
_ESCROW_ABI = [
    _view("totalAssets", ["uint256"]),
    _view("assetToken", ["address"]),
    _view("whitelist", ["bool"], ("address",)),
]
_ERC20_ABI = [_view("balanceOf", ["uint256"], ("address",)), _view("decimals", ["uint8"]), _view("symbol", ["string"])]
# Midas vaults return a static struct, which decodes the same as these flat outputs.
_REQUEST = ["address", "address", "uint8", "uint256", "uint256", "uint256"]
_MIDAS_VAULT_ABI = [
    _view("mToken", ["address"]),
    _view("mTokenDataFeed", ["address"]),
    _view("redeemRequests", _REQUEST, ("uint256",)),
    _view("mintRequests", _REQUEST, ("uint256",)),
]
_DATA_FEED_ABI = [_view("aggregator", ["address"])]
_AGGREGATOR_ABI = [
    _view("underlyingFeed", ["address"]),
    _view("decimals", ["uint8"]),
    _view("latestRoundData", ["uint80", "int256", "uint256", "uint256", "uint80"]),
]


@dataclass(frozen=True)
class Position:
    """Something the router owns, with its USD value when it can be priced."""

    description: str
    value: Decimal | None


@dataclass(frozen=True)
class EscrowValuation:
    """A router's reported value next to the value of what it holds."""

    farm: str
    escrow: str
    reported: Decimal
    positions: tuple[Position, ...]
    # infiniFi's total assets in USD (Accounting.totalAssetsValue), for sizing the gap.
    protocol_assets: Decimal = Decimal(0)

    @property
    def unpriced(self) -> tuple[Position, ...]:
        return tuple(position for position in self.positions if position.value is None)

    @property
    def value(self) -> Decimal:
        return sum((position.value for position in self.positions if position.value is not None), Decimal(0))

    @property
    def gap(self) -> Decimal:
        """Reported minus held value; positive when the router reports more than it holds."""
        return self.reported - self.value

    @property
    def gap_ratio(self) -> Decimal:
        return self.gap / self.reported if self.reported > 0 else Decimal(0)

    @property
    def gap_share_of_protocol(self) -> Decimal:
        return self.gap / self.protocol_assets if self.protocol_assets > 0 else Decimal(0)


@dataclass(frozen=True)
class _MidasVault:
    address: str
    mtoken: str
    nav: Decimal


def _link(address: str) -> str:
    return f"[{address}]({EXPLORER}/{address})"


def _amount(raw: int, decimals: int) -> Decimal:
    return Decimal(raw) / (Decimal(10) ** decimals)


def _router_escrows(client: Web3Client, block_identifier: int) -> list[tuple[str, str]]:
    """(farm, escrow) for every registered farm whose escrow is an RWAEscrowRouter."""
    routers: list[tuple[str, str]] = []
    for farm in (
        client.get_contract(FARM_REGISTRY, _REGISTRY_ABI).functions.getFarms().call(block_identifier=block_identifier)
    ):
        try:
            escrow = to_checksum_address(
                client.get_contract(farm, _FARM_ABI).functions.escrow().call(block_identifier=block_identifier)
            )
            # Only a router has the externalCall whitelist; plain escrows revert here.
            client.get_contract(escrow, _ESCROW_ABI).functions.whitelist(escrow).call(block_identifier=block_identifier)
        except _CALL_ERRORS:
            continue
        routers.append((to_checksum_address(farm), escrow))
    return routers


def _midas_nav(client: Web3Client, data_feed: str, block_identifier: int) -> Decimal:
    """Midas's NAV for an mToken: the feed under the vault's (possibly adjusted) aggregator."""
    aggregator = (
        client.get_contract(data_feed, _DATA_FEED_ABI).functions.aggregator().call(block_identifier=block_identifier)
    )
    try:
        # CustomAggregatorV3CompatibleFeedAdjusted ("PriceLowered") wraps the NAV feed.
        aggregator = (
            client.get_contract(aggregator, _AGGREGATOR_ABI)
            .functions.underlyingFeed()
            .call(block_identifier=block_identifier)
        )
    except _CALL_ERRORS:
        pass
    contract = client.get_contract(aggregator, _AGGREGATOR_ABI).functions
    answer = contract.latestRoundData().call(block_identifier=block_identifier)[1]
    return _amount(int(answer), int(contract.decimals().call(block_identifier=block_identifier)))


def _midas_vault(client: Web3Client, target: str, block_identifier: int) -> _MidasVault | None:
    """The whitelisted target as a Midas deposit or redemption vault, or None when it is not one."""
    contract = client.get_contract(target, _MIDAS_VAULT_ABI).functions
    try:
        mtoken = to_checksum_address(contract.mToken().call(block_identifier=block_identifier))
        feed = to_checksum_address(contract.mTokenDataFeed().call(block_identifier=block_identifier))
    except _CALL_ERRORS:
        return None
    return _MidasVault(target, mtoken, _midas_nav(client, feed, block_identifier))


def _token_position(
    client: Web3Client, escrow: str, token: str, price: Decimal | None, block_identifier: int
) -> Position | None:
    """The router's balance of ``token``, or None when it holds none or ``token`` is not an ERC20."""
    contract = client.get_contract(token, _ERC20_ABI).functions
    try:
        balance = int(contract.balanceOf(escrow).call(block_identifier=block_identifier))
        if balance == 0:
            return None
        amount = _amount(balance, int(contract.decimals().call(block_identifier=block_identifier)))
        symbol = str(contract.symbol().call(block_identifier=block_identifier))
    except _CALL_ERRORS:
        return None
    if price is None:
        return Position(f"{amount:,.2f} {symbol} ({_link(token)}): no price", None)
    return Position(f"{amount:,.2f} {symbol} × {price:,.6f}", amount * price)


def _pending_requests(
    client: Web3Client, vault: _MidasVault, escrow: str, symbol: str, block_identifier: int
) -> list[Position]:
    """Midas requests the router opened on this vault that are still pending.

    Midas request events index the request id first and the user second, so the
    router's requests are the vault's logs whose second indexed topic is the router.
    """
    user_topic = "0x" + "0" * 24 + escrow[2:].lower()
    logs = client.eth.get_logs(
        {"address": vault.address, "topics": [None, None, user_topic], "fromBlock": 0, "toBlock": block_identifier}
    )
    request_ids = sorted({int.from_bytes(bytes(log["topics"][1]), "big") for log in logs})
    contract = client.get_contract(vault.address, _MIDAS_VAULT_ABI).functions
    positions = []
    for request_id in request_ids:
        try:
            sender, _, status, amount_mtoken, _, _ = contract.redeemRequests(request_id).call(
                block_identifier=block_identifier
            )
            redeem = True
        except _CALL_ERRORS:
            sender, _, status, _, usd_without_fees, _ = contract.mintRequests(request_id).call(
                block_identifier=block_identifier
            )
            redeem = False
        if to_checksum_address(sender) != escrow or status != _PENDING:
            continue
        if redeem:
            amount = _amount(int(amount_mtoken), 18)
            positions.append(
                Position(
                    f"Pending Midas redemption #{request_id} at {_link(vault.address)}: {amount:,.2f} {symbol} "
                    f"× {vault.nav:,.6f}",
                    amount * vault.nav,
                )
            )
        else:
            usd = _amount(int(usd_without_fees), 18)
            positions.append(Position(f"Pending Midas deposit #{request_id} at {_link(vault.address)}", usd))
    return positions


def protocol_assets(client: Web3Client, block_identifier: int) -> Decimal:
    """infiniFi's total assets in USD, as its Accounting contract values them (18 decimals)."""
    return _amount(
        int(
            client.get_contract(ACCOUNTING, _ACCOUNTING_ABI)
            .functions.totalAssetsValue()
            .call(block_identifier=block_identifier)
        ),
        18,
    )


def value_router(
    client: Web3Client,
    farm: str,
    escrow: str,
    total_assets: Decimal = Decimal(0),
    *,
    block_identifier: int | None = None,
) -> EscrowValuation:
    """Value a router's holdings and pending requests using one block for every read."""
    if block_identifier is None:
        block_identifier = int(client.eth.block_number)
    escrow_contract = client.get_contract(escrow, _ESCROW_ABI).functions
    asset = to_checksum_address(escrow_contract.assetToken().call(block_identifier=block_identifier))
    asset_decimals = int(
        client.get_contract(asset, _ERC20_ABI).functions.decimals().call(block_identifier=block_identifier)
    )
    reported = _amount(int(escrow_contract.totalAssets().call(block_identifier=block_identifier)), asset_decimals)

    # Removing call permission does not remove balances or settle outstanding requests.
    targets = fetch_whitelist_targets(client, escrow, block_identifier=block_identifier, include_disabled=True)
    vaults = [
        vault for vault in (_midas_vault(client, target, block_identifier) for target in targets) if vault is not None
    ]
    nav_by_mtoken = {vault.mtoken.lower(): vault.nav for vault in vaults}
    vault_addresses = {vault.address.lower() for vault in vaults}

    tokens: dict[str, str] = {asset.lower(): asset}
    for address in [*targets, *(vault.mtoken for vault in vaults)]:
        if address.lower() not in vault_addresses:
            tokens.setdefault(address.lower(), address)

    positions: list[Position] = []
    for key, token in tokens.items():
        price = Decimal(1) if key == asset.lower() or key in PAR_TOKENS else nav_by_mtoken.get(key)
        position = _token_position(client, escrow, token, price, block_identifier)
        if position is not None:
            positions.append(position)

    for vault in vaults:
        symbol = str(
            client.get_contract(vault.mtoken, _ERC20_ABI).functions.symbol().call(block_identifier=block_identifier)
        )
        positions.extend(_pending_requests(client, vault, escrow, symbol, block_identifier))

    return EscrowValuation(
        farm=farm, escrow=escrow, reported=reported, positions=tuple(positions), protocol_assets=total_assets
    )


def _holdings_text(valuation: EscrowValuation) -> str:
    lines = []
    for position in valuation.positions:
        value = "" if position.value is None else f" = ${position.value:,.2f}"
        lines.append(f"- {position.description}{value}")
    return "\n".join(lines) or "- nothing"


def gap_message(valuation: EscrowValuation) -> str:
    return (
        "⚠️ *Infinifi Escrow Valuation Gap*\n\n"
        f"Farm: {_link(valuation.farm)}\n"
        f"Escrow (RWAEscrowRouter): {_link(valuation.escrow)}\n"
        f"Reported totalAssets: ${valuation.reported:,.2f}\n"
        f"Holdings at Midas NAV / par: ${valuation.value:,.2f}\n"
        f"Gap: ${valuation.gap:,.2f} ({valuation.gap_ratio:.2%} of reported, threshold {GAP_THRESHOLD:.0%})\n"
        f"Share of infiniFi total assets (${valuation.protocol_assets:,.2f}): {valuation.gap_share_of_protocol:.2%} "
        f"(HIGH above {GAP_TVL_HIGH_THRESHOLD:.0%})\n\n"
        f"Holdings:\n{_holdings_text(valuation)}\n\n"
        "The reported value grows at the rate manager's set rate, whatever the tokens are worth."
    )


def unpriced_message(valuation: EscrowValuation) -> str:
    return (
        "⚠️ *Infinifi Escrow Holds Unpriced Tokens*\n\n"
        f"Farm: {_link(valuation.farm)}\n"
        f"Escrow (RWAEscrowRouter): {_link(valuation.escrow)}\n"
        f"Reported totalAssets: ${valuation.reported:,.2f}\n\n"
        f"Holdings:\n{_holdings_text(valuation)}\n\n"
        "The valuation check is skipped until every holding has a price."
    )


# Alert level stored per cache key: 0 clear, 1 MEDIUM sent, 2 HIGH sent.
_LEVELS = {AlertSeverity.MEDIUM: 1, AlertSeverity.HIGH: 2}


def _alert_once(cache_key: str, message: str, severity: AlertSeverity) -> None:
    """Alert on the first breach, and again only if it escalates; stay quiet until it clears or goes stale."""
    level = _LEVELS[severity]
    sent = int(get_fresh_last_value_for_key_from_file(cache_filename, cache_key, HOURLY_CACHE_STALE_AFTER_SECONDS))
    if sent < level:
        send_alert(Alert(severity, message, PROTOCOL))
    write_last_value_with_timestamp_to_file(cache_filename, cache_key, level)


def _clear(cache_key: str) -> None:
    write_last_value_with_timestamp_to_file(cache_filename, cache_key, 0)


def check_valuation(valuation: EscrowValuation) -> None:
    """Send or clear the gap and unpriced-holdings alerts for one router."""
    gap_key = f"{PROTOCOL}_escrow_gap_{valuation.escrow.lower()}"
    unpriced_key = f"{PROTOCOL}_escrow_unpriced_{valuation.escrow.lower()}"
    if valuation.unpriced:
        _alert_once(unpriced_key, unpriced_message(valuation), AlertSeverity.MEDIUM)
        return
    _clear(unpriced_key)
    if valuation.gap_ratio > GAP_THRESHOLD:
        high = valuation.gap_share_of_protocol > GAP_TVL_HIGH_THRESHOLD
        _alert_once(gap_key, gap_message(valuation), AlertSeverity.HIGH if high else AlertSeverity.MEDIUM)
    else:
        _clear(gap_key)


def main() -> None:
    client = ChainManager.get_client(Chain.MAINNET)
    block_identifier = int(client.eth.block_number)
    total_assets = protocol_assets(client, block_identifier)
    for farm, escrow in _router_escrows(client, block_identifier):
        valuation = value_router(client, farm, escrow, total_assets, block_identifier=block_identifier)
        logger.info(
            "Escrow %s: reported %s, holdings %s, gap %s (%.2f%%)",
            escrow,
            f"{valuation.reported:,.2f}",
            f"{valuation.value:,.2f}",
            f"{valuation.gap:,.2f}",
            valuation.gap_ratio * 100,
        )
        check_valuation(valuation)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(main, PROTOCOL)

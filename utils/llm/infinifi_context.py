"""Resolve Infinifi farm and configured-token context for governance calls.

Infinifi's RWA rate manager receives an escrow address, while the useful farm
identity and configured token sit behind that escrow. The generic related-token
resolver only inspects the call target's getters, so it cannot discover this
relationship.

This adapter is deliberately narrow: it runs only for Infinifi on Ethereum,
identifies RWAEscrow contracts from their verified ABI, reads their accounting
asset and owner on-chain, matches and verifies the owning farm, and resolves
the escrow's whitelist from its events.

What the calls mean, from the verified sources (RWAEscrow, RWAEscrowRouter,
RWAEscrowRateManager):

- An escrow's value is a number its ``keeper`` reports (``totalAssets``), not a
  token balance. Plain escrows forward deposits to an off-chain ``receiver``.
- ``RWAEscrowRateManager.setRate(escrow, rate)`` sets an annual accrual rate in
  WAD around 1e18 (1.0868e18 = +8.68% a year), bounded to ±20%; the manager's
  permissionless ``harvest`` books it into ``totalAssets`` over time. Reports had
  read these as relative changes of a raw number ("raises the rate by 1.09%").
- ``governanceUpdateTotalAssets(escrow, assets)`` overwrites ``totalAssets`` with
  no bound, booking the difference as profit or loss.
- On an ``RWAEscrowRouter`` (the escrow holds the assets itself), ``whitelist``
  is the set of contracts ``externalCall`` may call, with any calldata, by any
  MANUAL_REBALANCER holder — the team multisig among them, with no timelock. A
  whitelisted token's router balance can therefore be moved by that call.
"""

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from eth_utils import to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.formatting import format_decimal_amount, normalize_token_amount
from utils.http_client import fetch_json
from utils.infinifi_escrow import fetch_whitelist_targets
from utils.llm.report import address_link, iter_address_values
from utils.logger import get_logger
from utils.source_context import fetch_abi_entries, get_contract_label
from utils.web3_wrapper import ChainManager

logger = get_logger("utils.llm.infinifi_context")

INFINIFI_API_URL = "https://api.infinifi.xyz/api/protocol/data"
MAX_CANDIDATE_ADDRESSES = 8

_ESCROW_GETTERS_ABI = [
    {
        "name": "assetToken",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "owner",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "totalAssets",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

_ESCROW_DETAIL_ABI = [
    {"name": name, "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": kind}]}
    for name, kind in (("receiver", "address"), ("keeper", "address"), ("lastUpdatedAt", "uint256"))
]

_RATE_MANAGER_ABI = [
    {
        "name": "rates",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "escrow", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]

_ROUTER_ABI = [
    {
        "name": "whitelist",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "target", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    }
]

_BALANCE_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]

# RWAEscrowRateManager bounds (BASE_RATE ± 20%); rates are annual, WAD-scaled around 1e18.
_RATE_BASE = 10**18
_RATE_BOUND = 2 * 10**17

_TOKEN_NAME_ABI = [
    {
        "name": "name",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
]

_FARM_ESCROW_ABI = [
    {
        "name": "escrow",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    }
]


@dataclass(frozen=True)
class TokenContext:
    """Verified token metadata."""

    address: str
    name: str
    symbol: str
    decimals: int


@dataclass(frozen=True)
class RateChange:
    """A setRate call against the rate currently stored for the escrow."""

    current_raw: int
    proposed_raw: int


@dataclass(frozen=True)
class WhitelistChange:
    """A setWhitelist call on a router escrow, with the flag before it."""

    target: str
    label: str
    enabled_before: bool
    enabled_after: bool
    # Set when the target is an ERC20: the router's own balance of it.
    token: TokenContext | None = None
    escrow_balance_raw: int | None = None


@dataclass(frozen=True)
class InfinifiEscrowContext:
    """Resolved context for one Infinifi RWA escrow."""

    escrow_address: str
    farm_address: str
    farm_name: str
    farm_slug: str
    accounting_asset: TokenContext
    total_assets_raw: int
    # Whitelisted call targets that verify as ERC20 tokens (router escrows only).
    configured_tokens: tuple[TokenContext, ...]
    receiver: str = ""
    keeper: str = ""
    keeper_label: str = ""
    last_updated_at: int = 0
    is_router: bool = False
    # Whitelisted call targets that are not ERC20s, as (address, label).
    whitelisted_contracts: tuple[tuple[str, str], ...] = ()
    rate_change: RateChange | None = None
    # governanceUpdateTotalAssets values this transaction writes, in call order.
    assets_overrides: tuple[int, ...] = ()
    whitelist_changes: tuple[WhitelistChange, ...] = ()

    @property
    def addresses(self) -> list[str]:
        """Addresses introduced by this context for explorer-link generation."""
        extra = [self.receiver, self.keeper] if self.receiver else []
        return list(
            dict.fromkeys(
                [
                    self.escrow_address,
                    self.farm_address,
                    self.accounting_asset.address,
                    *(token.address for token in self.configured_tokens),
                    *(address for address, _ in self.whitelisted_contracts),
                    *(change.target for change in self.whitelist_changes),
                    *extra,
                ]
            )
        )

    @property
    def labels(self) -> dict[str, str]:
        """Useful labels for addresses not present in the original calldata."""
        labels = {
            self.farm_address: self.farm_name or self.farm_slug,
            self.accounting_asset.address: _token_label(self.accounting_asset),
        }
        labels.update({token.address: _token_label(token) for token in self.configured_tokens})
        labels.update(dict(self.whitelisted_contracts))
        if self.keeper and self.keeper_label:
            labels[self.keeper] = self.keeper_label
        return {address: label for address, label in labels.items() if label}

    def amount(self, raw: int) -> str:
        """An amount in the escrow's accounting asset."""
        asset = self.accounting_asset
        return f"{format_decimal_amount(normalize_token_amount(raw, asset.decimals))} {asset.symbol}"

    def custody_line(self) -> str:
        """Where the escrow's value sits and who reports it."""
        if self.is_router:
            custody = (
                "RWAEscrowRouter: the escrow holds the assets itself (receiver is the escrow), and any "
                "MANUAL_REBALANCER holder can call its whitelisted targets with arbitrary calldata via externalCall — "
                "no timelock"
            )
        elif self.receiver:
            custody = f"RWAEscrow: deposits are forwarded to the off-chain receiver {self.receiver}"
        else:
            return ""
        keeper = f"{self.keeper} ({self.keeper_label})" if self.keeper_label else self.keeper
        return f"{custody}. totalAssets is a reported value, not a token balance; it is set by the keeper {keeper}."

    def rate_lines(self) -> list[str]:
        """setRate and governanceUpdateTotalAssets, in annual percentages and asset units."""
        lines: list[str] = []
        if self.rate_change is not None:
            current, proposed = self.rate_change.current_raw, self.rate_change.proposed_raw
            before = "unset (no accrual)" if current == 0 else f"{_rate_percent(current)} a year (raw {current})"
            yearly = self.total_assets_raw * abs(proposed - _RATE_BASE) // _RATE_BASE
            direction = "accrues" if proposed >= _RATE_BASE else "writes down"
            lines.append(
                f"setRate: annual accrual rate {before} → {_rate_percent(proposed)} a year (raw {proposed}); the rate "
                f"is WAD-scaled around 1e18, bounded to ±20%. On the current totalAssets it {direction} about "
                f"{self.amount(yearly)} a year, booked into totalAssets whenever harvest runs; setRate harvests at "
                "the old rate first."
            )
        for assets in self.assets_overrides:
            delta = assets - self.total_assets_raw
            effect = "profit" if delta >= 0 else "loss"
            lines.append(
                f"governanceUpdateTotalAssets: overwrites totalAssets {self.amount(self.total_assets_raw)} → "
                f"{self.amount(assets)} with no bound, booking a {effect} of {self.amount(abs(delta))}."
            )
        return lines

    def whitelist_lines(self) -> list[str]:
        """Each setWhitelist change, and what a whitelisted token means for the router's holdings."""
        lines = []
        for change in self.whitelist_changes:
            state = f"{str(change.enabled_before).lower()} → {str(change.enabled_after).lower()}"
            line = f"setWhitelist {change.target} ({change.label or 'unlabelled'}): {state}"
            if change.token is not None and change.escrow_balance_raw is not None:
                held = format_decimal_amount(normalize_token_amount(change.escrow_balance_raw, change.token.decimals))
                line += f"; ERC20 {change.token.symbol}, router balance {held} {change.token.symbol}"
                if change.enabled_after:
                    line += " — once whitelisted, externalCall can transfer or approve that balance"
            lines.append(line)
        return lines


def _rate_percent(raw: int) -> str:
    """A WAD rate around 1e18 as a signed annual percentage, e.g. ``+8.683%``."""
    percent = Decimal(raw - _RATE_BASE) * 100 / Decimal(_RATE_BASE)
    return f"{percent:+.3f}".rstrip("0").rstrip(".") + "%"


@dataclass(frozen=True)
class _EscrowState:
    address: str
    farm_address: str
    asset_address: str
    total_assets_raw: int
    receiver: str = ""
    keeper: str = ""
    last_updated_at: int = 0
    is_router: bool = False


@dataclass(frozen=True)
class _FarmRecord:
    address: str
    label: str
    slug: str


@dataclass(frozen=True)
class _TokenCandidate:
    address: str
    fallback_name: str


def _token_label(token: TokenContext) -> str:
    """Human label that includes both descriptive name and ticker metadata."""
    name = token.name or token.symbol
    return f"{name} ({token.symbol}, {token.decimals} dec)"


def _abi_function_names(entries: list[dict]) -> set[str]:
    """Function names present in a verified ABI."""
    return {str(entry.get("name")) for entry in entries if entry.get("type") == "function" and entry.get("name")}


def _looks_like_escrow(entries: list[dict]) -> bool:
    """Return whether an ABI exposes the required RWAEscrow state getters."""
    return {"assetToken", "owner", "totalAssets"}.issubset(_abi_function_names(entries))


def _candidate_addresses(targets_and_calls: list[tuple[str, DecodedCall]]) -> list[str]:
    """Targets and address arguments that could be an Infinifi escrow."""
    addresses: list[str] = []
    seen: set[str] = set()
    for target, call in targets_and_calls:
        raw_addresses = [target]
        for type_str, value in call.params:
            raw_addresses.extend(iter_address_values(type_str, value))
        for raw in raw_addresses:
            if not raw or raw.lower() in seen:
                continue
            try:
                checksum = to_checksum_address(raw)
            except ValueError:
                continue
            seen.add(raw.lower())
            addresses.append(checksum)
            if len(addresses) >= MAX_CANDIDATE_ADDRESSES:
                return addresses
    return addresses


def _read_escrow_state(chain_id: int, address: str) -> _EscrowState | None:
    """Read the three state values needed to identify and describe an escrow."""
    entries = fetch_abi_entries(chain_id, address) or []
    if not _looks_like_escrow(entries):
        return None

    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    contract = client.get_contract(to_checksum_address(address), _ESCROW_GETTERS_ABI + _ESCROW_DETAIL_ABI)
    with client.batch_requests() as batch:
        batch.add(contract.functions.assetToken())
        batch.add(contract.functions.owner())
        batch.add(contract.functions.totalAssets())
        batch.add(contract.functions.receiver())
        batch.add(contract.functions.keeper())
        batch.add(contract.functions.lastUpdatedAt())
        asset_address, farm_address, total_assets, receiver, keeper, last_updated = client.execute_batch(batch)
    return _EscrowState(
        address=to_checksum_address(address),
        farm_address=to_checksum_address(str(farm_address)),
        asset_address=to_checksum_address(str(asset_address)),
        total_assets_raw=int(total_assets),
        receiver=to_checksum_address(str(receiver)),
        keeper=to_checksum_address(str(keeper)),
        last_updated_at=int(last_updated),
        is_router={"whitelist", "externalCall"}.issubset(_abi_function_names(entries)),
    )


@lru_cache(maxsize=1)
def _fetch_farm_records() -> tuple[_FarmRecord, ...]:
    """Fetch the current Infinifi farm list used by its public analytics API."""
    data = fetch_json(
        INFINIFI_API_URL,
        timeout=10,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Yearn Monitoring)"},
    )
    payload = data.get("data") if isinstance(data, dict) and data.get("code") == "OK" else None
    farms = payload.get("farms") if isinstance(payload, dict) else None
    if not isinstance(farms, list):
        return ()

    records: list[_FarmRecord] = []
    for farm in farms:
        if not isinstance(farm, dict):
            continue
        address = farm.get("address")
        if not isinstance(address, str):
            continue
        try:
            checksum = to_checksum_address(address)
        except ValueError:
            continue
        records.append(
            _FarmRecord(
                address=checksum,
                label=str(farm.get("label") or ""),
                slug=str(farm.get("name") or ""),
            )
        )
    return tuple(records)


def _farm_by_address(address: str, farms: tuple[_FarmRecord, ...]) -> _FarmRecord | None:
    """Find an API farm record by its checksummed or lowercase address."""
    return next((farm for farm in farms if farm.address.lower() == address.lower()), None)


def _farm_matches_escrow(chain_id: int, farm_address: str, escrow_address: str) -> bool:
    """Verify that an Infinifi farm identifies the candidate as its escrow."""
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    farm = client.get_contract(to_checksum_address(farm_address), _FARM_ESCROW_ABI)
    configured_escrow = str(to_checksum_address(str(farm.functions.escrow().call())))
    return configured_escrow.lower() == escrow_address.lower()


def _fetch_whitelist_targets(chain_id: int, escrow_address: str) -> list[str]:
    """Reconstruct the escrow's current whitelist from its emitted updates."""
    return fetch_whitelist_targets(ChainManager.get_client(Chain.from_chain_id(chain_id)), escrow_address)


def _read_token(chain_id: int, candidate: _TokenCandidate) -> TokenContext | None:
    """Verify token metadata and read its descriptive name."""
    metadata = fetch_erc20_metadata(chain_id, candidate.address)
    if metadata is None:
        return None

    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    token = client.get_contract(candidate.address, _TOKEN_NAME_ABI)
    try:
        name = str(token.functions.name().call())
    except Exception:  # noqa: BLE001 - some older ERC20s return bytes32 names
        name = candidate.fallback_name

    return TokenContext(
        address=candidate.address,
        name=name or candidate.fallback_name or metadata.symbol,
        symbol=metadata.symbol,
        decimals=metadata.decimals,
    )


def _resolve_configured_tokens(chain_id: int, escrow: _EscrowState) -> tuple[TokenContext, ...]:
    """Whitelisted non-accounting addresses that verify as ERC20 tokens."""
    return _resolve_whitelist(chain_id, escrow)[0]


def _resolve_whitelist(
    chain_id: int, escrow: _EscrowState
) -> tuple[tuple[TokenContext, ...], tuple[tuple[str, str], ...]]:
    """Every enabled whitelist target: ERC20s as tokens, the rest as labelled contracts.

    Non-token targets used to be dropped, yet they are what externalCall can call
    (a redemption vault, a swapper), so they are listed with their explorer label.
    """
    tokens: list[TokenContext] = []
    contracts: list[tuple[str, str]] = []
    for address in _fetch_whitelist_targets(chain_id, escrow.address):
        if address.lower() == escrow.asset_address.lower():
            continue
        token = _read_token(chain_id, _TokenCandidate(address, ""))
        if token is not None:
            tokens.append(token)
        else:
            contracts.append((address, _contract_label(chain_id, address)))
    return tuple(tokens), tuple(contracts)


def _contract_label(chain_id: int, address: str) -> str:
    """Best-effort verified contract name for a whitelisted target."""
    try:
        return get_contract_label(chain_id, address)
    except Exception as error:  # noqa: BLE001 - a label is enrichment
        logger.info("Infinifi whitelist label failed for %s: %s", address, error)
        return ""


def _escrow_address_arg(call: DecodedCall) -> str | None:
    """The first argument of a rate-manager call, when it is an address."""
    if not call.params or call.params[0][0] != "address":
        return None
    try:
        return to_checksum_address(str(call.params[0][1]))
    except ValueError:
        return None


def _uint_arg(call: DecodedCall, position: int) -> int | None:
    """The call's ``position``-th argument when it is an unsigned integer."""
    if len(call.params) <= position:
        return None
    type_str, value = call.params[position]
    return int(value) if type_str.startswith("uint") and isinstance(value, int) else None


def _rate_and_overrides(
    chain_id: int, escrow: str, targets_and_calls: list[tuple[str, DecodedCall]]
) -> tuple[RateChange | None, tuple[int, ...]]:
    """setRate / governanceUpdateTotalAssets calls that name this escrow, with the stored rate."""
    rate_call: tuple[str, int] | None = None
    overrides: list[int] = []
    for target, call in targets_and_calls:
        if _escrow_address_arg(call) != escrow:
            continue
        value = _uint_arg(call, 1)
        if value is None:
            continue
        if call.function_name == "setRate":
            rate_call = (target, value)
        elif call.function_name == "governanceUpdateTotalAssets":
            overrides.append(value)
    if rate_call is None:
        return None, tuple(overrides)
    manager, proposed = rate_call
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    current = int(client.get_contract(to_checksum_address(manager), _RATE_MANAGER_ABI).functions.rates(escrow).call())
    return RateChange(current_raw=current, proposed_raw=proposed), tuple(overrides)


def _whitelist_changes(
    chain_id: int, escrow: _EscrowState, targets_and_calls: list[tuple[str, DecodedCall]]
) -> tuple[WhitelistChange, ...]:
    """setWhitelist calls on this router escrow, with the flag before and any token balance it holds."""
    calls = [
        call
        for target, call in targets_and_calls
        if call.function_name == "setWhitelist" and target.lower() == escrow.address.lower() and len(call.params) == 2
    ]
    if not calls:
        return ()
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    router = client.get_contract(escrow.address, _ROUTER_ABI)
    changes = []
    for call in calls:
        (_, raw_target), (_, enabled) = call.params
        target = to_checksum_address(str(raw_target))
        before = bool(router.functions.whitelist(target).call())
        token = _read_token(chain_id, _TokenCandidate(target, ""))
        balance = None
        if token is not None:
            balance = int(client.get_contract(target, _BALANCE_ABI).functions.balanceOf(escrow.address).call())
        label = _token_label(token) if token else _contract_label(chain_id, target)
        changes.append(WhitelistChange(target, label, before, bool(enabled), token, balance))
    return tuple(changes)


def _keeper_label(chain_id: int, keeper: str) -> str:
    """Name the keeper, which is normally the RWAEscrowRateManager."""
    return _contract_label(chain_id, keeper) if keeper else ""


def resolve_infinifi_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[InfinifiEscrowContext]:
    """Resolve deterministic farm context for Infinifi escrow-related calls."""
    if protocol.lower() != "infinifi" or chain_id != Chain.MAINNET.chain_id:
        return []

    contexts: list[InfinifiEscrowContext] = []
    farms: tuple[_FarmRecord, ...] | None = None
    for address in _candidate_addresses(targets_and_calls):
        try:
            escrow = _read_escrow_state(chain_id, address)
            if escrow is None:
                continue
            accounting_asset = _read_token(chain_id, _TokenCandidate(escrow.asset_address, ""))
            if accounting_asset is None:
                logger.info(
                    "Infinifi escrow %s: ERC20 metadata unavailable or incompatible for accounting asset %s",
                    escrow.address,
                    escrow.asset_address,
                )
                continue
            if farms is None:
                farms = _fetch_farm_records()
                if not farms:
                    logger.info("Infinifi farm records unavailable; skipping escrow %s", escrow.address)
                    continue
            farm = _farm_by_address(escrow.farm_address, farms)
            if farm is None:
                logger.info(
                    "Infinifi escrow %s: owner %s is not a known Infinifi farm", escrow.address, escrow.farm_address
                )
                continue
            if not _farm_matches_escrow(chain_id, farm.address, escrow.address):
                logger.info("Infinifi escrow %s: farm %s does not reference this escrow", escrow.address, farm.address)
                continue
            try:
                configured_tokens, whitelisted_contracts = _resolve_whitelist(chain_id, escrow)
            except Exception as error:  # noqa: BLE001 - optional token context must not block farm context
                logger.info("Infinifi token resolution failed for %s: %s", escrow.address, error)
                configured_tokens, whitelisted_contracts = (), ()
            try:
                rate_change, overrides = _rate_and_overrides(chain_id, escrow.address, targets_and_calls)
                whitelist_changes = _whitelist_changes(chain_id, escrow, targets_and_calls)
            except Exception as error:  # noqa: BLE001 - call context is enrichment
                logger.info("Infinifi escrow call context failed for %s: %s", escrow.address, error)
                rate_change, overrides, whitelist_changes = None, (), ()
            contexts.append(
                InfinifiEscrowContext(
                    escrow_address=escrow.address,
                    farm_address=escrow.farm_address,
                    farm_name=farm.label,
                    farm_slug=farm.slug,
                    accounting_asset=accounting_asset,
                    total_assets_raw=escrow.total_assets_raw,
                    configured_tokens=configured_tokens,
                    receiver=escrow.receiver,
                    keeper=escrow.keeper,
                    keeper_label=_keeper_label(chain_id, escrow.keeper),
                    last_updated_at=escrow.last_updated_at,
                    is_router=escrow.is_router,
                    whitelisted_contracts=whitelisted_contracts,
                    rate_change=rate_change,
                    assets_overrides=overrides,
                    whitelist_changes=whitelist_changes,
                )
            )
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Infinifi context resolution failed for %s: %s", address, error)
    return contexts


def format_infinifi_prompt(contexts: list[InfinifiEscrowContext]) -> str:
    """Render verified Infinifi context for the LLM prompt."""
    sections: list[str] = []
    for context in contexts:
        asset = context.accounting_asset
        total_assets = format_decimal_amount(normalize_token_amount(context.total_assets_raw, asset.decimals))
        lines = [
            f"Escrow: {context.escrow_address}",
            f"Farm: {context.farm_address} ({context.farm_name or context.farm_slug or 'name unavailable'})",
            f"Accounting asset: {asset.address} ({asset.name}, {asset.symbol}, {asset.decimals} decimals)",
            f"Current escrow totalAssets: {context.total_assets_raw} raw units = {total_assets} {asset.symbol}",
        ]
        if custody := context.custody_line():
            lines.append(custody)
        for token in context.configured_tokens:
            lines.append(
                f"Whitelisted ERC20 call target: {token.address} "
                f"({token.name}, {token.symbol}, {token.decimals} decimals)"
            )
        for address, label in context.whitelisted_contracts:
            lines.append(f"Whitelisted contract call target: {address} ({label or 'unlabelled'})")
        lines.extend(context.rate_lines())
        lines.extend(context.whitelist_lines())
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def format_infinifi_report(
    contexts: list[InfinifiEscrowContext],
    chain_id: int,
    labels: dict[str, str],
) -> str:
    """Render the deterministic Infinifi farm section for the gist report."""
    sections: list[str] = []
    for context in contexts:
        asset = context.accounting_asset
        total_assets = format_decimal_amount(normalize_token_amount(context.total_assets_raw, asset.decimals))
        farm_name = context.farm_name or context.farm_slug or "Unknown farm"
        lines = [
            f"- **Farm:** {farm_name} — {address_link(context.farm_address, chain_id)}",
            f"- **Escrow:** {address_link(context.escrow_address, chain_id, labels)}",
            f"- **Accounting asset:** {asset.name} (`{asset.symbol}`, {asset.decimals} decimals) — "
            f"{address_link(asset.address, chain_id)}",
            f"- **Current `totalAssets`:** `{total_assets} {asset.symbol}` (`{context.total_assets_raw:,}` raw units)",
        ]
        if custody := context.custody_line():
            lines.append(f"- **Custody:** {custody}")
        if context.configured_tokens or context.whitelisted_contracts:
            lines.append("- **Whitelisted call targets** (`externalCall`):")
            for token in context.configured_tokens:
                lines.append(
                    f"  - {token.name} (`{token.symbol}`, {token.decimals} decimals) — "
                    f"{address_link(token.address, chain_id)}"
                )
            for address, label in context.whitelisted_contracts:
                lines.append(f"  - {address_link(address, chain_id, {address: label} if label else labels)}")
        lines.extend(f"- {line}" for line in context.rate_lines())
        lines.extend(f"- {line}" for line in context.whitelist_lines())
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def reset_cache() -> None:
    """Reset process caches for tests or long-running workers."""
    _fetch_farm_records.cache_clear()

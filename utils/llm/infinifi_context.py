"""Resolve Infinifi farm and configured-token context for governance calls.

Infinifi's RWA rate manager receives an escrow address, while the useful farm
identity and configured token sit behind that escrow. The generic related-token
resolver only inspects the call target's getters, so it cannot discover this
relationship.

This adapter is deliberately narrow: it runs only for Infinifi on Ethereum,
identifies RWAEscrow contracts from their verified ABI, reads their accounting
asset and owner on-chain, matches and verifies the owning farm, and resolves
configured non-accounting ERC20 targets from the escrow's whitelist events.
"""

from dataclasses import dataclass
from functools import lru_cache

from eth_utils import to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.formatting import format_decimal_amount, normalize_token_amount
from utils.http_client import fetch_json
from utils.llm.report import address_link, iter_address_values
from utils.logger import get_logger
from utils.source_context import fetch_abi_entries
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

_WHITELIST_EVENT_ABI = [
    {
        "anonymous": False,
        "name": "WhitelistUpdated",
        "type": "event",
        "inputs": [
            {"indexed": True, "name": "timestamp", "type": "uint256"},
            {"indexed": False, "name": "target", "type": "address"},
            {"indexed": False, "name": "enabled", "type": "bool"},
        ],
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
class InfinifiEscrowContext:
    """Resolved context for one Infinifi RWA escrow."""

    escrow_address: str
    farm_address: str
    farm_name: str
    farm_slug: str
    accounting_asset: TokenContext
    total_assets_raw: int
    configured_tokens: tuple[TokenContext, ...]

    @property
    def addresses(self) -> list[str]:
        """Addresses introduced by this context for explorer-link generation."""
        return [
            self.escrow_address,
            self.farm_address,
            self.accounting_asset.address,
            *(token.address for token in self.configured_tokens),
        ]

    @property
    def labels(self) -> dict[str, str]:
        """Useful labels for addresses not present in the original calldata."""
        labels = {
            self.farm_address: self.farm_name or self.farm_slug,
            self.accounting_asset.address: _token_label(self.accounting_asset),
        }
        labels.update({token.address: _token_label(token) for token in self.configured_tokens})
        return {address: label for address, label in labels.items() if label}


@dataclass(frozen=True)
class _EscrowState:
    address: str
    farm_address: str
    asset_address: str
    total_assets_raw: int


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
    contract = client.get_contract(to_checksum_address(address), _ESCROW_GETTERS_ABI)
    with client.batch_requests() as batch:
        batch.add(contract.functions.assetToken())
        batch.add(contract.functions.owner())
        batch.add(contract.functions.totalAssets())
        asset_address, farm_address, total_assets = client.execute_batch(batch)
    return _EscrowState(
        address=to_checksum_address(address),
        farm_address=to_checksum_address(str(farm_address)),
        asset_address=to_checksum_address(str(asset_address)),
        total_assets_raw=int(total_assets),
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
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    escrow = client.get_contract(to_checksum_address(escrow_address), _WHITELIST_EVENT_ABI)
    events = escrow.events.WhitelistUpdated().get_logs(from_block=0, to_block="latest")
    enabled_by_address: dict[str, tuple[str, bool]] = {}
    for event in events:
        args = event.get("args", {})
        raw_address = args.get("target")
        enabled = args.get("enabled")
        if not isinstance(raw_address, str) or not isinstance(enabled, bool):
            continue
        try:
            address = to_checksum_address(raw_address)
        except ValueError:
            continue
        enabled_by_address[address.lower()] = (address, enabled)
    return [address for address, enabled in enabled_by_address.values() if enabled]


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
    tokens: list[TokenContext] = []
    for address in _fetch_whitelist_targets(chain_id, escrow.address):
        if address.lower() == escrow.asset_address.lower():
            continue
        token = _read_token(chain_id, _TokenCandidate(address, ""))
        if token is not None:
            tokens.append(token)
        else:
            logger.debug("Infinifi whitelist target %s is not an ERC20; skipping", address)
    return tuple(tokens)


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
                    "Infinifi escrow %s: accounting asset %s is not an ERC20", escrow.address, escrow.asset_address
                )
                continue
            if farms is None:
                farms = _fetch_farm_records()
                if not farms:
                    logger.info("Infinifi farm records unavailable; skipping escrow %s", escrow.address)
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
                configured_tokens = _resolve_configured_tokens(chain_id, escrow)
            except Exception as error:  # noqa: BLE001 - optional token context must not block farm context
                logger.info("Infinifi token resolution failed for %s: %s", escrow.address, error)
                configured_tokens = ()
            contexts.append(
                InfinifiEscrowContext(
                    escrow_address=escrow.address,
                    farm_address=escrow.farm_address,
                    farm_name=farm.label,
                    farm_slug=farm.slug,
                    accounting_asset=accounting_asset,
                    total_assets_raw=escrow.total_assets_raw,
                    configured_tokens=configured_tokens,
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
        for token in context.configured_tokens:
            lines.append(
                f"Configured non-accounting ERC20 target: {token.address} "
                f"({token.name}, {token.symbol}, {token.decimals} decimals)"
            )
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
        if context.configured_tokens:
            lines.append("- **Configured non-accounting ERC-20 targets:**")
            for token in context.configured_tokens:
                lines.append(
                    f"  - {token.name} (`{token.symbol}`, {token.decimals} decimals) — "
                    f"{address_link(token.address, chain_id)}"
                )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def reset_cache() -> None:
    """Reset process caches for tests or long-running workers."""
    _fetch_farm_records.cache_clear()

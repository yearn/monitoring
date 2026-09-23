"""Resolve Infinifi Outland (cross-chain) context for governance calls.

Onboarding a chain to Infinifi's Outland spans several contracts, and each call
arrives at the LLM without the facts that make it reviewable:

- ``FarmRegistry.addFarms(type, farms)`` names the farm type by number only.
- ``Accounting.setOracle(asset, oracle)`` carries the oracle address, not the
  price it reports or what that price means for the asset's decimals.
- ``PortalHub.setVault(vault)`` keys vaults by the vault's own ``chainId()``, so
  the calldata cannot say whether it adds a chain or replaces a live vault.
- Connector calls (``enableChainAsset``, ``setCctpDomain``) do not show whether
  the connector can actually send to the chain: ``sendTokens`` also needs a
  peer, gas limit and selector from ``setConfiguration`` — and the peer is the
  destination-side address that receives the bridged funds.

This adapter runs only for Infinifi on Ethereum, identifies contracts by the
getters their verified ABI exposes, and reads the surrounding state on-chain.
"""

from dataclasses import dataclass
from decimal import Decimal

from eth_utils import to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.llm.abi_exposure import exposes
from utils.llm.report import address_link
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager

logger = get_logger("utils.llm.infinifi_outland_context")

PROTOCOL = "infinifi"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# FarmTypes library: the uint256 FarmRegistry.addFarms takes as its first argument.
FARM_TYPES: dict[int, tuple[str, str]] = {
    0: ("PROTOCOL", "not generating yield but capable of storing funds"),
    1: ("LIQUID", "instant principal withdrawals (e.g. Aave)"),
    2: ("MATURITY", "illiquid: principal is locked until the farm's maturity"),
}

# IOracle.price() scale: a whole token's value in the reference unit is
# price * 10**decimals / 1e36 (USDC is quoted at ~1e30 for a 1:1 price).
_ORACLE_PRICE_SCALE = Decimal(10) ** 36

_CONNECTOR_CHAIN_CALLS = {"enableChainAsset", "disableChainAsset", "setCctpDomain", "setConfiguration"}

_ADDRESS_OUT = [{"name": "", "type": "address"}]
_UINT_OUT = [{"name": "", "type": "uint256"}]

_ORACLE_ABI = [{"name": "price", "type": "function", "stateMutability": "view", "inputs": [], "outputs": _UINT_OUT}]
_VAULT_ABI = [{"name": "chainId", "type": "function", "stateMutability": "view", "inputs": [], "outputs": _UINT_OUT}]
_HUB_ABI = [
    {
        "name": "getVaultChainIds",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256[]"}],
    },
    {
        "name": "getVault",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "_chainId", "type": "uint256"}],
        "outputs": _ADDRESS_OUT,
    },
]
_CONNECTOR_ABI = [
    {
        "name": "chainConfig",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "chainId", "type": "uint256"}],
        "outputs": [
            {"name": "peer", "type": "address"},
            {"name": "gasLimit", "type": "uint128"},
            {"name": "chainSelector", "type": "uint128"},
        ],
    }
]


@dataclass(frozen=True)
class FarmTypeContext:
    """A FarmRegistry farm-type number resolved to its FarmTypes name."""

    registry: str
    function_name: str
    farm_type: int
    farms: tuple[str, ...]

    @property
    def addresses(self) -> list[str]:
        return [self.registry, *self.farms]

    @property
    def labels(self) -> dict[str, str]:
        return {}

    @property
    def type_name(self) -> str:
        return FARM_TYPES.get(self.farm_type, ("UNKNOWN", ""))[0]

    @property
    def type_note(self) -> str:
        return FARM_TYPES.get(self.farm_type, ("", "not a FarmTypes constant"))[1]


@dataclass(frozen=True)
class OracleAssignmentContext:
    """The price an oracle reports for the asset it is being assigned to."""

    accounting: str
    asset: str
    asset_symbol: str
    asset_decimals: int
    oracle: str
    price_raw: int

    @property
    def addresses(self) -> list[str]:
        return [self.asset, self.oracle]

    @property
    def labels(self) -> dict[str, str]:
        return {}

    @property
    def unit_price(self) -> Decimal:
        """Reference-unit value of one whole asset token (1 = parity with USDC)."""
        return Decimal(self.price_raw) * (Decimal(10) ** self.asset_decimals) / _ORACLE_PRICE_SCALE


@dataclass(frozen=True)
class HubVaultContext:
    """How a PortalHub.setVault changes the per-chain vault registry."""

    hub: str
    vault: str
    vault_chain_id: int
    registered_chain_ids: tuple[int, ...]
    replaced_vault: str | None

    @property
    def addresses(self) -> list[str]:
        return [self.hub, self.vault] + ([self.replaced_vault] if self.replaced_vault else [])

    @property
    def labels(self) -> dict[str, str]:
        return {}


@dataclass(frozen=True)
class ConnectorRouteContext:
    """Whether a connector can send to a chain once the batch executes."""

    connector: str
    chain_id: int
    peer: str
    gas_limit: int
    chain_selector: int
    configured_in_batch: bool

    @property
    def addresses(self) -> list[str]:
        return [self.connector] + ([self.peer] if self.peer != ZERO_ADDRESS else [])

    @property
    def labels(self) -> dict[str, str]:
        return {}

    @property
    def is_configured(self) -> bool:
        return self.peer != ZERO_ADDRESS and self.gas_limit != 0


OutlandContext = FarmTypeContext | OracleAssignmentContext | HubVaultContext | ConnectorRouteContext


def _uint_param(call: DecodedCall, position: int) -> int | None:
    """The call's ``position``-th argument when it is an unsigned integer."""
    if len(call.params) <= position:
        return None
    type_str, value = call.params[position]
    return int(value) if type_str.startswith("uint") and isinstance(value, int) else None


def _address_param(call: DecodedCall, position: int) -> str | None:
    """The call's ``position``-th argument as a checksum address, when it is one."""
    if len(call.params) <= position:
        return None
    type_str, value = call.params[position]
    if type_str != "address" or not isinstance(value, str):
        return None
    try:
        return to_checksum_address(value)
    except ValueError:
        return None


def _farm_type_context(target: str, call: DecodedCall) -> FarmTypeContext | None:
    """Name the farm type in an ``addFarms`` / ``removeFarms`` call. No RPC."""
    if call.function_name not in {"addFarms", "removeFarms"}:
        return None
    farm_type = _uint_param(call, 0)
    if farm_type is None or len(call.params) < 2:
        return None
    type_str, farms = call.params[1]
    if type_str != "address[]" or not isinstance(farms, (list, tuple)):
        return None
    return FarmTypeContext(
        registry=target,
        function_name=call.function_name,
        farm_type=farm_type,
        farms=tuple(to_checksum_address(str(farm)) for farm in farms),
    )


def _oracle_context(chain_id: int, target: str, call: DecodedCall) -> OracleAssignmentContext | None:
    """Read the price a newly assigned oracle reports and scale it by the asset's decimals."""
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
    price = client.get_contract(oracle, _ORACLE_ABI).functions.price().call()
    return OracleAssignmentContext(
        accounting=target,
        asset=asset,
        asset_symbol=metadata.symbol,
        asset_decimals=metadata.decimals,
        oracle=oracle,
        price_raw=int(price),
    )


def _hub_vault_context(chain_id: int, target: str, call: DecodedCall) -> HubVaultContext | None:
    """Read which chains a PortalHub has vaults for, and what this setVault replaces."""
    if call.function_name != "setVault":
        return None
    vault = _address_param(call, 0)
    if vault is None or not exposes(chain_id, target, {"getVaultChainIds", "getVault", "setVault"}):
        return None
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    hub = client.get_contract(target, _HUB_ABI)
    with client.batch_requests() as batch:
        batch.add(client.get_contract(vault, _VAULT_ABI).functions.chainId())
        batch.add(hub.functions.getVaultChainIds())
        vault_chain_id, chain_ids = client.execute_batch(batch)
    registered = tuple(int(chain) for chain in chain_ids)
    replaced = None
    if int(vault_chain_id) in registered:
        replaced = to_checksum_address(hub.functions.getVault(int(vault_chain_id)).call())
    return HubVaultContext(
        hub=target,
        vault=vault,
        vault_chain_id=int(vault_chain_id),
        registered_chain_ids=registered,
        replaced_vault=replaced,
    )


def _connector_contexts(chain_id: int, target: str, calls: list[DecodedCall]) -> list[ConnectorRouteContext]:
    """Read each touched destination chain's send configuration on a connector."""
    chain_ids = list(
        dict.fromkeys(
            chain
            for call in calls
            if call.function_name in _CONNECTOR_CHAIN_CALLS
            for chain in [_uint_param(call, 0)]
            if chain is not None
        )
    )
    if not chain_ids or not exposes(chain_id, target, {"chainConfig", "portal"}):
        return []
    configured_in_batch = {_uint_param(call, 0) for call in calls if call.function_name == "setConfiguration"}
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    connector = client.get_contract(target, _CONNECTOR_ABI)
    with client.batch_requests() as batch:
        for destination in chain_ids:
            batch.add(connector.functions.chainConfig(destination))
        configs = client.execute_batch(batch)
    return [
        ConnectorRouteContext(
            connector=target,
            chain_id=destination,
            peer=to_checksum_address(str(peer)),
            gas_limit=int(gas_limit),
            chain_selector=int(selector),
            configured_in_batch=destination in configured_in_batch,
        )
        for destination, (peer, gas_limit, selector) in zip(chain_ids, configs)
    ]


def resolve_outland_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[OutlandContext]:
    """Resolve deterministic Outland context for the calls in one alert."""
    if protocol.lower() != PROTOCOL or chain_id != Chain.MAINNET.chain_id:
        return []

    calls_by_target: dict[str, list[DecodedCall]] = {}
    for target, call in targets_and_calls:
        try:
            calls_by_target.setdefault(to_checksum_address(target), []).append(call)
        except ValueError:
            continue

    contexts: list[OutlandContext] = []
    for target, calls in calls_by_target.items():
        for call in calls:
            try:
                resolved = (
                    _farm_type_context(target, call)
                    or _oracle_context(chain_id, target, call)
                    or _hub_vault_context(chain_id, target, call)
                )
            except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
                logger.info("Outland context failed for %s.%s: %s", target, call.function_name, error)
                continue
            if resolved is not None:
                contexts.append(resolved)
        try:
            contexts.extend(_connector_contexts(chain_id, target, calls))
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Outland connector context failed for %s: %s", target, error)
    return contexts


def _format_unit_price(context: OracleAssignmentContext) -> str:
    """Whole-token price in the reference unit, e.g. ``1`` or ``0.9985``."""
    return f"{context.unit_price.normalize():f}"


def _route_status(context: ConnectorRouteContext) -> str:
    """One sentence on whether the connector can send to the chain after this batch."""
    if context.is_configured:
        return f"configured — peer {context.peer}, gas limit {context.gas_limit:,}, selector {context.chain_selector}"
    if context.configured_in_batch:
        return "not configured on-chain yet; this batch calls setConfiguration for it"
    return (
        "NOT configured (peer, gas limit and selector all unset). sendTokens to this chain reverts "
        "(MissingPeer / NoGasLimit) until a separate setConfiguration(chainId, peer, selector, gasLimit) "
        "executes — that later call sets the peer, the destination-side address that receives the bridged "
        "funds, so the route is not live after this batch"
    )


def _hub_vault_line(context: HubVaultContext) -> str:
    registered = ", ".join(str(chain) for chain in context.registered_chain_ids) or "none"
    if context.replaced_vault:
        effect = f"REPLACES the existing chain-{context.vault_chain_id} vault {context.replaced_vault}"
    else:
        effect = f"adds chain {context.vault_chain_id}; no existing vault is replaced"
    return (
        f"PortalHub {context.hub} keys vaults by chain id. setVault({context.vault}) targets chain "
        f"{context.vault_chain_id} and {effect}. Chains registered before this call: {registered}."
    )


def format_outland_prompt(contexts: list[OutlandContext]) -> str:
    """Render verified Outland context for the LLM prompt."""
    lines: list[str] = []
    for context in contexts:
        if isinstance(context, FarmTypeContext):
            lines.append(
                f"FarmRegistry {context.registry}.{context.function_name}: farm type {context.farm_type} = "
                f"FarmTypes.{context.type_name} — {context.type_note}"
            )
        elif isinstance(context, OracleAssignmentContext):
            lines.append(
                f"Oracle {context.oracle} assigned to {context.asset} ({context.asset_symbol}, "
                f"{context.asset_decimals} decimals) reports price() = {context.price_raw}, i.e. one whole "
                f"{context.asset_symbol} is valued at {_format_unit_price(context)} reference units "
                "(IOracle scale: price * 10^decimals / 1e36; USDC is ~1)"
            )
        elif isinstance(context, HubVaultContext):
            lines.append(_hub_vault_line(context))
        else:
            lines.append(f"Connector {context.connector} route to chain {context.chain_id}: {_route_status(context)}.")
    return "\n".join(lines)


def format_outland_report(contexts: list[OutlandContext], chain_id: int, labels: dict[str, str]) -> str:
    """Render the deterministic Outland section for the gist report."""
    lines: list[str] = []
    for context in contexts:
        if isinstance(context, FarmTypeContext):
            lines.append(
                f"- **Farm type {context.farm_type}:** `FarmTypes.{context.type_name}` — {context.type_note} "
                f"({address_link(context.registry, chain_id, labels)} `{context.function_name}`)"
            )
        elif isinstance(context, OracleAssignmentContext):
            lines.append(
                f"- **Oracle price:** {address_link(context.oracle, chain_id, labels)} reports `{context.price_raw}` "
                f"→ one whole `{context.asset_symbol}` = `{_format_unit_price(context)}` reference units "
                "(USDC ≈ 1)"
            )
        elif isinstance(context, HubVaultContext):
            registered = ", ".join(f"`{chain}`" for chain in context.registered_chain_ids) or "none"
            effect = (
                f"replaces {address_link(context.replaced_vault, chain_id, labels)}"
                if context.replaced_vault
                else "new chain, no vault replaced"
            )
            lines.append(
                f"- **PortalHub vault for chain `{context.vault_chain_id}`:** {effect}; "
                f"chains registered before: {registered}"
            )
        else:
            status = (
                "configured"
                if context.is_configured
                else "set in this batch"
                if context.configured_in_batch
                else "**not configured** — sends revert until a later `setConfiguration` sets the peer"
            )
            lines.append(
                f"- **Connector route to chain `{context.chain_id}`** "
                f"({address_link(context.connector, chain_id, labels)}): {status}"
            )
    return "\n".join(lines)

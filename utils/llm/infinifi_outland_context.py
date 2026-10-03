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
- ``Connector.govReceive(chainId, message)`` queues an arbitrary inbound message
  on the PortalHub as if the bridge had delivered it, and
  ``PortalHub.processMessage`` executes it. The message is opaque bytes; an
  assets update in it mints or burns OutlandFarm shares on mainnet, i.e. books
  profit or loss. Reports could not resolve its numbers.

This adapter runs only for Infinifi on Ethereum, identifies contracts by the
getters their verified ABI exposes, and reads the surrounding state on-chain.
"""

from dataclasses import dataclass, replace
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

# Holds PROTOCOL_PARAMETERS, which govReceive requires. Its delay has changed between 1h and 24h, so read it live.
SHORT_TIMELOCK = "0x4B174afbeD7b98BA01F50E36109EEE5e6d327c32"

# FarmTypes library: the uint256 FarmRegistry.addFarms takes as its first argument.
FARM_TYPES: dict[int, tuple[str, str]] = {
    0: ("PROTOCOL", "not generating yield but capable of storing funds"),
    1: ("LIQUID", "instant principal withdrawals (e.g. Aave)"),
    2: (
        "MATURITY",
        "principal is available after the farm's maturity(): a fixed timestamp, or for a perpetual farm a rolling "
        "now + duration notice period",
    ),
}

# OutlandMsgCodec wire layout: type (32) | chainId (32) | nonce (32) | abi.encode(payload).
_MESSAGE_TYPES = {0: "MESSAGE (assets update)", 1: "TRANSFER", 2: "KEY_VALUE", 3: "TOKEN_BRIDGE"}
_HEADER_SIZE = 96

# IOracle.price() scale: a whole token's value in the reference unit is
# price * 10**decimals / 1e36 (USDC is quoted at ~1e30 for a 1:1 price).
_ORACLE_PRICE_SCALE = Decimal(10) ** 36

_CONNECTOR_CHAIN_CALLS = {"enableChainAsset", "disableChainAsset", "setCctpDomain", "setConfiguration"}

_ADDRESS_OUT = [{"name": "", "type": "address"}]
_UINT_OUT = [{"name": "", "type": "uint256"}]

_MATURITY_ABI = [
    {"name": name, "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": kind}]}
    for name, kind in (("duration", "uint256"), ("perpetual", "bool"))
]
_MIN_DELAY_ABI = [
    {"name": "getMinDelay", "type": "function", "stateMutability": "view", "inputs": [], "outputs": _UINT_OUT}
]
_PORTAL_ABI = [{"name": "portal", "type": "function", "stateMutability": "view", "inputs": [], "outputs": _ADDRESS_OUT}]
_VAULT_REPORT_ABI = [
    {
        "name": "portalAssetsReport",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"} for _ in range(4)],
    }
]

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
    # (farm, duration seconds, perpetual) for MATURITY farms whose terms could be read.
    maturity_terms: tuple[tuple[str, int, bool], ...] = ()

    def terms_lines(self) -> list[str]:
        """Each MATURITY farm's notice terms, since perpetual farms roll their maturity forward."""
        lines = []
        for farm, duration, perpetual in self.maturity_terms:
            if perpetual:
                lines.append(
                    f"{farm}: perpetual, rolling {duration / 86400:g}-day notice period (maturity() = now + {duration:,}s)"
                )
            else:
                lines.append(f"{farm}: fixed maturity at unix time {duration}")
        return lines

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
class RouteConfig:
    """A connector's send configuration for one destination chain."""

    peer: str
    gas_limit: int
    chain_selector: int

    @property
    def is_configured(self) -> bool:
        return self.peer != ZERO_ADDRESS and self.gas_limit != 0

    def describe(self) -> str:
        return f"peer {self.peer}, gas limit {self.gas_limit:,}, selector {self.chain_selector}"


@dataclass(frozen=True)
class ConnectorRouteContext:
    """Whether a connector can send to a chain once the batch executes."""

    connector: str
    chain_id: int
    # chainConfig read on-chain, before the batch executes.
    current: RouteConfig
    # The last setConfiguration this batch makes for the chain, if any; it
    # overrides ``current`` once the batch executes.
    proposed: RouteConfig | None = None

    @property
    def addresses(self) -> list[str]:
        peers = [config.peer for config in (self.current, self.proposed) if config and config.peer != ZERO_ADDRESS]
        return list(dict.fromkeys([self.connector, *peers]))

    @property
    def labels(self) -> dict[str, str]:
        return {}

    @property
    def after_batch(self) -> RouteConfig:
        """The configuration in force once the batch executes."""
        return self.proposed or self.current


@dataclass(frozen=True)
class OutlandMessageContext:
    """A cross-chain message injected (govReceive) or executed (processMessage) by governance."""

    target: str
    function_name: str
    source_chain_id: int
    message_type: int
    nonce: int
    payload: tuple[object, ...]
    # Vault for the source chain and its current portalAssetsReport (assets, iUSD, siUSD), when readable.
    vault: str = ""
    current_report: tuple[int, int, int] | None = None
    # Short Timelock getMinDelay() in seconds, read for govReceive.
    timelock_delay: int | None = None

    @property
    def addresses(self) -> list[str]:
        return [self.target] + ([self.vault] if self.vault else [])

    @property
    def labels(self) -> dict[str, str]:
        return {}

    @property
    def type_name(self) -> str:
        return _MESSAGE_TYPES.get(self.message_type, f"unknown type {self.message_type}")

    def describe(self) -> str:
        """What the call does with the message, and what the message carries."""
        if self.function_name == "govReceive":
            action = (
                f"govReceive on connector {self.target} queues this message on the PortalHub as if the bridge had "
                f"delivered it from chain {self.source_chain_id} — no bridge involved. It runs once processMessage "
                "executes it. govReceive needs PROTOCOL_PARAMETERS; its natspec says it should sit behind a 1-day "
                "timelock"
            )
            if self.timelock_delay is not None:
                action += (
                    f". The Short Timelock {SHORT_TIMELOCK}, which holds that role, has a current delay "
                    f"(getMinDelay) of {_duration(self.timelock_delay)}"
                )
        else:
            action = (
                f"processMessage on PortalHub {self.target} executes the queued chain-{self.source_chain_id} "
                "message with this nonce"
            )
        return f"{action}. Message: {self.type_name}, nonce {self.nonce}. {self._payload_text()}"

    def _payload_text(self) -> str:
        if self.message_type == 0 and len(self.payload) == 3:
            # ABI decoding supplies integers; malformed payloads are handled by the caller.
            assets, receipt, staked = (int(value) for value in self.payload)  # ty: ignore[invalid-argument-type]
            text = (
                f"Reports totalAssetsValue {_e18(assets)}, L2 iUSD supply {_e18(receipt)}, L2 siUSD supply "
                f"{_e18(staked)} (18 decimals). Executing it mints or burns OutlandFarm shares toward that value, "
                "booking the difference as profit or loss on mainnet"
            )
            if self.current_report is not None:
                current = self.current_report[0]
                delta = assets - current
                text += (
                    f"; the vault {self.vault} reports {_e18(current)} now, so the change is {'+' if delta >= 0 else '-'}"
                    f"{_e18(abs(delta))}"
                )
            return text + "."
        if self.message_type == 1 and len(self.payload) == 2:
            token, amount = self.payload
            return (
                f"Transfers {amount} raw units of {token} (the outpost-side token): the hub pulls the mapped token "
                "from the connector and deposits it into the chain's OutlandVault."
            )
        return "Payload not decoded."


def _duration(seconds: int) -> str:
    """Seconds as whole hours (or minutes), e.g. ``24h``."""
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m" if seconds % 60 == 0 else f"{seconds}s"


def _e18(raw: int) -> str:
    """An 18-decimal amount, grouped."""
    return f"{Decimal(raw) / Decimal(10**18):,.6f}"


def _decode_message(data: bytes) -> tuple[int, int, int, tuple[object, ...]] | None:
    """(type, chainId, nonce, payload) from an OutlandMsgCodec message, or None when malformed."""
    if len(data) < _HEADER_SIZE:
        return None
    message_type = data[0]
    chain = int.from_bytes(data[32:64], "big")
    nonce = int.from_bytes(data[64:96], "big")
    body = data[_HEADER_SIZE:]
    payload: tuple[object, ...] = ()
    if message_type == 0 and len(body) >= 96:
        payload = tuple(int.from_bytes(body[i : i + 32], "big") for i in (0, 32, 64))
    elif message_type == 1 and len(body) >= 64:
        payload = (to_checksum_address(body[12:32]), int.from_bytes(body[32:64], "big"))
    return message_type, chain, nonce, payload


OutlandContext = (
    FarmTypeContext | OracleAssignmentContext | HubVaultContext | ConnectorRouteContext | OutlandMessageContext
)


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


def _with_maturity_terms(chain_id: int, context: FarmTypeContext) -> FarmTypeContext:
    """Read duration/perpetual for added MATURITY farms; farms without the getters are skipped."""
    if context.farm_type != 2 or context.function_name != "addFarms":
        return context
    terms = []
    try:
        client = ChainManager.get_client(Chain.from_chain_id(chain_id))
        for farm in context.farms:
            contract = client.get_contract(farm, _MATURITY_ABI)
            terms.append((farm, int(contract.functions.duration().call()), bool(contract.functions.perpetual().call())))
    except Exception as error:  # noqa: BLE001 - terms are enrichment; the farm type still renders
        logger.info("Outland maturity terms unavailable: %s", error)
    return replace(context, maturity_terms=tuple(terms))


def _message_context(chain_id: int, target: str, call: DecodedCall) -> OutlandMessageContext | None:
    """Decode a govReceive / processMessage payload and read the source chain's vault report."""
    if call.function_name not in {"govReceive", "processMessage"} or len(call.params) < 2:
        return None
    source_chain = _uint_param(call, 0)
    type_str, data = call.params[1]
    if source_chain is None or type_str != "bytes":
        return None
    raw = bytes.fromhex(data[2:]) if isinstance(data, str) else bytes(data)
    decoded = _decode_message(raw)
    if decoded is None:
        return None
    message_type, _, nonce, payload = decoded
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    hub = target
    if call.function_name == "govReceive":
        if not exposes(chain_id, target, {"govReceive", "portal"}):
            return None
        hub = to_checksum_address(client.get_contract(target, _PORTAL_ABI).functions.portal().call())
    elif not exposes(chain_id, target, {"processMessage", "getVault"}):
        return None
    delay = None
    if call.function_name == "govReceive":
        try:
            delay = int(client.get_contract(SHORT_TIMELOCK, _MIN_DELAY_ABI).functions.getMinDelay().call())
        except Exception as error:  # noqa: BLE001 - the decoded message is still useful
            logger.info("Short Timelock delay unavailable: %s", error)
    vault, report = "", None
    try:
        vault = to_checksum_address(client.get_contract(hub, _HUB_ABI).functions.getVault(source_chain).call())
        values = client.get_contract(vault, _VAULT_REPORT_ABI).functions.portalAssetsReport().call()
        report = (int(values[0]), int(values[1]), int(values[2]))
    except Exception as error:  # noqa: BLE001 - the decoded message is still useful
        logger.info("Outland vault report unavailable for chain %s: %s", source_chain, error)
    return OutlandMessageContext(
        target=target,
        function_name=call.function_name,
        source_chain_id=source_chain,
        message_type=message_type,
        nonce=nonce,
        payload=payload,
        vault=vault,
        current_report=report,
        timelock_delay=delay,
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


def _proposed_route_configs(calls: list[DecodedCall]) -> dict[int, RouteConfig]:
    """Route configs the batch's ``setConfiguration(chainId, peer, selector, gasLimit)`` calls set.

    A later call for the same chain wins, as it would on execution.
    """
    configs: dict[int, RouteConfig] = {}
    for call in calls:
        if call.function_name != "setConfiguration":
            continue
        destination, peer = _uint_param(call, 0), _address_param(call, 1)
        selector, gas_limit = _uint_param(call, 2), _uint_param(call, 3)
        if destination is None or peer is None or selector is None or gas_limit is None:
            continue
        configs[destination] = RouteConfig(peer, gas_limit, selector)
    return configs


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
    proposed = _proposed_route_configs(calls)
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
            current=RouteConfig(to_checksum_address(str(peer)), int(gas_limit), int(selector)),
            proposed=proposed.get(destination),
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
                farm_type = _farm_type_context(target, call)
                resolved = (
                    (_with_maturity_terms(chain_id, farm_type) if farm_type else None)
                    or _oracle_context(chain_id, target, call)
                    or _hub_vault_context(chain_id, target, call)
                    or _message_context(chain_id, target, call)
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
    current, proposed = context.current, context.proposed
    before = current.describe() if current.is_configured else "not configured"
    if proposed is not None:
        if not proposed.is_configured:
            return (
                f"this batch CLEARS the route via setConfiguration ({proposed.describe()}); sendTokens to this "
                f"chain reverts afterwards. Before the batch: {before}"
            )
        return (
            f"this batch sets it via setConfiguration to {proposed.describe()} — the peer is the destination-side "
            f"address that receives the bridged funds. Before the batch: {before}"
        )
    if current.is_configured:
        return f"configured, unchanged by this batch — {current.describe()}"
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
            lines.extend(f"  {line}" for line in context.terms_lines())
        elif isinstance(context, OutlandMessageContext):
            lines.append(context.describe())
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
            lines.extend(f"  - {line}" for line in context.terms_lines())
        elif isinstance(context, OutlandMessageContext):
            lines.append(f"- **Cross-chain message (`{context.function_name}`):** {context.describe()}")
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
            if context.proposed is not None:
                change = "set" if context.proposed.is_configured else "**cleared**"
                status = f"{change} by this batch — peer {address_link(context.proposed.peer, chain_id, labels)}"
                status += f", gas limit `{context.proposed.gas_limit:,}`, selector `{context.proposed.chain_selector}`"
                if context.current.is_configured:
                    status += f" (was peer {address_link(context.current.peer, chain_id, labels)})"
            elif context.current.is_configured:
                status = f"configured, unchanged — peer {address_link(context.current.peer, chain_id, labels)}"
            else:
                status = "**not configured** — sends revert until a later `setConfiguration` sets the peer"
            lines.append(
                f"- **Connector route to chain `{context.chain_id}`** "
                f"({address_link(context.connector, chain_id, labels)}): {status}"
            )
    return "\n".join(lines)

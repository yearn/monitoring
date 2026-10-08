"""Resolve Infinifi farm-configuration context: swap pairs, enabled assets, Pendle PT discounts.

Three calls reached the LLM with only their natspec, and each was misread:

- ``SwapFarmV2.setPairConfig(tokenIn, tokenOut, cooldown, slippage)`` documents
  ``_slippage`` as "the maximum slippage tolerance (in WAD)", but the code
  enforces ``minAmountOut = convert(in, out, amountIn).mulWadDown(slippage)``:
  it is a MINIMUM-OUTPUT ratio. ``0.9995e18`` (at most 0.05% loss) was reported
  as a 99.95% loss allowance and rated HIGH. The cooldown is compared against
  ``block.timestamp`` (seconds) but was reported as "unit unconfirmed".
- ``MultiAssetFarmV2.enableAssets`` and ``setPairConfig`` revert unless
  ``Accounting`` can already price the asset (``InvalidOracle`` /
  ``InvalidToken``). When the oracle comes from a separately scheduled
  ``setOracle`` the simulation reverts, and the report could not say why.
- ``PendleV2FarmV3.setMaturityPTDiscount`` warns that it jumps reported
  ``assets()`` on a farm holding PTs; whether the farm holds any is one read.

This adapter runs only for Infinifi on Ethereum and identifies contracts by
the functions their verified ABI exposes.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from eth_utils import to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.llm.abi_exposure import exposes
from utils.llm.report import address_link
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager

logger = get_logger("utils.llm.infinifi_farm_context")

PROTOCOL = "infinifi"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
FARM_REGISTRY = "0xF5f2718708f471e43968271956CC01aaA8c46119"
WAD = 10**18


def _fn(name: str, outputs: list[str], inputs: list[str] | None = None) -> dict:
    return {
        "name": name,
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": kind} for kind in inputs or []],
        "outputs": [{"name": "", "type": kind} for kind in outputs],
    }


_FARM_ABI = [
    _fn("accounting", ["address"]),
    _fn("assetToken", ["address"]),
    _fn("assets", ["uint256"]),
    _fn("isAssetSupported", ["bool"], ["address"]),
    _fn("maxSlippage", ["uint256"]),
    _fn("_MAX_COOLDOWN", ["uint256"]),
    _fn("getSwapPairConfig", ["uint64", "uint64", "uint128"], ["address", "address"]),
]
_ACCOUNTING_ABI = [_fn("oracle", ["address"], ["address"])]
_PENDLE_FARM_ABI = [
    _fn("assetToken", ["address"]),
    _fn("assets", ["uint256"]),
    _fn("maturity", ["uint256"]),
    _fn("maturityPTDiscount", ["uint256"]),
    _fn("totalReceivedPTs", ["uint256"]),
    _fn("ptToAssetsAtMaturity", ["uint256"], ["uint256"]),
    _fn("PT", ["address"]),
]
_REGISTRY_ABI = [_fn("isFarm", ["bool"], ["address"])]


@dataclass(frozen=True)
class TokenSupport:
    """Whether a farm can hold/price a token at the point its call runs in the batch."""

    token: str
    symbol: str
    # "asset token" | "supported" | "enabled earlier in this batch" | "enable pending oracle" | "not supported"
    status: str
    oracle: str  # Accounting.oracle(token) before the batch, ZERO_ADDRESS when unset
    oracle_in_batch: bool = False  # an earlier call in this batch sets it

    @property
    def can_be_priced(self) -> bool:
        return self.status == "asset token" or self.oracle != ZERO_ADDRESS or self.oracle_in_batch

    def describe(self) -> str:
        if self.status == "asset token":
            return f"{self.symbol} is the farm's asset token"
        if self.status == "enable pending oracle":
            return (
                f"{self.symbol}: enabled by the earlier enableAssets in this batch once Accounting has its oracle "
                "(NOT set yet)"
            )
        oracle = (
            "set by an earlier call in this batch"
            if self.oracle_in_batch
            else ("set" if self.oracle != ZERO_ADDRESS else "NOT set")
        )
        return f"{self.symbol}: {self.status}; Accounting oracle {oracle}"


@dataclass(frozen=True)
class SwapPairContext:
    """A SwapFarmV2 pair configuration, read against the farm's limits and current pair."""

    farm: str
    token_in: TokenSupport
    token_out: TokenSupport
    cooldown: int
    ratio: int  # the call's _slippage: minimum output as a WAD fraction of the oracle value
    floor: int  # farm-wide maxSlippage the pair ratio must be >= to
    max_cooldown: int
    current: tuple[int, int, int]  # (lastSwap, cooldown, ratio) before the batch
    farm_assets: int | None = None
    asset_decimals: int = 6
    asset_symbol: str = ""

    @property
    def addresses(self) -> list[str]:
        return [self.farm, self.token_in.token, self.token_out.token]

    @property
    def labels(self) -> dict[str, str]:
        return {}

    def prompt_line(self) -> str:
        pair = f"{self.token_in.symbol}/{self.token_out.symbol}"
        parts = [
            f"SwapFarmV2 {self.farm} setPairConfig({pair}): _slippage {self.ratio} is a MINIMUM-OUTPUT ratio, "
            f"not a loss allowance — the code enforces minAmountOut = convert(tokenIn, tokenOut, amountIn) "
            f"× {_percent(self.ratio)}, so each swap of this pair (either direction; the key is "
            f"direction-independent) must return at least {_percent(self.ratio)} of the oracle-converted input, "
            f"i.e. at most {_percent(WAD - self.ratio)} loss versus Accounting prices. The natspec's "
            '"maximum slippage tolerance" wording describes this same bound',
            self._floor_text(),
            f"cooldown {self.cooldown:,} seconds ({_duration(self.cooldown)}) between swaps of this pair "
            f"(compared with block.timestamp; maximum {_duration(self.max_cooldown)})",
            f"current pair config: {self.current_text()}",
            f"token support (both must be supported or the call reverts InvalidToken): "
            f"{self.token_in.describe()}; {self.token_out.describe()}",
        ]
        if self.farm_assets is not None:
            parts.append(
                f"the farm holds assets() = {_amount(self.farm_assets, self.asset_decimals, self.asset_symbol)}"
            )
        return "; ".join(parts) + "."

    def _floor_text(self) -> str:
        """How the pair ratio compares with the farm-wide floor every pair must meet."""
        if self.ratio < self.floor:
            return f"BELOW the farm-wide floor maxSlippage {_percent(self.floor)} — the call reverts InvalidSlippage"
        relation = "stricter than" if self.ratio > self.floor else "equal to"
        return (
            f"farm-wide floor maxSlippage {_percent(self.floor)} (a pair ratio below it reverts InvalidSlippage); "
            f"this pair is {relation} the floor"
        )

    def current_text(self) -> str:
        """The pair's configuration before the batch."""
        _last, cooldown, ratio = self.current
        if cooldown == 0 and ratio == 0:
            return "unset"
        return f"cooldown {cooldown:,}s, minimum output {_percent(ratio)}"


@dataclass(frozen=True)
class AssetEnableContext:
    """A MultiAssetFarmV2.enableAssets call and the oracle each asset needs."""

    farm: str
    accounting: str
    assets: tuple[TokenSupport, ...] = field(default_factory=tuple)

    @property
    def addresses(self) -> list[str]:
        return [self.farm, self.accounting, *(asset.token for asset in self.assets)]

    @property
    def labels(self) -> dict[str, str]:
        return {}

    def prompt_line(self) -> str:
        lines = []
        for asset in self.assets:
            if asset.status == "supported":
                effect = "is already supported, so the call reverts (InvalidAsset)"
            elif asset.can_be_priced:
                source = "an earlier call in this batch sets it" if asset.oracle_in_batch else "it is set"
                effect = f"needs an Accounting oracle — {source}, so the asset is enabled"
            else:
                effect = (
                    f"needs Accounting {self.accounting} to price it, and oracle({asset.symbol}) is NOT set yet: "
                    f"until an Accounting.setOracle for {asset.symbol} executes — typically a separately scheduled "
                    "operation — this call reverts (InvalidOracle), and an atomic batch reverts with it. Once the "
                    "oracle is set the asset is enabled. This is an execution-order dependency: assess the call by "
                    "its effect once enabled, not as a failed or no-op transaction"
                )
            lines.append(
                f"enableAssets on {self.farm}: {asset.symbol} {asset.token} {effect}. Enabled assets are valued "
                "in assets() through Accounting.price"
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class PendleDiscountContext:
    """A PendleV2FarmV3 maturity-discount change, with the farm's PT position."""

    farm: str
    current: int
    proposed: int
    total_pts: int
    pt_symbol: str
    pt_decimals: int
    assets: int
    asset_symbol: str
    asset_decimals: int
    maturity: int
    registered: bool | None
    maturity_value: int = 0  # ptToAssetsAtMaturity(totalReceivedPTs), in the asset token

    @property
    def addresses(self) -> list[str]:
        return [self.farm]

    @property
    def labels(self) -> dict[str, str]:
        return {}

    @property
    def assets_change(self) -> int:
        """Immediate change in reported assets(): maturity value × (new − old) discount."""
        return self.maturity_value * (self.proposed - self.current) // WAD

    def prompt_line(self) -> str:
        date = datetime.fromtimestamp(self.maturity, timezone.utc).strftime("%d/%m/%Y")
        parts = [
            f"PendleV2FarmV3 {self.farm} ({self.pt_symbol}, matures {date}): maturityPTDiscount "
            f"{_percent(self.current)} → {_percent(self.proposed)} (haircut {_percent(WAD - self.current)} → "
            f"{_percent(WAD - self.proposed)}); assets() values PTs as ptToAssetsAtMaturity(totalReceivedPTs) × "
            "maturityPTDiscount − remaining yield",
            f"the farm holds {_amount(self.total_pts, self.pt_decimals, 'PT')} and reports assets() = "
            f"{_amount(self.assets, self.asset_decimals, self.asset_symbol)}",
        ]
        if self.total_pts == 0:
            parts.append(
                "with no PTs held, reported assets do not change now; the new discount applies to PTs bought later"
            )
        else:
            parts.append(
                "reported assets change immediately by "
                f"{_amount(self.assets_change, self.asset_decimals, self.asset_symbol)}"
            )
        if self.proposed > WAD:
            parts.append("the new factor is ABOVE 100%: PTs would be valued above their maturity value")
        if self.registered is not None:
            parts.append(
                "the farm is registered in FarmRegistry"
                if self.registered
                else "the farm is NOT yet registered in FarmRegistry (a separate addFarms registers it)"
            )
        return "; ".join(parts) + "."


FarmContext = SwapPairContext | AssetEnableContext | PendleDiscountContext


def _percent(wad: int) -> str:
    """A WAD fraction as a percentage, e.g. 999500000000000000 → ``99.95%``."""
    value = (Decimal(wad) * 100 / Decimal(WAD)).normalize()
    return f"{value:f}%"


def _duration(seconds: int) -> str:
    if seconds and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds}s"


def _amount(raw: int, decimals: int, symbol: str) -> str:
    value = Decimal(raw) / (Decimal(10) ** decimals)
    rendered = f"{value:,.2f}".rstrip("0").rstrip(".") if value else "0"
    return f"{rendered} {symbol}".strip()


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


def _uint_param(call: DecodedCall, position: int) -> int | None:
    if len(call.params) <= position:
        return None
    type_str, value = call.params[position]
    return int(value) if type_str.startswith("uint") and isinstance(value, int) else None


def _address_list(call: DecodedCall, position: int) -> list[str]:
    if len(call.params) <= position:
        return []
    type_str, value = call.params[position]
    if type_str != "address[]" or not isinstance(value, (list, tuple)):
        return []
    return [to_checksum_address(str(item)) for item in value]


def _symbol(chain_id: int, token: str) -> str:
    meta = fetch_erc20_metadata(chain_id, token)
    return meta.symbol if meta else token


@dataclass
class _BatchState:
    """What earlier calls in the alert have already done, in execution order."""

    oracles_set: set[str] = field(default_factory=set)  # assets given an Accounting oracle
    enabled: set[tuple[str, str]] = field(default_factory=set)  # (farm, asset) enabled
    # (farm, asset) an earlier enableAssets would enable once Accounting can price it
    pending_oracle: set[tuple[str, str]] = field(default_factory=set)
    registered: set[str] = field(default_factory=set)  # farms added to the FarmRegistry


class _Resolver:
    """Reads farm state once per farm and resolves calls in batch order."""

    def __init__(self, chain_id: int) -> None:
        self.chain_id = chain_id
        self.client = ChainManager.get_client(Chain.from_chain_id(chain_id))
        self.state = _BatchState()

    def _farm(self, farm: str) -> Any:
        return self.client.get_contract(farm, _FARM_ABI)

    def support(self, farm: str, token: str) -> TokenSupport:
        contract = self._farm(farm).functions
        accounting = to_checksum_address(contract.accounting().call())
        oracle = to_checksum_address(
            self.client.get_contract(accounting, _ACCOUNTING_ABI).functions.oracle(token).call()
        )
        if token.lower() == str(contract.assetToken().call()).lower():
            status = "asset token"
        elif bool(contract.isAssetSupported(token).call()):
            status = "supported"
        elif (farm.lower(), token.lower()) in self.state.enabled:
            status = "enabled earlier in this batch"
        elif (farm.lower(), token.lower()) in self.state.pending_oracle:
            status = "enable pending oracle"
        else:
            status = "not supported"
        return TokenSupport(
            token=token,
            symbol=_symbol(self.chain_id, token),
            status=status,
            oracle=oracle,
            oracle_in_batch=token.lower() in self.state.oracles_set,
        )

    def swap_pair(self, farm: str, call: DecodedCall) -> SwapPairContext | None:
        token_in, token_out = _address_param(call, 0), _address_param(call, 1)
        cooldown, ratio = _uint_param(call, 2), _uint_param(call, 3)
        if token_in is None or token_out is None or cooldown is None or ratio is None:
            return None
        contract = self._farm(farm).functions
        asset = to_checksum_address(contract.assetToken().call())
        meta = fetch_erc20_metadata(self.chain_id, asset)
        last, current_cooldown, current_ratio = contract.getSwapPairConfig(token_in, token_out).call()
        return SwapPairContext(
            farm=farm,
            token_in=self.support(farm, token_in),
            token_out=self.support(farm, token_out),
            cooldown=cooldown,
            ratio=ratio,
            floor=int(contract.maxSlippage().call()),
            max_cooldown=int(contract._MAX_COOLDOWN().call()),
            current=(int(last), int(current_cooldown), int(current_ratio)),
            farm_assets=int(contract.assets().call()),
            asset_decimals=meta.decimals if meta else 6,
            asset_symbol=meta.symbol if meta else "",
        )

    def enable_assets(self, farm: str, call: DecodedCall) -> AssetEnableContext | None:
        assets = _address_list(call, 0)
        if not assets:
            return None
        accounting = to_checksum_address(self._farm(farm).functions.accounting().call())
        supports = tuple(self.support(farm, asset) for asset in assets)
        for support in supports:
            if support.status != "not supported":
                continue
            key = (farm.lower(), support.token.lower())
            (self.state.enabled if support.can_be_priced else self.state.pending_oracle).add(key)
        return AssetEnableContext(farm=farm, accounting=accounting, assets=supports)

    def pendle_discount(self, farm: str, call: DecodedCall) -> PendleDiscountContext | None:
        proposed = _uint_param(call, 0)
        if proposed is None:
            return None
        contract = self.client.get_contract(farm, _PENDLE_FARM_ABI).functions
        total_pts = int(contract.totalReceivedPTs().call())
        asset = to_checksum_address(contract.assetToken().call())
        pt = to_checksum_address(contract.PT().call())
        asset_meta, pt_meta = fetch_erc20_metadata(self.chain_id, asset), fetch_erc20_metadata(self.chain_id, pt)
        registered: bool | None = True if farm.lower() in self.state.registered else None
        if registered is None:
            try:
                registered = bool(self.client.get_contract(FARM_REGISTRY, _REGISTRY_ABI).functions.isFarm(farm).call())
            except Exception:  # noqa: BLE001 - registration is enrichment
                registered = None
        return PendleDiscountContext(
            farm=farm,
            current=int(contract.maturityPTDiscount().call()),
            proposed=proposed,
            total_pts=total_pts,
            pt_symbol=pt_meta.symbol if pt_meta else pt,
            pt_decimals=pt_meta.decimals if pt_meta else 18,
            assets=int(contract.assets().call()),
            asset_symbol=asset_meta.symbol if asset_meta else "",
            asset_decimals=asset_meta.decimals if asset_meta else 6,
            maturity=int(contract.maturity().call()),
            registered=registered,
            maturity_value=int(contract.ptToAssetsAtMaturity(total_pts).call()) if total_pts else 0,
        )

    def resolve(self, target: str, call: DecodedCall) -> FarmContext | None:
        name = call.function_name
        if name == "addFarms":
            self.state.registered.update(farm.lower() for farm in _address_list(call, 1))
            return None
        if name == "setOracle":
            asset, oracle = _address_param(call, 0), _address_param(call, 1)
            if asset and oracle and oracle != ZERO_ADDRESS:
                self.state.oracles_set.add(asset.lower())
            return None
        if name == "setPairConfig" and exposes(self.chain_id, target, {"setPairConfig", "getSwapPairConfig"}):
            return self.swap_pair(target, call)
        if name == "enableAssets" and exposes(self.chain_id, target, {"enableAssets", "isAssetSupported"}):
            return self.enable_assets(target, call)
        if name == "setMaturityPTDiscount" and exposes(
            self.chain_id, target, {"setMaturityPTDiscount", "totalReceivedPTs"}
        ):
            return self.pendle_discount(target, call)
        return None


# Calls this adapter explains; addFarms and setOracle only update the batch state.
_EXPLAINED = {"setPairConfig", "enableAssets", "setMaturityPTDiscount"}


def resolve_infinifi_farm_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[FarmContext]:
    """Resolve farm-configuration context for an Infinifi alert on Ethereum, in batch order."""
    if protocol.lower() != PROTOCOL or chain_id != Chain.MAINNET.chain_id:
        return []
    if not any(call.function_name in _EXPLAINED for _, call in targets_and_calls):
        return []
    resolver = _Resolver(chain_id)
    contexts: list[FarmContext] = []
    for target, call in targets_and_calls:
        try:
            context = resolver.resolve(to_checksum_address(target), call)
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Infinifi farm context failed for %s.%s: %s", target, call.function_name, error)
            continue
        if context is not None:
            contexts.append(context)
    return contexts


def format_infinifi_farm_prompt(contexts: list[FarmContext]) -> str:
    """Render verified farm-configuration context for the LLM prompt."""
    return "\n".join(context.prompt_line() for context in contexts)


def format_infinifi_farm_report(contexts: list[FarmContext], chain_id: int, labels: dict[str, str]) -> str:
    """Render the deterministic farm-configuration section for the gist report."""
    lines = []
    for context in contexts:
        link = address_link(context.farm, chain_id, labels)
        if isinstance(context, SwapPairContext):
            lines.append(
                f"- **Swap pair {context.token_in.symbol}/{context.token_out.symbol}** on {link}: minimum output "
                f"`{_percent(context.ratio)}` of oracle value (max loss `{_percent(WAD - context.ratio)}`; farm "
                f"floor `{_percent(context.floor)}`), cooldown `{context.cooldown:,}s` ({_duration(context.cooldown)}); "
                f"before: {context.current_text()}"
            )
        elif isinstance(context, AssetEnableContext):
            for asset in context.assets:
                state = "oracle set" if asset.can_be_priced else "**no Accounting oracle — reverts until one is set**"
                lines.append(f"- **Enable {asset.symbol}** on {link}: {asset.status}; {state}")
        else:
            change = (
                "no immediate change (no PTs held)"
                if context.total_pts == 0
                else f"assets() changes by `{_amount(context.assets_change, context.asset_decimals, context.asset_symbol)}`"
            )
            lines.append(
                f"- **Maturity PT discount** on {link}: `{_percent(context.current)}` → `{_percent(context.proposed)}`; "
                f"holds `{_amount(context.total_pts, context.pt_decimals, 'PT')}`; {change}"
            )
    return "\n".join(lines)

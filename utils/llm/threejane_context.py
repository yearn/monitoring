"""Resolve 3Jane governance context for timelock calls.

3Jane routes its configuration and rewards operations through two
TimelockControllers, and both call shapes arrive at the LLM as opaque data:

- ``ProtocolConfig.setConfig(bytes32,uint256)`` identifies the parameter being
  changed by ``keccak256("<NAME>")`` only, so the decoded call shows a 32-byte
  hash with no indication of whether it is a pause flag or the max LTV.
- ``RewardsDistributor.setEpochEmissions`` / ``updateRoot`` allocate JANE and
  swap the Merkle root, but whether a claim mints new supply or transfers an
  existing balance lives in ``useMint`` — state the calldata never carries.

This adapter is deliberately narrow: it runs only for 3Jane on Ethereum,
identifies contracts from their verified ABI, reverses known hashed labels from
a checked-in name table, and reads the surrounding state on-chain.
"""

from dataclasses import dataclass
from functools import lru_cache

from eth_utils import keccak, to_checksum_address

from utils.abi import load_abi
from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.llm.report import address_link
from utils.logger import get_logger
from utils.source_context import fetch_abi_entries
from utils.web3_wrapper import ChainManager

logger = get_logger("utils.llm.threejane_context")

PROTOCOL = "3jane"

# Epochs of emission history rendered alongside the epoch being set. Enough to
# show whether a weekly allocation is in line with recent ones.
EMISSION_HISTORY_EPOCHS = 3

# Hashed labels 3Jane passes as bytes32 arguments. Names are the pre-image; the
# note explains what the value controls so the LLM does not have to guess from
# the name alone. Sourced from ProtocolConfigLib, IProtocolConfig's config
# structs, and the Jane / EmergencyController role declarations.
_HASHED_LABELS: dict[str, str] = {
    # --- ProtocolConfig: market control ---
    "IS_PAUSED": "protocol-wide pause flag for the credit market (non-zero pauses)",
    "MAX_ON_CREDIT": "share of supplied assets allowed to be lent on credit",
    "DEBT_CAP": "ceiling on total protocol debt",
    # --- ProtocolConfig: credit line (CreditLineConfig) ---
    "MAX_LTV": "maximum loan-to-value accepted when setting a credit line (WAD)",
    "MAX_VV": "maximum vv (verified value) accepted when setting a credit line",
    "MAX_CREDIT_LINE": "maximum size of a single borrower credit line",
    "MIN_CREDIT_LINE": "minimum size of a single borrower credit line",
    "MAX_DRP": "maximum borrower default-risk premium, per second in WAD",
    # --- ProtocolConfig: market timing (MarketConfig) ---
    "GRACE_PERIOD": "seconds after cycle end before a borrower counts as delinquent",
    "DELINQUENCY_PERIOD": "seconds of delinquency before a borrower defaults",
    "MIN_BORROW": "minimum outstanding loan balance, prevents dust positions",
    "IRP": "penalty rate charged to delinquent borrowers, per second in WAD",
    "CYCLE_DURATION": "length of a payment cycle in seconds",
    "MIN_LOAN_DURATION": "minimum loan duration in seconds",
    "LATE_REPAYMENT_THRESHOLD": "threshold at which a repayment counts as late",
    "DEFAULT_THRESHOLD": "threshold at which a borrower is treated as defaulted",
    # --- ProtocolConfig: interest rate model (IRMConfig) ---
    "CURVE_STEEPNESS": "AdaptiveCurveIRM curve steepness",
    "ADJUSTMENT_SPEED": "AdaptiveCurveIRM rate adjustment speed",
    "TARGET_UTILIZATION": "utilization the IRM steers towards (WAD)",
    "INITIAL_RATE_AT_TARGET": "IRM starting rate at target utilization",
    "MIN_RATE_AT_TARGET": "IRM lower bound on the rate at target utilization",
    "MAX_RATE_AT_TARGET": "IRM upper bound on the rate at target utilization",
    # --- ProtocolConfig: tranches ---
    "TRANCHE_RATIO": "junior/senior tranche ratio",
    "TRANCHE_SHARE_VARIANT": "tranche share variant selector",
    "MIN_SUSD3_BACKING_RATIO": "minimum sUSD3 backing ratio; 0 disables the ratio floor",
    "SUSD3_NOMINAL_BACKING_FLOOR": "absolute sUSD3 backing floor; sUSD3 redemptions block below it",
    # --- ProtocolConfig: timing and caps ---
    "SUSD3_LOCK_DURATION": "sUSD3 lock duration in seconds",
    "SUSD3_COOLDOWN_PERIOD": "sUSD3 cooldown period in seconds",
    "SUSD3_WITHDRAWAL_WINDOW": "seconds after cooldown during which sUSD3 can be withdrawn",
    "USD3_COMMITMENT_TIME": "USD3 deposit commitment period in seconds",
    "USD3_SUPPLY_CAP": "cap on USD3 supply in asset units",
    "FULL_MARKDOWN_DURATION": "seconds over which a defaulted loan is marked down to zero",
    # --- Roles (Jane token, EmergencyController, MorphoCredit) ---
    "OWNER_ROLE": "owner role: manages all other roles and contract parameters",
    "MINTER_ROLE": "minter role: can mint new JANE",
    "TRANSFER_ROLE": "transfer role: can move JANE while transfers are globally disabled",
    "EMERGENCY_AUTHORIZED_ROLE": "emergency role: pause, zero caps, revoke credit lines — bypasses the timelocks",
}

# keccak256(name) → (name, note). Derived so the table cannot drift from the hash.
_LABELS_BY_HASH: dict[str, tuple[str, str]] = {
    "0x" + keccak(text=name).hex(): (name, note) for name, note in _HASHED_LABELS.items()
}

ABI_DIR = "protocols/3jane/abi"


@lru_cache(maxsize=None)
def _abi(name: str) -> list[dict]:
    """Load a checked-in 3Jane ABI once per process.

    Lazily, not at import: this module sits in the explainer's import chain, and
    a missing or unreadable file should degrade one protocol's context rather
    than break every AI alert.
    """
    entries: list[dict] = load_abi(f"{ABI_DIR}/{name}.json")
    return entries


_MINTER_ROLE = keccak(text="MINTER_ROLE")

_DISTRIBUTOR_GETTERS = {"useMint", "merkleRoot", "jane", "maxClaimable", "totalClaimed", "epochEmissions"}


@dataclass(frozen=True)
class HashedLabelContext:
    """A bytes32 argument resolved back to the name it hashes."""

    target: str
    argument_hex: str
    name: str
    note: str
    # Set only for ProtocolConfig keys; a role hash has no value to read.
    is_config_key: bool = False
    current_value: int | None = None

    @property
    def addresses(self) -> list[str]:
        return [self.target]

    @property
    def labels(self) -> dict[str, str]:
        return {}


@dataclass(frozen=True)
class RewardsDistributorContext:
    """Distribution mode and reward accounting around a RewardsDistributor call."""

    distributor_address: str
    token_address: str
    token_symbol: str
    token_decimals: int
    use_mint: bool
    distributor_is_minter: bool
    token_transferable: bool
    token_total_supply_raw: int
    distributor_balance_raw: int
    merkle_root: str
    max_claimable_raw: int
    total_claimed_raw: int
    current_epoch: int
    epoch_emissions: tuple[tuple[int, int], ...]

    @property
    def addresses(self) -> list[str]:
        return [self.distributor_address, self.token_address]

    @property
    def labels(self) -> dict[str, str]:
        return {
            self.distributor_address: "RewardsDistributor",
            self.token_address: f"{self.token_symbol} token",
        }

    @property
    def outstanding_raw(self) -> int:
        """Allocated but not yet claimed — the distributor's remaining claim ceiling."""
        return max(self.max_claimable_raw - self.total_claimed_raw, 0)

    def amount(self, raw: int) -> str:
        """Render a raw token amount with this token's verified decimals.

        Truncated to whole tokens, matching the call flow's amount hints — an
        18-decimal tail on a multi-million reward allocation is noise the LLM
        then has to carry through its own arithmetic.
        """
        scale = 10**self.token_decimals
        whole = raw // scale
        if whole >= 1 or raw == 0:
            return f"{whole:,} {self.token_symbol}"
        tenths = (raw * 10) // scale
        return f"0.{tenths} {self.token_symbol}" if tenths else f"<0.1 {self.token_symbol}"


ThreeJaneContext = HashedLabelContext | RewardsDistributorContext


def _abi_function_names(entries: list[dict]) -> set[str]:
    """Function names present in a verified ABI."""
    return {str(entry.get("name")) for entry in entries if entry.get("type") == "function" and entry.get("name")}


def _exposes(chain_id: int, address: str, wanted: set[str]) -> bool:
    """Whether a contract exposes every wanted getter, following EIP-1967.

    3Jane's ProtocolConfig and MorphoCredit sit behind transparent proxies, so
    the address's own verified ABI lists the proxy's functions, not `config` or
    the distributor getters. Only pay for the implementation lookup when the
    proxy ABI comes up short.
    """
    names = _abi_function_names(fetch_abi_entries(chain_id, address) or [])
    if wanted.issubset(names):
        return True

    from utils.proxy import get_current_implementation

    implementation = get_current_implementation(address, chain_id)
    if not implementation or implementation.lower() == address.lower():
        return False
    return wanted.issubset(_abi_function_names(fetch_abi_entries(chain_id, implementation) or []))


def _as_hex32(value: object) -> str | None:
    """Normalize a decoded bytes32 argument to lowercase 0x-prefixed hex."""
    if isinstance(value, bytes):
        return "0x" + value.hex() if len(value) == 32 else None
    if isinstance(value, str) and value.startswith("0x") and len(value) == 66:
        return value.lower()
    return None


def _bytes32_arguments(call: DecodedCall) -> list[str]:
    """Every bytes32 argument of a call, normalized to hex."""
    hexes = []
    for type_str, value in call.params:
        if type_str != "bytes32":
            continue
        as_hex = _as_hex32(value)
        if as_hex:
            hexes.append(as_hex)
    return hexes


def _requested_epochs(calls: list[DecodedCall], current_epoch: int) -> list[int]:
    """Epochs named by setEpochEmissions calls, else the current epoch."""
    epochs = [
        int(value)
        for call in calls
        if call.function_name == "setEpochEmissions"
        for type_str, value in call.params[:1]
        if type_str.startswith("uint") and isinstance(value, int)
    ]
    return epochs or [current_epoch]


def _read_config_values(chain_id: int, target: str, keys: list[str]) -> dict[str, int]:
    """Read ProtocolConfig values for hashed keys, batched. Empty dict on failure."""
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    contract = client.get_contract(to_checksum_address(target), _abi("ProtocolConfig"))
    with client.batch_requests() as batch:
        for key in keys:
            batch.add(contract.functions.config(bytes.fromhex(key[2:])))
        values = client.execute_batch(batch)
    return {key: int(value) for key, value in zip(keys, values)}


def _resolve_hashed_labels(chain_id: int, target: str, calls: list[DecodedCall]) -> list[HashedLabelContext]:
    """Reverse known hashed labels passed as bytes32 arguments to one target."""
    hashes = [as_hex for call in calls for as_hex in _bytes32_arguments(call)]
    known = [as_hex for as_hex in dict.fromkeys(hashes) if as_hex in _LABELS_BY_HASH]
    if not known:
        return []

    is_config_key = _exposes(chain_id, target, {"config"})
    values: dict[str, int] = {}
    if is_config_key:
        try:
            values = _read_config_values(chain_id, target, known)
        except Exception as error:  # noqa: BLE001 - the name alone is still useful
            logger.info("3Jane config read failed for %s: %s", target, error)

    contexts = []
    for as_hex in known:
        name, note = _LABELS_BY_HASH[as_hex]
        contexts.append(
            HashedLabelContext(
                target=to_checksum_address(target),
                argument_hex=as_hex,
                name=name,
                note=note,
                is_config_key=is_config_key,
                current_value=values.get(as_hex),
            )
        )
    return contexts


def _read_distributor_context(chain_id: int, target: str, calls: list[DecodedCall]) -> RewardsDistributorContext | None:
    """Read distribution mode, claim accounting, and emission history for a distributor."""
    if not _exposes(chain_id, target, _DISTRIBUTOR_GETTERS):
        return None

    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    address = to_checksum_address(target)
    distributor = client.get_contract(address, _abi("RewardsDistributor"))
    with client.batch_requests() as batch:
        batch.add(distributor.functions.useMint())
        batch.add(distributor.functions.merkleRoot())
        batch.add(distributor.functions.jane())
        batch.add(distributor.functions.maxClaimable())
        batch.add(distributor.functions.totalClaimed())
        batch.add(distributor.functions.epoch())
        use_mint, merkle_root, token_address, max_claimable, total_claimed, current_epoch = client.execute_batch(batch)

    token_address = to_checksum_address(str(token_address))
    metadata = fetch_erc20_metadata(chain_id, token_address)
    if metadata is None:
        logger.info("3Jane distributor %s: ERC20 metadata unavailable for %s", address, token_address)
        return None

    epochs = sorted(
        {
            epoch - offset
            for epoch in _requested_epochs(calls, int(current_epoch))
            for offset in range(EMISSION_HISTORY_EPOCHS + 1)
            if epoch - offset >= 0
        }
    )
    token = client.get_contract(token_address, _abi("Jane"))
    with client.batch_requests() as batch:
        batch.add(token.functions.totalSupply())
        batch.add(token.functions.balanceOf(address))
        batch.add(token.functions.hasRole(_MINTER_ROLE, address))
        batch.add(token.functions.transferable())
        for epoch in epochs:
            batch.add(distributor.functions.epochEmissions(epoch))
        total_supply, balance, is_minter, transferable, *emissions = client.execute_batch(batch)

    return RewardsDistributorContext(
        distributor_address=address,
        token_address=token_address,
        token_symbol=metadata.symbol,
        token_decimals=metadata.decimals,
        use_mint=bool(use_mint),
        distributor_is_minter=bool(is_minter),
        token_transferable=bool(transferable),
        token_total_supply_raw=int(total_supply),
        distributor_balance_raw=int(balance),
        merkle_root="0x" + bytes(merkle_root).hex(),
        max_claimable_raw=int(max_claimable),
        total_claimed_raw=int(total_claimed),
        current_epoch=int(current_epoch),
        epoch_emissions=tuple((epoch, int(value)) for epoch, value in zip(epochs, emissions)),
    )


def resolve_threejane_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[ThreeJaneContext]:
    """Resolve deterministic 3Jane governance context for the calls in one alert."""
    if protocol.lower() != PROTOCOL or chain_id != Chain.MAINNET.chain_id:
        return []

    calls_by_target: dict[str, list[DecodedCall]] = {}
    for target, call in targets_and_calls:
        try:
            checksum = to_checksum_address(target)
        except ValueError:
            continue
        calls_by_target.setdefault(checksum, []).append(call)

    contexts: list[ThreeJaneContext] = []
    for target, calls in calls_by_target.items():
        try:
            distributor = _read_distributor_context(chain_id, target, calls)
            if distributor is not None:
                contexts.append(distributor)
            contexts.extend(_resolve_hashed_labels(chain_id, target, calls))
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("3Jane context resolution failed for %s: %s", target, error)
    return contexts


def _distribution_mode_line(context: RewardsDistributorContext) -> str:
    """State where claimed tokens come from, and whether that path is authorized."""
    if context.use_mint:
        authority = "holds" if context.distributor_is_minter else "does NOT hold"
        return (
            f"Distribution mode: useMint = true — claims MINT new {context.token_symbol}. "
            f"The distributor {authority} MINTER_ROLE on the token, so its own balance "
            f"({context.amount(context.distributor_balance_raw)}) is not the funding source."
        )
    return (
        f"Distribution mode: useMint = false — claims TRANSFER from the distributor's own balance "
        f"of {context.amount(context.distributor_balance_raw)}."
    )


def _emissions_line(context: RewardsDistributorContext) -> str:
    """Recent on-chain emissions, so a new allocation can be judged against them."""
    rendered = ", ".join(f"epoch {epoch}: {context.amount(value)}" for epoch, value in context.epoch_emissions)
    return f"Epoch emissions currently stored on-chain — {rendered}"


def format_threejane_prompt(contexts: list[ThreeJaneContext]) -> str:
    """Render verified 3Jane context for the LLM prompt."""
    sections: list[str] = []
    for context in contexts:
        if isinstance(context, RewardsDistributorContext):
            sections.append(
                "\n".join(
                    [
                        f"RewardsDistributor: {context.distributor_address}",
                        _distribution_mode_line(context),
                        f"Reward token: {context.token_address} ({context.token_symbol}, "
                        f"{context.token_decimals} decimals), current totalSupply "
                        f"{context.amount(context.token_total_supply_raw)}",
                        f"Token transfers globally enabled: {str(context.token_transferable).lower()} "
                        "(when false, only TRANSFER_ROLE holders can move the token)",
                        f"Claim accounting: maxClaimable {context.amount(context.max_claimable_raw)}, "
                        f"totalClaimed {context.amount(context.total_claimed_raw)}, "
                        f"outstanding claimable {context.amount(context.outstanding_raw)}",
                        f"Current merkleRoot: {context.merkle_root}",
                        f"Current epoch: {context.current_epoch}",
                        _emissions_line(context),
                    ]
                )
            )
        else:
            line = f'bytes32 {context.argument_hex} on {context.target} = keccak256("{context.name}") — {context.note}'
            if context.is_config_key:
                value = "not readable" if context.current_value is None else str(context.current_value)
                line += f"; value stored on-chain right now: {value}"
            sections.append(line)
    return "\n\n".join(sections)


def format_threejane_report(
    contexts: list[ThreeJaneContext],
    chain_id: int,
    labels: dict[str, str],
) -> str:
    """Render the deterministic 3Jane section for the gist report."""
    sections: list[str] = []
    for context in contexts:
        if isinstance(context, RewardsDistributorContext):
            lines = [
                f"- **Rewards distributor:** {address_link(context.distributor_address, chain_id, labels)}",
                f"- **Reward token:** `{context.token_symbol}` ({context.token_decimals} decimals) — "
                f"{address_link(context.token_address, chain_id)}",
                f"- **Distribution mode:** `useMint = {str(context.use_mint).lower()}` — "
                + (
                    f"claims mint new {context.token_symbol}"
                    + (
                        " (distributor holds `MINTER_ROLE`)"
                        if context.distributor_is_minter
                        else " (distributor does NOT hold `MINTER_ROLE`)"
                    )
                    if context.use_mint
                    else f"claims transfer from the distributor's balance of `{context.amount(context.distributor_balance_raw)}`"
                ),
                f"- **Token supply:** `{context.amount(context.token_total_supply_raw)}` total, "
                f"transfers globally enabled: `{str(context.token_transferable).lower()}`",
                f"- **Claim accounting:** `maxClaimable {context.amount(context.max_claimable_raw)}` | "
                f"`totalClaimed {context.amount(context.total_claimed_raw)}` | "
                f"`outstanding {context.amount(context.outstanding_raw)}`",
                f"- **Current `merkleRoot`:** `{context.merkle_root}`",
                f"- **Current epoch:** `{context.current_epoch}`",
                "- **Epoch emissions on-chain now:**",
            ]
            lines.extend(f"  - Epoch `{epoch}`: `{context.amount(value)}`" for epoch, value in context.epoch_emissions)
            sections.append("\n".join(lines))
        else:
            lines = [
                f'- **`{context.argument_hex}`** = `keccak256("{context.name}")` — {context.note}',
                f"  - Target: {address_link(context.target, chain_id, labels)}",
            ]
            if context.is_config_key:
                value = "not readable" if context.current_value is None else f"`{context.current_value:,}`"
                lines.append(f"  - Value stored on-chain right now: {value}")
            sections.append("\n".join(lines))
    return "\n\n".join(sections)

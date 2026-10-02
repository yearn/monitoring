"""Resolve 3Jane governance context for timelock calls.

3Jane routes its configuration and rewards operations through two
TimelockControllers, and both call shapes arrive at the LLM as opaque data:

- ``ProtocolConfig.setConfig(bytes32,uint256)`` identifies the parameter being
  changed by ``keccak256("<NAME>")`` only, so the decoded call shows a 32-byte
  hash with no indication of whether it is a pause flag or the max LTV.
- ``RewardsDistributor.setEpochEmissions`` / ``updateRoot`` allocate JANE and
  swap the Merkle root, but whether a claim mints new supply or transfers an
  existing balance lives in ``useMint`` — state the calldata never carries.

Account-level actions (LCC bounces, USD3 supply-cap exemptions) live in
``threejane_account_context``; this module dispatches to it.

This adapter is deliberately narrow: it runs only for 3Jane on Ethereum,
identifies contracts from their verified ABI, reverses known hashed labels from
a checked-in name table, and reads the surrounding state on-chain.
"""

from dataclasses import dataclass

from eth_utils import keccak, to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.llm.report import address_link
from utils.llm.threejane_abi import exposes, threejane_abi
from utils.llm.threejane_abi import reset_cache as reset_abi_cache
from utils.llm.threejane_account_context import (
    USD3_ADDRESS,
    AccountContext,
    TokenUnit,
    fetch_token_unit,
    format_account_prompt,
    format_account_report,
    resolve_account_contexts,
)
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager

logger = get_logger("utils.llm.threejane_context")

PROTOCOL = "3jane"

# Epochs of emission history rendered alongside the epoch being set. Enough to
# show whether a weekly allocation is in line with recent ones.
EMISSION_HISTORY_EPOCHS = 3

# Keys ProtocolConfigLib declares that no deployed 3Jane contract reads. Checked against the verified
# sources of MorphoCredit, CreditLine, USD3, sUSD3, MarkdownController, LCCVault, LCCVaultFactory,
# AdaptiveCurveIrm, Helper, InsuranceFund and NotificationVault (USD3l); re-check after an upgrade.
_UNUSED_KEY = "declared in ProtocolConfigLib but read by no deployed 3Jane contract: setting it has no on-chain effect"

# Hashed labels 3Jane passes as bytes32 arguments. Names are the pre-image; the
# note explains what the value controls so the LLM does not have to guess from
# the name alone. Each note is read from the contract that consumes the key
# (ProtocolConfig itself only stores values), plus the Jane / EmergencyController
# role declarations. Zero values are called out where a consumer treats 0 as a
# switch or a default rather than as the number zero.
_HASHED_LABELS: dict[str, str] = {
    # --- ProtocolConfig: market control ---
    "IS_PAUSED": (
        "MorphoCredit pause flag: non-zero blocks USD3 supplying to the credit market and new borrows; "
        "repayments and USD3 withdrawals from MorphoCredit continue"
    ),
    "MAX_ON_CREDIT": (
        "share of USD3's waUSDC that USD3 supplies to MorphoCredit, in bps (10000 = 100%); a deployment target, "
        "not a lending limit (DEBT_CAP bounds borrowing); 0 stops deployment"
    ),
    "DEBT_CAP": (
        "ceiling on MorphoCredit totalBorrowAssets, in waUSDC (the market's loan token), not USDC; 0 blocks all "
        "new borrowing; sUSD3's deposit cap is sized from max(actual debt, DEBT_CAP)"
    ),
    # --- ProtocolConfig: credit line (CreditLineConfig) ---
    "MAX_LTV": "maximum loan-to-value accepted when setting a credit line (WAD)",
    "MAX_VV": "maximum vv (verified value) accepted when setting a credit line",
    "MAX_CREDIT_LINE": "maximum size of a single borrower credit line",
    "MIN_CREDIT_LINE": "minimum size of a single borrower credit line",
    "MAX_DRP": (
        "maximum borrower default-risk premium, per second in WAD; CreditLine also hard-caps it at 31709791983 "
        "(100% a year)"
    ),
    # --- ProtocolConfig: market timing (MarketConfig) ---
    "GRACE_PERIOD": "seconds after cycle end before a borrower counts as delinquent",
    "DELINQUENCY_PERIOD": "seconds of delinquency before a borrower defaults",
    "MIN_BORROW": "minimum outstanding loan balance, prevents dust positions",
    "IRP": "penalty rate charged to delinquent borrowers, per second in WAD",
    "CYCLE_DURATION": (
        "minimum spacing between payment cycles in seconds; 0 freezes the market (MorphoCredit blocks borrows and "
        "repayments)"
    ),
    "MIN_LOAN_DURATION": _UNUSED_KEY,
    "LATE_REPAYMENT_THRESHOLD": _UNUSED_KEY,
    "DEFAULT_THRESHOLD": _UNUSED_KEY,
    # --- ProtocolConfig: interest rate model (IRMConfig) ---
    "CURVE_STEEPNESS": "AdaptiveCurveIRM curve steepness",
    "ADJUSTMENT_SPEED": "AdaptiveCurveIRM rate adjustment speed",
    "TARGET_UTILIZATION": "utilization the IRM steers towards (WAD)",
    "INITIAL_RATE_AT_TARGET": "IRM starting rate at target utilization",
    "MIN_RATE_AT_TARGET": "IRM lower bound on the rate at target utilization",
    "MAX_RATE_AT_TARGET": "IRM upper bound on the rate at target utilization",
    # --- ProtocolConfig: tranches ---
    "TRANCHE_RATIO": (
        "maximum sUSD3 size as a share of debt, in bps: sUSD3 deposits stop at max(actual debt, DEBT_CAP) x ratio; "
        "0 falls back to 1500 (15%)"
    ),
    "TRANCHE_SHARE_VARIANT": (
        "sUSD3's share of USD3 yield, in bps (at most 10000); written into USD3's performance fee only when a "
        "keeper calls USD3.syncTrancheShare, so it has no effect until then"
    ),
    "MIN_SUSD3_BACKING_RATIO": (
        "in bps: sUSD3 withdrawals stop below debt x ratio, and USD3 deploys to credit at most sUSD3 value / "
        "ratio; 0 disables both"
    ),
    "SUSD3_NOMINAL_BACKING_FLOOR": "absolute sUSD3 backing floor in USDC; sUSD3 withdrawals block below it",
    # --- ProtocolConfig: timing and caps ---
    "SUSD3_LOCK_DURATION": "sUSD3 lock duration in seconds",
    "SUSD3_COOLDOWN_PERIOD": "sUSD3 cooldown period in seconds",
    "SUSD3_WITHDRAWAL_WINDOW": "seconds after cooldown during which sUSD3 can be withdrawn; 0 falls back to 2 days",
    "USD3_COMMITMENT_TIME": _UNUSED_KEY,
    "USD3_SUPPLY_CAP": "cap on USD3 totalAssets, in the vault's asset",
    "USD3_REDEMPTION_FLOOR": (
        "hard USD3 redemption floor in USDC: withdrawals cannot take USD3 totalAssets below it (the higher of "
        "this and USD3_REDEMPTION_FLOOR_BPS applies)"
    ),
    "USD3_REDEMPTION_FLOOR_BPS": (
        "USD3 redemption floor as bps of totalAssets; applied to the then-current total, so repeated "
        "redemptions drain toward the nominal floor"
    ),
    "TEND_DRIFT_THRESHOLD": (
        "bps that USD3's credit deployment may drift from its target before keepers rebalance; 0 falls back to 10"
    ),
    "FULL_MARKDOWN_DURATION": "seconds over which a defaulted loan is marked down to zero",
    # --- Roles (Jane token, EmergencyController, MorphoCredit) ---
    "OWNER_ROLE": "owner role: manages all other roles and contract parameters",
    "MINTER_ROLE": "minter role: can mint new JANE",
    "TRANSFER_ROLE": (
        "transfer role: while JANE transfers are globally disabled, a transfer is allowed when the sender or the "
        "recipient holds it"
    ),
    "EMERGENCY_AUTHORIZED_ROLE": "emergency role: pause, zero caps, revoke credit lines — bypasses the timelocks",
}

# keccak256(name) → (name, note). Derived so the table cannot drift from the hash.
_LABELS_BY_HASH: dict[str, tuple[str, str]] = {
    "0x" + keccak(text=name).hex(): (name, note) for name, note in _HASHED_LABELS.items()
}

_MINTER_ROLE = keccak(text="MINTER_ROLE")


@dataclass(frozen=True)
class _UsageRead:
    """An ERC4626 whose totalAssets a config key caps, and how the cap is enforced."""

    vault_address: str
    label: str
    enforcement: str


# A cap only reads as slack or binding next to what it is capping. USD3's cap
# and its totalAssets are both denominated in the vault's asset (USDC), so the
# two are directly comparable and both render in that asset's units. DEBT_CAP is
# absent: MorphoCredit compares it to totalBorrowAssets in waUSDC (sUSD3 converts
# it with WAUSDC.convertToAssets), so it needs a waUSDC unit, not USDC. The
# enforcement note is read from USD3's availableDepositLimit — without it the
# model hedged that totalAssets might not be the measure the cap checks.
_USAGE_READS: dict[str, _UsageRead] = {
    "0x" + keccak(text="USD3_SUPPLY_CAP").hex(): _UsageRead(
        vault_address=USD3_ADDRESS,
        label="USD3 totalAssets",
        enforcement=(
            "USD3.availableDepositLimit compares this cap directly against USD3 totalAssets and allows new deposits "
            "only up to the difference. supplyCapExempt accounts skip that check. Exempt deposits and accrued "
            "interest can both carry totalAssets above the cap; which one did so here is not known."
        ),
    ),
}

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
    # What the key is capping, when the two are denominated the same way.
    usage_label: str = ""
    current_usage: int | None = None
    # Token the key's value is denominated in, when verified; None leaves values raw.
    unit: TokenUnit | None = None
    # Values this transaction's setConfig calls write for the key, in call order.
    proposed_values: tuple[int, ...] = ()
    usage_enforcement: str = ""

    def value_text(self, raw: int) -> str:
        """A config value in its verified unit with the raw integer beside it, else raw alone."""
        return f"{self.unit.amount(raw)} (raw {raw})" if self.unit else str(raw)

    def usage_lines(self) -> list[str]:
        """Where the capped quantity stands against the current cap and each proposed one."""
        if self.current_usage is None or self.unit is None:
            return []
        usage = self.current_usage
        lines = [f"{self.usage_label} right now: {self.value_text(usage)}"]
        for label, cap in [("current", self.current_value), *(("proposed", value) for value in self.proposed_values)]:
            if cap is None:
                continue
            if usage > cap:
                lines.append(f"Against the {label} cap: above it by {self.unit.amount(usage - cap)}")
            else:
                lines.append(f"Against the {label} cap: {self.unit.amount(cap - usage)} of headroom")
        return lines

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
    # Sole caller of the onlyOwner setters (setEpochEmissions, updateRoot, setUseMint).
    owner_address: str
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
    # (epoch, emissions) this transaction proposes, straight from the calldata.
    proposed_emissions: tuple[tuple[int, int], ...]

    @property
    def addresses(self) -> list[str]:
        return [self.distributor_address, self.owner_address, self.token_address]

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

    def cadence_lines(self) -> list[str]:
        """State how each proposed allocation compares to the epoch before it.

        Emissions run in a tight weekly band, so the same routine allocation has
        been called "substantial in absolute terms" one week and routine the
        next. Deriving the comparison here means the verdict rests on the
        series rather than on the model's own arithmetic.
        """
        stored = dict(self.epoch_emissions)
        lines = []
        for epoch, proposed in self.proposed_emissions:
            timing = (
                "the current epoch"
                if epoch == self.current_epoch
                else f"a past epoch (current is {self.current_epoch})"
                if epoch < self.current_epoch
                else f"a future epoch (current is {self.current_epoch})"
            )
            previous = stored.get(epoch - 1, 0)
            if previous > 0:
                delta = (proposed - previous) / previous * 100
                comparison = f"{delta:+.1f}% versus epoch {epoch - 1}'s {self.amount(previous)}"
            else:
                comparison = f"no allocation stored for epoch {epoch - 1} to compare against"
            lines.append(f"Epoch {epoch} is {timing}. Proposed {self.amount(proposed)} is {comparison}.")
        return lines

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


ThreeJaneContext = HashedLabelContext | RewardsDistributorContext | AccountContext


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


def _proposed_config_values(calls: list[DecodedCall]) -> dict[str, tuple[int, ...]]:
    """Values each setConfig(bytes32,uint256) call writes, keyed by the normalized key hash."""
    proposed: dict[str, list[int]] = {}
    for call in calls:
        if call.function_name != "setConfig" or len(call.params) != 2:
            continue
        (key_type, key), (value_type, value) = call.params
        as_hex = _as_hex32(key) if key_type == "bytes32" else None
        if as_hex and value_type.startswith("uint") and isinstance(value, int):
            proposed.setdefault(as_hex, []).append(int(value))
    return {key: tuple(values) for key, values in proposed.items()}


def _proposed_emissions(calls: list[DecodedCall]) -> list[tuple[int, int]]:
    """(epoch, emissions) pairs each setEpochEmissions call proposes."""
    proposed = []
    for call in calls:
        if call.function_name != "setEpochEmissions" or len(call.params) < 2:
            continue
        (epoch_type, epoch), (value_type, value) = call.params[0], call.params[1]
        if not (epoch_type.startswith("uint") and value_type.startswith("uint")):
            continue
        if isinstance(epoch, int) and isinstance(value, int):
            proposed.append((int(epoch), int(value)))
    return proposed


def _requested_epochs(calls: list[DecodedCall], current_epoch: int) -> list[int]:
    """Epochs named by setEpochEmissions calls, else the current epoch."""
    return [epoch for epoch, _ in _proposed_emissions(calls)] or [current_epoch]


@dataclass(frozen=True)
class _ConfigState:
    """Config values plus, for capping keys, the capped quantity and its unit."""

    values: dict[str, int]
    usage: dict[str, int]
    units: dict[str, TokenUnit]


def _read_config_state(chain_id: int, target: str, keys: list[str]) -> _ConfigState:
    """Read config values, plus what any capped quantity currently stands at.

    Both come back in one batched request: a cap read without its usage costs
    the same round trip and leaves the reader unable to tell a routine ceiling
    raise from one that unblocks a queue. The capped vault's asset() comes back
    in the same batch, so both numbers render in that asset's units rather than
    as 14-digit integers.
    """
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    contract = client.get_contract(to_checksum_address(target), threejane_abi("ProtocolConfig"))
    usage_keys = [key for key in keys if key in _USAGE_READS]
    with client.batch_requests() as batch:
        for key in keys:
            batch.add(contract.functions.config(bytes.fromhex(key[2:])))
        for key in usage_keys:
            vault = client.get_contract(
                to_checksum_address(_USAGE_READS[key].vault_address), threejane_abi("ERC4626Vault")
            )
            batch.add(vault.functions.totalAssets())
            batch.add(vault.functions.asset())
        results = client.execute_batch(batch)

    values = {key: int(value) for key, value in zip(keys, results[: len(keys)])}
    usage_results = results[len(keys) :]
    usage: dict[str, int] = {}
    units: dict[str, TokenUnit] = {}
    for index, key in enumerate(usage_keys):
        usage[key] = int(usage_results[2 * index])
        unit = fetch_token_unit(chain_id, str(usage_results[2 * index + 1]))
        if unit is not None:
            units[key] = unit
    return _ConfigState(values=values, usage=usage, units=units)


def _resolve_hashed_labels(chain_id: int, target: str, calls: list[DecodedCall]) -> list[HashedLabelContext]:
    """Reverse known hashed labels passed as bytes32 arguments to one target."""
    hashes = [as_hex for call in calls for as_hex in _bytes32_arguments(call)]
    known = [as_hex for as_hex in dict.fromkeys(hashes) if as_hex in _LABELS_BY_HASH]
    if not known:
        return []

    is_config_key = exposes(chain_id, target, {"config"})
    state = _ConfigState(values={}, usage={}, units={})
    if is_config_key:
        try:
            state = _read_config_state(chain_id, target, known)
        except Exception as error:  # noqa: BLE001 - the name alone is still useful
            logger.info("3Jane config read failed for %s: %s", target, error)

    proposed = _proposed_config_values(calls) if is_config_key else {}
    contexts = []
    for as_hex in known:
        name, note = _LABELS_BY_HASH[as_hex]
        usage_read = _USAGE_READS.get(as_hex)
        contexts.append(
            HashedLabelContext(
                target=to_checksum_address(target),
                argument_hex=as_hex,
                name=name,
                note=note,
                is_config_key=is_config_key,
                current_value=state.values.get(as_hex),
                usage_label=usage_read.label if usage_read else "",
                current_usage=state.usage.get(as_hex),
                unit=state.units.get(as_hex),
                proposed_values=proposed.get(as_hex, ()),
                usage_enforcement=usage_read.enforcement if usage_read else "",
            )
        )
    return contexts


def _read_distributor_context(chain_id: int, target: str, calls: list[DecodedCall]) -> RewardsDistributorContext | None:
    """Read distribution mode, claim accounting, and emission history for a distributor."""
    if not exposes(chain_id, target, _DISTRIBUTOR_GETTERS):
        return None

    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    address = to_checksum_address(target)
    distributor = client.get_contract(address, threejane_abi("RewardsDistributor"))
    with client.batch_requests() as batch:
        batch.add(distributor.functions.useMint())
        batch.add(distributor.functions.merkleRoot())
        batch.add(distributor.functions.jane())
        batch.add(distributor.functions.maxClaimable())
        batch.add(distributor.functions.totalClaimed())
        batch.add(distributor.functions.epoch())
        batch.add(distributor.functions.owner())
        (
            use_mint,
            merkle_root,
            token_address,
            max_claimable,
            total_claimed,
            current_epoch,
            owner,
        ) = client.execute_batch(batch)

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
    token = client.get_contract(token_address, threejane_abi("Jane"))
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
        owner_address=to_checksum_address(str(owner)),
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
        proposed_emissions=tuple(_proposed_emissions(calls)),
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
            contexts.extend(resolve_account_contexts(chain_id, target, calls))
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


def _ownership_line(context: RewardsDistributorContext) -> str:
    """State the ownership direction, which the model has otherwise inverted.

    Without it, a report described the executing timelock as "owned by the
    distributor" — the reverse of what `owner()` returns.
    """
    return (
        f"Ownership: RewardsDistributor.owner() = {context.owner_address}. The distributor is owned BY "
        "this address (not the other way round); only it can call the onlyOwner functions "
        "setEpochEmissions, updateRoot, setUseMint and sweep (which sends the distributor's whole balance of any "
        "token to the owner)."
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
                        _ownership_line(context),
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
                        *context.cadence_lines(),
                    ]
                )
            )
        elif isinstance(context, HashedLabelContext):
            sections.append(_hashed_label_prompt(context))
        else:
            sections.append(format_account_prompt(context))
    return "\n\n".join(sections)


def _hashed_label_prompt(context: HashedLabelContext) -> str:
    """One hashed label: its name, and for a config key its value before and after."""
    line = f'bytes32 {context.argument_hex} on {context.target} = keccak256("{context.name}") — {context.note}'
    if context.unit is not None:
        line += f" (denominated in {context.unit.symbol}, {context.unit.decimals} decimals, {context.unit.address})"
    if context.is_config_key:
        value = "not readable" if context.current_value is None else context.value_text(context.current_value)
        line += f"; value stored on-chain right now: {value}"
        for proposed in context.proposed_values:
            line += f"; this transaction sets it to: {context.value_text(proposed)}"
    if context.current_usage is not None and context.unit is None:
        line += (
            f"; {context.usage_label} right now: {context.current_usage} "
            "(same units as the key, so the two are directly comparable)"
        )
    lines = [line, *context.usage_lines()]
    if context.usage_enforcement:
        lines.append(context.usage_enforcement)
    return "\n".join(lines)


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
                f"- **Owner** (`owner()`, sole caller of the `onlyOwner` setters): "
                f"{address_link(context.owner_address, chain_id, labels)}",
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
            lines.extend(f"- **Proposed:** {line}" for line in context.cadence_lines())
            sections.append("\n".join(lines))
        elif isinstance(context, HashedLabelContext):
            sections.append(_hashed_label_report(context, chain_id, labels))
        else:
            sections.append(format_account_report(context, chain_id, labels))
    return "\n\n".join(sections)


def _report_value(context: HashedLabelContext, raw: int) -> str:
    """A config value for the gist: in its unit when verified, else the grouped raw integer."""
    return f"`{context.unit.amount(raw)}`" if context.unit else f"`{raw:,}`"


def _hashed_label_report(context: HashedLabelContext, chain_id: int, labels: dict[str, str]) -> str:
    """The gist bullet for one hashed label."""
    lines = [
        f'- **`{context.argument_hex}`** = `keccak256("{context.name}")` — {context.note}',
        f"  - Target: {address_link(context.target, chain_id, labels)}",
    ]
    if context.is_config_key:
        value = "not readable" if context.current_value is None else _report_value(context, context.current_value)
        lines.append(f"  - Value stored on-chain right now: {value}")
        lines.extend(
            f"  - Set by this transaction to: {_report_value(context, raw)}" for raw in context.proposed_values
        )
    if context.current_usage is not None:
        suffix = "" if context.unit else " (same units as the key)"
        lines.append(f"  - {context.usage_label} right now: {_report_value(context, context.current_usage)}{suffix}")
        lines.extend(f"  - {line}" for line in context.usage_lines()[1:])
    if context.usage_enforcement:
        lines.append(f"  - {context.usage_enforcement}")
    return "\n".join(lines)


def reset_cache() -> None:
    """Reset process caches for tests or long-running workers."""
    reset_abi_cache()

"""Resolve 3Jane account-level governance actions for timelock calls.

Two 3Jane calls act on a single account, and both reach the LLM with the one
fact that matters missing:

- ``LCCVault.bounceCommitment(user, commitment)`` passes a bare integer. It is
  denominated in the facility's *funding* asset, while the margin it returns
  is in a different *margin* asset, and whether it is a full or partial bounce
  depends on the user's account. A raw 499999999986 read at 1e18 looks like
  dust; it is a ~$500k commitment.
- ``USD3.setSupplyCapExempt(account, bool)`` flips a flag whose reach — cap
  headroom, first-deposit minimum, the waUSDC-paused block — is only visible in
  USD3's deposit-limit code, not in the setter's natspec.

Each context reads the account's state on-chain and states the effect in the
right units, so the model reports it rather than guessing.
"""

from dataclasses import dataclass

from eth_utils import keccak, to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.formatting import format_decimal_amount, normalize_token_amount
from utils.llm.report import address_link
from utils.llm.threejane_abi import exposes, threejane_abi
from utils.logger import get_logger
from utils.web3_wrapper import ChainManager

logger = get_logger("utils.llm.threejane_account_context")

USD3_ADDRESS = "0x056B269Eb1f75477a8666ae8C7fE01b64dD55eCc"
PROTOCOL_CONFIG_ADDRESS = "0x6b276A2A7dd8b629adBA8A06AD6573d01C84f34E"
USD3_SUPPLY_CAP_KEY = keccak(text="USD3_SUPPLY_CAP")
# USD3 treats a max-uint cap as "no cap" and skips the headroom check entirely.
UNLIMITED = 2**256 - 1

_LCC_GETTERS = {"getAccount", "assetConfig", "riskConfig", "totals"}


@dataclass(frozen=True)
class TokenUnit:
    """An ERC20 an on-chain amount is denominated in, with verified decimals."""

    address: str
    symbol: str
    decimals: int

    def amount(self, raw: int) -> str:
        """Render a raw amount exactly, e.g. ``499,999.999986 USDC``."""
        return f"{format_decimal_amount(normalize_token_amount(raw, self.decimals))} {self.symbol}"


def fetch_token_unit(chain_id: int, address: str) -> TokenUnit | None:
    """Resolve an address to a TokenUnit, or None when it is not an ERC20."""
    checksum = to_checksum_address(address)
    metadata = fetch_erc20_metadata(chain_id, checksum)
    if metadata is None:
        return None
    return TokenUnit(address=checksum, symbol=metadata.symbol, decimals=metadata.decimals)


@dataclass(frozen=True)
class LCCBounceContext:
    """A user's LCCVault account around a ``bounceCommitment`` call."""

    vault_address: str
    user_address: str
    commitment_raw: int
    funding: TokenUnit
    margin: TokenUnit
    active_commitment_raw: int
    active_margin_raw: int
    pending_commitment_raw: int
    pending_margin_raw: int
    exit_in_progress: bool
    min_deposit_margin_raw: int
    vault_active_commitment_raw: int

    @property
    def addresses(self) -> list[str]:
        return [self.vault_address, self.user_address, self.funding.address, self.margin.address]

    @property
    def labels(self) -> dict[str, str]:
        return {
            self.vault_address: "LCCVault",
            self.funding.address: f"{self.funding.symbol} (LCC funding asset)",
            self.margin.address: f"{self.margin.symbol} (LCC margin asset)",
        }

    @property
    def is_full(self) -> bool:
        return self.commitment_raw == self.active_commitment_raw

    @property
    def margin_returned_raw(self) -> int:
        """Pro-rata margin the bounce returns: activeMargin × commitment / activeCommitment, rounded down."""
        if self.active_commitment_raw == 0:
            return 0
        return self.active_margin_raw * self.commitment_raw // self.active_commitment_raw

    def blocker(self) -> str:
        """Why the call would revert against the account's current state, or "" when it would not.

        Mirrors the checks at the top of ``LCCVault.bounceCommitment``.
        """
        if self.exit_in_progress:
            return "the user's exit is in progress (ExitInProgress)"
        if self.pending_margin_raw or self.pending_commitment_raw:
            return "the user has a pending deposit that has not activated yet (PendingDepositExists)"
        if self.commitment_raw == 0 or self.commitment_raw > self.active_commitment_raw:
            return (
                f"the commitment exceeds the user's active commitment of "
                f"{self.funding.amount(self.active_commitment_raw)} (InvalidAmount)"
            )
        remaining_margin = self.active_margin_raw - self.margin_returned_raw
        if not self.is_full and remaining_margin < self.min_deposit_margin_raw:
            return (
                f"a partial bounce would leave {self.margin.amount(remaining_margin)} of margin, below the "
                f"vault's minDepositAssets of {self.margin.amount(self.min_deposit_margin_raw)} (InvalidAmount)"
            )
        return ""

    def effect_line(self) -> str:
        """The bounce's effect on the account, stated against current state."""
        blocker = self.blocker()
        if blocker:
            return f"Against current state the call would REVERT: {blocker}."
        returned = self.margin.amount(self.margin_returned_raw)
        if self.is_full:
            return (
                f"FULL bounce: removes 100% of the user's active commitment and returns all {returned} of "
                "margin to the user, leaving the account with no exposure in this vault."
            )
        share = self.commitment_raw / self.active_commitment_raw * 100
        return (
            f"PARTIAL bounce: removes {share:.1f}% of the user's active commitment and returns {returned} of "
            f"margin, leaving {self.funding.amount(self.active_commitment_raw - self.commitment_raw)} committed "
            f"against {self.margin.amount(self.active_margin_raw - self.margin_returned_raw)} of margin."
        )

    def vault_share_line(self) -> str:
        """How much of the vault's callable commitment this bounce withdraws."""
        total = self.vault_active_commitment_raw
        if total == 0 or self.blocker():
            return ""
        share = self.commitment_raw / total * 100
        return (
            f"Vault-wide active commitment: {self.funding.amount(total)}; this bounce withdraws {share:.1f}% of it, "
            f"leaving {self.funding.amount(total - self.commitment_raw)}."
        )


@dataclass(frozen=True)
class SupplyCapExemptContext:
    """An account's USD3 deposit flags around a ``setSupplyCapExempt`` call."""

    usd3_address: str
    account_address: str
    proposed_exempt: bool
    current_exempt: bool
    ring_fence_conduit: bool
    account_is_contract: bool
    asset: TokenUnit
    min_deposit_raw: int
    supply_cap_raw: int
    total_assets_raw: int

    @property
    def addresses(self) -> list[str]:
        return [self.usd3_address, self.account_address]

    @property
    def labels(self) -> dict[str, str]:
        return {self.usd3_address: "USD3"}

    def flag_line(self) -> str:
        """Before → after for the flag, flagging a no-op."""
        before, after = str(self.current_exempt).lower(), str(self.proposed_exempt).lower()
        if self.current_exempt == self.proposed_exempt:
            return f"supplyCapExempt is already {after}; the call changes nothing."
        return f"supplyCapExempt: {before} → {after}."

    def semantics_line(self) -> str:
        """What the exemption bypasses and what still applies.

        Read from USD3's ``availableDepositLimit`` and ``_preDepositHook``
        (implementation 0xd1f1c3f485063712873285bf4ef25ab068f13893). Re-check on
        a USD3 upgrade.
        """
        return (
            "What the flag does (USD3 availableDepositLimit / _preDepositHook): an exempt receiver skips the "
            f"supply-cap headroom check, the first-deposit minimum of {self.asset.amount(self.min_deposit_raw)}, "
            "and the deposit block that applies while waUSDC is paused, and may only deposit for itself "
            "(msg.sender == receiver). Still enforced for exempt accounts: accounts with outstanding borrow shares "
            "cannot deposit, and a supply cap of 0 blocks every deposit."
        )

    def pairing_line(self) -> str:
        """The natspec pairs the flag with ringFenceConduit; say when that pairing is absent."""
        kind = "a contract" if self.account_is_contract else "an EOA (no contract code)"
        pairing = (
            f"USD3 natspec pairs this flag with ringFenceConduit, and LCC deployment grants both atomically. "
            f"The account is {kind}; ringFenceConduit = {str(self.ring_fence_conduit).lower()}"
        )
        if self.proposed_exempt and not self.ring_fence_conduit:
            return pairing + ", so its exempt deposits receive no ring-fence credit."
        return pairing + "."

    def supply_line(self) -> str:
        """Where USD3 supply stands against the cap the exemption bypasses."""
        cap, assets = self.supply_cap_raw, self.total_assets_raw
        if cap == UNLIMITED:
            return f"USD3 now: totalAssets {self.asset.amount(assets)}; USD3_SUPPLY_CAP is unlimited (max uint256)."
        standing = (
            f"above the cap by {self.asset.amount(assets - cap)}"
            if assets > cap
            else f"{self.asset.amount(cap - assets)} of headroom"
        )
        return (
            f"USD3 now: totalAssets {self.asset.amount(assets)} against USD3_SUPPLY_CAP "
            f"{self.asset.amount(cap)} ({standing})."
        )


AccountContext = LCCBounceContext | SupplyCapExemptContext


def _address_uint_args(call: DecodedCall) -> tuple[str, int] | None:
    """The (address, uint256) arguments of a bounceCommitment call, or None."""
    if len(call.params) != 2:
        return None
    (address_type, user), (amount_type, amount) = call.params
    if address_type != "address" or not amount_type.startswith("uint") or not isinstance(amount, int):
        return None
    return to_checksum_address(str(user)), int(amount)


def _address_bool_args(call: DecodedCall) -> tuple[str, bool] | None:
    """The (address, bool) arguments of a setSupplyCapExempt call, or None."""
    if len(call.params) != 2:
        return None
    (address_type, account), (bool_type, flag) = call.params
    if address_type != "address" or bool_type != "bool":
        return None
    return to_checksum_address(str(account)), bool(flag)


def _read_lcc_bounces(chain_id: int, target: str, calls: list[DecodedCall]) -> list[LCCBounceContext]:
    """Read each bounced user's account, plus the vault's units and limits, in one batch."""
    bounces = [
        args for call in calls if call.function_name == "bounceCommitment" and (args := _address_uint_args(call))
    ]
    if not bounces or not exposes(chain_id, target, _LCC_GETTERS):
        return []

    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    vault_address = to_checksum_address(target)
    vault = client.get_contract(vault_address, threejane_abi("LCCVault"))
    with client.batch_requests() as batch:
        batch.add(vault.functions.assetConfig())
        batch.add(vault.functions.riskConfig())
        batch.add(vault.functions.totals())
        for user, _ in bounces:
            batch.add(vault.functions.getAccount(user))
        asset_config, risk_config, totals, *accounts = client.execute_batch(batch)

    # ILCCVault.AssetConfig: (marginAsset, fundingAsset, usd3, notificationVault, marginOracle, treasury)
    margin = fetch_token_unit(chain_id, str(asset_config[0]))
    funding = fetch_token_unit(chain_id, str(asset_config[1]))
    if margin is None or funding is None:
        logger.info("3Jane LCCVault %s: asset metadata unavailable", vault_address)
        return []

    contexts = []
    for (user, commitment), account in zip(bounces, accounts):
        # ILCCVault.Account: activeMargin, activeCommitment, pendingMargin, pendingCommitment, ...,
        # exitRequested (9), exitMaturityEpoch (10), exitClaimed (11)
        contexts.append(
            LCCBounceContext(
                vault_address=vault_address,
                user_address=user,
                commitment_raw=commitment,
                funding=funding,
                margin=margin,
                active_margin_raw=int(account[0]),
                active_commitment_raw=int(account[1]),
                pending_margin_raw=int(account[2]),
                pending_commitment_raw=int(account[3]),
                exit_in_progress=bool(account[9]) and not bool(account[11]),
                # ILCCVault.RiskConfig: (..., minDepositAssets (3), ...)
                min_deposit_margin_raw=int(risk_config[3]),
                # ILCCVault.Totals: (activeMargin, activeCommitment, pendingMargin, pendingCommitment)
                vault_active_commitment_raw=int(totals[1]),
            )
        )
    return contexts


def _read_supply_cap_exemptions(chain_id: int, target: str, calls: list[DecodedCall]) -> list[SupplyCapExemptContext]:
    """Read each account's USD3 flags and code, plus USD3's cap and supply, in one batch."""
    if target.lower() != USD3_ADDRESS.lower():
        return []
    changes = [
        args for call in calls if call.function_name == "setSupplyCapExempt" and (args := _address_bool_args(call))
    ]
    if not changes:
        return []

    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    usd3_address = to_checksum_address(USD3_ADDRESS)
    usd3 = client.get_contract(usd3_address, threejane_abi("USD3"))
    vault = client.get_contract(usd3_address, threejane_abi("ERC4626Vault"))
    config = client.get_contract(to_checksum_address(PROTOCOL_CONFIG_ADDRESS), threejane_abi("ProtocolConfig"))
    with client.batch_requests() as batch:
        batch.add(vault.functions.asset())
        batch.add(vault.functions.totalAssets())
        batch.add(usd3.functions.minDeposit())
        batch.add(config.functions.config(USD3_SUPPLY_CAP_KEY))
        for account, _ in changes:
            batch.add(usd3.functions.supplyCapExempt(account))
            batch.add(usd3.functions.ringFenceConduit(account))
        asset_address, total_assets, min_deposit, supply_cap, *flags = client.execute_batch(batch)

    asset = fetch_token_unit(chain_id, str(asset_address))
    if asset is None:
        logger.info("3Jane USD3 %s: asset metadata unavailable", usd3_address)
        return []

    contexts = []
    for index, (account, proposed) in enumerate(changes):
        contexts.append(
            SupplyCapExemptContext(
                usd3_address=usd3_address,
                account_address=account,
                proposed_exempt=proposed,
                current_exempt=bool(flags[2 * index]),
                ring_fence_conduit=bool(flags[2 * index + 1]),
                account_is_contract=len(client.eth.get_code(account)) > 0,
                asset=asset,
                min_deposit_raw=int(min_deposit),
                supply_cap_raw=int(supply_cap),
                total_assets_raw=int(total_assets),
            )
        )
    return contexts


def resolve_account_contexts(chain_id: int, target: str, calls: list[DecodedCall]) -> list[AccountContext]:
    """Resolve LCC bounces and USD3 exemptions among one target's calls."""
    contexts: list[AccountContext] = []
    contexts.extend(_read_lcc_bounces(chain_id, target, calls))
    contexts.extend(_read_supply_cap_exemptions(chain_id, target, calls))
    return contexts


def format_account_prompt(context: AccountContext) -> str:
    """Render one account context for the LLM prompt."""
    if isinstance(context, LCCBounceContext):
        lines = [
            f"LCCVault.bounceCommitment on {context.vault_address} for user {context.user_address}",
            f"Units verified from the vault's assetConfig(): `commitment` is in the funding asset "
            f"{context.funding.symbol} ({context.funding.decimals} decimals, {context.funding.address}); "
            f"margin is in {context.margin.symbol} ({context.margin.decimals} decimals, {context.margin.address}).",
            f"Commitment to remove: {context.funding.amount(context.commitment_raw)} (raw {context.commitment_raw}).",
            f"User's account right now: activeCommitment {context.funding.amount(context.active_commitment_raw)}, "
            f"activeMargin {context.margin.amount(context.active_margin_raw)}, "
            f"pending deposit {'yes' if context.pending_margin_raw or context.pending_commitment_raw else 'none'}, "
            f"exit in progress {'yes' if context.exit_in_progress else 'no'}.",
            context.effect_line(),
            context.vault_share_line(),
            "State is read at scheduling time. The call executes after the timelock delay and first replays the "
            "user's lazy defaults, so the returned margin can differ at execution.",
        ]
        return "\n".join(line for line in lines if line)

    return "\n".join(
        [
            f"USD3.setSupplyCapExempt on {context.usd3_address} for account {context.account_address}: "
            f"{context.flag_line()}",
            context.semantics_line(),
            context.pairing_line(),
            context.supply_line(),
        ]
    )


def format_account_report(context: AccountContext, chain_id: int, labels: dict[str, str]) -> str:
    """Render one account context for the gist report."""
    if isinstance(context, LCCBounceContext):
        lines = [
            f"- **LCC bounce:** {address_link(context.vault_address, chain_id, labels)} → user "
            f"{address_link(context.user_address, chain_id, labels)}",
            f"  - Commitment removed: `{context.funding.amount(context.commitment_raw)}` "
            f"(funding asset {address_link(context.funding.address, chain_id)})",
            f"  - Account now: activeCommitment `{context.funding.amount(context.active_commitment_raw)}`, "
            f"activeMargin `{context.margin.amount(context.active_margin_raw)}` "
            f"(margin asset {address_link(context.margin.address, chain_id)})",
            f"  - Effect: {context.effect_line()}",
        ]
        if share := context.vault_share_line():
            lines.append(f"  - {share}")
        return "\n".join(lines)

    return "\n".join(
        [
            f"- **USD3 supply-cap exemption:** {address_link(context.account_address, chain_id, labels)} "
            f"on {address_link(context.usd3_address, chain_id, labels)}",
            f"  - {context.flag_line()}",
            f"  - {context.semantics_line()}",
            f"  - {context.pairing_line()}",
            f"  - {context.supply_line()}",
        ]
    )

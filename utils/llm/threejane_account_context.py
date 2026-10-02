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
  USD3's deposit-limit code, not in the setter's natspec. Batches of these are
  rendered as one context: shared semantics once, then one line per account,
  with each account's last flag change from USD3's ``SupplyCapExemptUpdated``
  log so a re-grant of a recently revoked exemption reads as one.

Each context reads the account's state on-chain and states the effect in the
right units, so the model reports it rather than guessing.
"""

from dataclasses import dataclass

from eth_utils import keccak, to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import EXPLORER_URLS, Chain
from utils.erc20_metadata import fetch_erc20_metadata
from utils.formatting import format_decimal_amount, normalize_token_amount
from utils.llm.report import address_link
from utils.llm.threejane_abi import exposes, threejane_abi
from utils.logger import get_logger
from utils.proxy import eip7702_delegate
from utils.web3_wrapper import ChainManager, Web3Client

logger = get_logger("utils.llm.threejane_account_context")

USD3_ADDRESS = "0x056B269Eb1f75477a8666ae8C7fE01b64dD55eCc"
PROTOCOL_CONFIG_ADDRESS = "0x6b276A2A7dd8b629adBA8A06AD6573d01C84f34E"
USD3_SUPPLY_CAP_KEY = keccak(text="USD3_SUPPLY_CAP")
# USD3 treats a max-uint cap as "no cap" and skips the headroom check entirely.
UNLIMITED = 2**256 - 1
# SupplyCapExemptUpdated(address indexed account, bool exempt)
EXEMPT_UPDATED_TOPIC = "0x" + keccak(text="SupplyCapExemptUpdated(address,bool)").hex()
# supplyCapExempt arrived with a USD3 upgrade; its first SupplyCapExemptUpdated is at block 26057349.
# Starting the scan just before keeps the log query small.
EXEMPT_HISTORY_FROM_BLOCK = 26_050_000

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
class ExemptionChange:
    """The last ``SupplyCapExemptUpdated`` event for an account before this transaction."""

    block: int
    exempt: bool
    tx_hash: str


@dataclass(frozen=True)
class ExemptAccount:
    """One ``setSupplyCapExempt`` call's account, with its flags and type before the call."""

    address: str
    proposed_exempt: bool
    current_exempt: bool
    ring_fence_conduit: bool
    # Set only for a contract: False for an EOA, including one with an EIP-7702 delegation.
    is_contract: bool
    # The delegate an EIP-7702 EOA runs, or None.
    delegate: str | None
    usd3_balance_raw: int
    # Last on-chain flag change before this transaction; None when the account has none on record.
    last_change: ExemptionChange | None = None

    def kind(self) -> str:
        """EOA, delegated EOA, or contract — a delegation designator is not a deployed contract."""
        if self.delegate:
            return f"EOA with EIP-7702 delegation to {self.delegate}"
        return "contract" if self.is_contract else "EOA"

    def change(self) -> str:
        """Before → after for the flag, flagging a no-op."""
        after = str(self.proposed_exempt).lower()
        if self.current_exempt == self.proposed_exempt:
            return f"already {after} (no change)"
        return f"{str(self.current_exempt).lower()} → {after}"

    def pairing_issue(self) -> str:
        """How this change leaves the flag out of step with ringFenceConduit, or "".

        Only a revocation that leaves the conduit flag set breaks the natspec's pairing:
        the ring fence only matters for LCC capital-call funding, so a wallet exempted
        without it is not missing a protection. What a wallet exemption does mean is
        stated once, in ``SupplyCapExemptContext.purpose_line``.
        """
        if not self.proposed_exempt and self.current_exempt and self.ring_fence_conduit:
            return (
                "exemption revoked while ringFenceConduit stays true: third-party deposits become possible and "
                "receive no ring-fence credit (USD3 natspec says to revoke both flags together)"
            )
        return ""

    def history(self) -> str:
        """The account's last flag change before this transaction, naming a re-grant as one."""
        change = self.last_change
        if change is None:
            return "no earlier exemption change on record"
        if self.proposed_exempt and not self.current_exempt and not change.exempt:
            return f"re-grants an exemption revoked at block {change.block} (tx {change.tx_hash})"
        return f"before this transaction, last set to {str(change.exempt).lower()} at block {change.block} (tx {change.tx_hash})"


@dataclass(frozen=True)
class SupplyCapExemptContext:
    """Every ``setSupplyCapExempt`` call on USD3 in one alert, with the state they share.

    One context per batch rather than per account: the flag's semantics and the
    cap position are the same for every account, and a 44-call revocation
    otherwise repeated them 44 times in both the prompt and the report.
    """

    usd3_address: str
    asset: TokenUnit
    share: TokenUnit
    min_deposit_raw: int
    supply_cap_raw: int
    total_assets_raw: int
    accounts: tuple[ExemptAccount, ...]
    # Set when USD3's SupplyCapExemptUpdated log was read; history lines are omitted otherwise.
    history_read: bool = False
    exempt_before: int = 0
    exempt_conduits_before: int = 0

    @property
    def addresses(self) -> list[str]:
        return [self.usd3_address, *(account.address for account in self.accounts)]

    @property
    def labels(self) -> dict[str, str]:
        return {self.usd3_address: "USD3"}

    def overview_line(self) -> str:
        """Counts by direction and account type, so the model does not tally 44 lines itself.

        Directions count calls; types, holders and the USD3 total count distinct
        accounts, so an account named in two calls is not counted — or its
        balance summed — twice.
        """
        grants = sum(1 for a in self.accounts if a.proposed_exempt and not a.current_exempt)
        revokes = sum(1 for a in self.accounts if not a.proposed_exempt and a.current_exempt)
        noops = len(self.accounts) - grants - revokes
        distinct = list({a.address: a for a in self.accounts}.values())
        delegated = sum(1 for a in distinct if a.delegate)
        contracts = sum(1 for a in distinct if a.is_contract)
        plain = len(distinct) - delegated - contracts
        holders = [a for a in distinct if a.usd3_balance_raw]
        held = sum(a.usd3_balance_raw for a in holders)
        return (
            f"{len(self.accounts)} setSupplyCapExempt call(s) on USD3 across {len(distinct)} account(s): "
            f"{grants} grant (false → true), {revokes} revoke (true → false), {noops} no-op. "
            f"Accounts: {plain} EOA, "
            f"{delegated} EOA with an EIP-7702 delegation (a key-controlled wallet running delegated code, not a "
            f"deployed contract), {contracts} contract. {len(holders)} hold USD3, "
            f"{self.share.amount(held)} in total."
        )

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
            "cannot deposit, and a supply cap of 0 blocks every deposit. The flag does not touch existing balances "
            "or withdrawals."
        )

    def purpose_line(self) -> str:
        """Why the cap and the exemption exist, so a grant to a wallet reads as what it is.

        Sources: 3Jane docs (``USD3_SUPPLY_CAP`` "controls overall protocol size and risk
        exposure"), the USD3 contract header ("Supply-cap exemptions for protocol-controlled
        deposit receivers"), and USD3's ``_effectiveDeployCapWaUSDC`` (deployment capped by
        sUSD3 backing; borrowing bounded by DEBT_CAP, which reads debt, never supply).
        """
        return (
            "Why it matters: USD3_SUPPLY_CAP controls overall protocol size and risk exposure (3Jane docs). USD3's "
            "contract header reserves exemptions for protocol-controlled deposit receivers: LCC vaults, whose "
            "capital-call funding and auction fills must not fail on a full cap. An exemption for an EOA, or any "
            "account the protocol does not control, is a per-wallet right to grow USD3 past the cap, with no limit, "
            "outside that documented purpose. A deposit above the cap adds no credit exposure (deployment to credit "
            "is capped by sUSD3 backing via MIN_SUSD3_BACKING_RATIO, and borrowing by DEBT_CAP), so the excess "
            "earns only waUSDC base yield and dilutes USD3's return."
        )

    def pairing_line(self) -> str:
        """The natspec pairs the flag with ringFenceConduit; say where this batch breaks the pairing."""
        issues = [a for a in self.accounts if a.pairing_issue()]
        pairing = (
            "USD3 natspec pairs this flag with ringFenceConduit: with both set, every accepted deposit is a "
            "self-deposit that gets ring-fence credit, and LCC deployment grants both atomically. The ring fence only "
            "matters for LCC capital-call funding: without ringFenceConduit, an exempt account's deposits are ordinary "
            "withdrawable USD3 liquidity. The pairing breaks when an exemption is revoked while ringFenceConduit "
            "stays true."
        )
        if not issues:
            return pairing + " No call in this batch leaves the two flags out of step."
        return pairing + f" {len(issues)} account(s) end up out of step (see the per-account list)."

    def exempt_set_line(self) -> str:
        """How many accounts were exempt before this transaction, from USD3's own log."""
        if not self.history_read:
            return ""
        return (
            f"Exempt before this transaction (replayed from SupplyCapExemptUpdated): {self.exempt_before} "
            f"account(s), {self.exempt_conduits_before} of them also ring-fence conduits."
        )

    def supply_line(self) -> str:
        """Where USD3 supply stands against the cap the exemption bypasses."""
        cap, assets = self.supply_cap_raw, self.total_assets_raw
        if cap == UNLIMITED:
            return f"USD3 now: totalAssets {self.asset.amount(assets)}; USD3_SUPPLY_CAP is unlimited (max uint256)."
        if assets >= cap:
            standing = "exactly at the cap" if assets == cap else f"above the cap by {self.asset.amount(assets - cap)}"
            return (
                f"USD3 now: totalAssets {self.asset.amount(assets)} against USD3_SUPPLY_CAP "
                f"{self.asset.amount(cap)} ({standing}), so "
                "availableDepositLimit is 0 for every non-exempt receiver until totalAssets falls below the cap or "
                "the cap rises."
            )
        return (
            f"USD3 now: totalAssets {self.asset.amount(assets)} against USD3_SUPPLY_CAP "
            f"{self.asset.amount(cap)} ({self.asset.amount(cap - assets)} of headroom)."
        )

    def account_line(self, account: ExemptAccount) -> str:
        """One account's change, type, ring-fence flag and USD3 balance."""
        line = (
            f"{account.address} ({account.kind()}): {account.change()}; ringFenceConduit "
            f"{str(account.ring_fence_conduit).lower()}; holds {self.share.amount(account.usd3_balance_raw)}"
        )
        if self.history_read:
            line = f"{line}; {account.history()}"
        issue = account.pairing_issue()
        return f"{line}; {issue}" if issue else line


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
    """Read every account's USD3 flags, code and balance, plus USD3's cap and supply, in one batch."""
    if target.lower() != USD3_ADDRESS.lower():
        return []
    changes = [
        args for call in calls if call.function_name == "setSupplyCapExempt" and (args := _address_bool_args(call))
    ]
    if not changes:
        return []

    client = ChainManager.get_client(Chain.from_chain_id(chain_id))
    usd3_address = to_checksum_address(USD3_ADDRESS)
    history = _read_exemption_history(client, usd3_address)
    exempt_before = [account for account, change in (history or {}).items() if change.exempt]
    usd3 = client.get_contract(usd3_address, threejane_abi("USD3"))
    vault = client.get_contract(usd3_address, threejane_abi("ERC4626Vault"))
    config = client.get_contract(to_checksum_address(PROTOCOL_CONFIG_ADDRESS), threejane_abi("ProtocolConfig"))
    with client.batch_requests() as batch:
        batch.add(vault.functions.asset())
        batch.add(vault.functions.totalAssets())
        batch.add(usd3.functions.minDeposit())
        batch.add(config.functions.config(USD3_SUPPLY_CAP_KEY))
        # USD3's own metadata comes from the batch: it serves symbol()/decimals() through the
        # TokenizedStrategy fallback, which fetch_erc20_metadata's bytecode gate cannot see.
        batch.add(vault.functions.symbol())
        batch.add(vault.functions.decimals())
        # Each request is built inside the batch context; one built outside it runs unbatched.
        for account, _ in changes:
            batch.add(usd3.functions.supplyCapExempt(account))
            batch.add(usd3.functions.ringFenceConduit(account))
            batch.add(vault.functions.balanceOf(account))
            batch.add(client.eth.get_code(account))
        for account in exempt_before:
            batch.add(usd3.functions.ringFenceConduit(account))
        (
            asset_address,
            total_assets,
            min_deposit,
            supply_cap,
            share_symbol,
            share_decimals,
            *results,
        ) = client.execute_batch(batch)
    per_account = results[: 4 * len(changes)]
    exempt_conduits = results[4 * len(changes) :]

    asset = fetch_token_unit(chain_id, str(asset_address))
    if asset is None:
        logger.info("3Jane USD3 %s: asset metadata unavailable", usd3_address)
        return []
    share = TokenUnit(address=usd3_address, symbol=str(share_symbol), decimals=int(share_decimals))

    accounts = []
    # Calls execute in order, so a repeated account's "before" is what the previous call set.
    flag_before: dict[str, bool] = {}
    for index, (account, proposed) in enumerate(changes):
        exempt, ring_fence, balance, code = per_account[4 * index : 4 * index + 4]
        delegate = eip7702_delegate(code)
        current = flag_before.get(account, bool(exempt))
        flag_before[account] = proposed
        accounts.append(
            ExemptAccount(
                address=account,
                proposed_exempt=proposed,
                current_exempt=current,
                ring_fence_conduit=bool(ring_fence),
                is_contract=bool(code) and delegate is None,
                delegate=delegate,
                usd3_balance_raw=int(balance),
                last_change=(history or {}).get(account),
            )
        )
    return [
        SupplyCapExemptContext(
            usd3_address=usd3_address,
            asset=asset,
            share=share,
            min_deposit_raw=int(min_deposit),
            supply_cap_raw=int(supply_cap),
            total_assets_raw=int(total_assets),
            accounts=tuple(accounts),
            history_read=history is not None,
            exempt_before=len(exempt_before),
            exempt_conduits_before=sum(1 for conduit in exempt_conduits if conduit),
        )
    ]


def _read_exemption_history(client: Web3Client, usd3_address: str) -> dict[str, ExemptionChange] | None:
    """Replay USD3's SupplyCapExemptUpdated log into each account's last change.

    Returns None when the log cannot be read; RPC providers cap log ranges
    differently, and the history is enrichment, not a reason to drop the context.
    """
    try:
        logs = client.eth.get_logs(
            {
                "address": usd3_address,
                "topics": [EXEMPT_UPDATED_TOPIC],
                "fromBlock": EXEMPT_HISTORY_FROM_BLOCK,
                "toBlock": "latest",
            }
        )
    except Exception as e:  # noqa: BLE001 - best-effort enrichment
        logger.info("3Jane USD3 %s: exemption history unavailable: %s", usd3_address, e)
        return None
    last_change: dict[str, ExemptionChange] = {}
    for log in logs:  # chain order, so a later event overwrites an earlier one
        account = to_checksum_address(bytes(log["topics"][1])[-20:])
        last_change[account] = ExemptionChange(
            block=int(log["blockNumber"]),
            exempt=int.from_bytes(bytes(log["data"]), "big") != 0,
            tx_hash="0x" + bytes(log["transactionHash"]).hex(),
        )
    return last_change


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
            f"USD3.setSupplyCapExempt on {context.usd3_address}: {context.overview_line()}",
            context.semantics_line(),
            context.purpose_line(),
            context.pairing_line(),
            context.supply_line(),
            *([line] if (line := context.exempt_set_line()) else []),
            "Per account, in call order:",
            *(f"- {context.account_line(account)}" for account in context.accounts),
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

    lines = [
        f"- **USD3 supply-cap exemptions** on {address_link(context.usd3_address, chain_id, labels)}: "
        f"{context.overview_line()}",
        f"  - {context.semantics_line()}",
        f"  - {context.purpose_line()}",
        f"  - {context.pairing_line()}",
        f"  - {context.supply_line()}",
    ]
    if exempt_set := context.exempt_set_line():
        lines.append(f"  - {exempt_set}")
    history_header = " Last change |" if context.history_read else ""
    lines += [
        "",
        f"| # | Account | Type | supplyCapExempt | ringFenceConduit | USD3 balance |{history_header}",
        "|---|---|---|---|---|---|" + ("---|" if context.history_read else ""),
    ]
    for number, account in enumerate(context.accounts, start=1):
        kind = (
            f"EOA, EIP-7702 → {address_link(account.delegate, chain_id, labels)}"
            if account.delegate
            else account.kind()
        )
        issue = f" ⚠️ {account.pairing_issue()}" if account.pairing_issue() else ""
        history = f" {_history_cell(account.last_change, chain_id)} |" if context.history_read else ""
        lines.append(
            f"| {number} | {address_link(account.address, chain_id, labels)} | {kind} | {account.change()} | "
            f"{str(account.ring_fence_conduit).lower()}{issue} | `{context.share.amount(account.usd3_balance_raw)}` |"
            f"{history}"
        )
    return "\n".join(lines)


def _history_cell(change: ExemptionChange | None, chain_id: int) -> str:
    """``set false at [block N](tx link)`` for the report table, or "none on record"."""
    if change is None:
        return "none on record"
    explorer = EXPLORER_URLS.get(chain_id)
    block = f"[block {change.block}]({explorer}/tx/{change.tx_hash})" if explorer else f"block {change.block}"
    return f"set {str(change.exempt).lower()} at {block}"

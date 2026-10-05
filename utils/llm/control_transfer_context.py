"""Describe who gains control when a call hands over management, ownership, or a role.

A call like ``set_management(0xac7D…)`` names the new controller by address
only. Whether that is a 6-of-9 multisig, a 2-of-4 multisig behind an operator
contract, or a single EOA decides how serious the change is, and the LLM saw
none of it: a report once described moving a Funding Distributor from Yearn's
6-of-9 Safe to an Executor contract managed by a 2-of-4 Safe as merely a
"pending management handover".

For every call whose shape transfers control, this adapter reads, on-chain:

- the current controller (the target's own ``management()`` / ``owner()`` / …),
- the proposed controller, classified as EOA, EIP-7702 delegated EOA, Safe
  (threshold and owner count), or contract — following a contract one hop to
  its own controller, and noting an operator whitelist,
- whether the transfer is two-step: decided from the call itself (a
  nominate-only setter, BoringOwnable's ``direct`` flag, or which slot the
  setter's source writes), never from the mere existence of a pending slot,
  since some setters bypass it; and
- how the signing threshold behind control changes.

It keys on call shape, not protocol, so every governance alert benefits.
"""

from dataclasses import dataclass

from eth_utils import to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.eth_view import call_view
from utils.llm.abi_exposure import exposes
from utils.llm.report import address_link
from utils.logger import get_logger
from utils.on_chain_state import resolve_function_source
from utils.source_context import find_state_var_writes, get_contract_label
from utils.web3_wrapper import ChainManager, Web3Client

logger = get_logger("utils.llm.control_transfer_context")

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
# EIP-7702 delegation designator: an EOA whose code is 0xef0100 || delegate.
_EIP7702_PREFIX = "ef0100"


@dataclass(frozen=True)
class _Role:
    """A controller role: the getter naming the current holder and any pending slot."""

    name: str
    getters: tuple[str, ...]
    pending_getters: tuple[str, ...]


_MANAGEMENT = _Role("management", ("management",), ("pending_management", "pendingManagement"))
_OWNER = _Role("owner", ("owner",), ("pendingOwner", "pending_owner"))
_GOVERNANCE = _Role("governance", ("governance",), ("pending_governance", "pendingGovernance"))
_ADMIN = _Role("admin", ("admin",), ("pendingAdmin", "pending_admin"))
_ROLE_MANAGER = _Role("role manager", ("role_manager", "roleManager"), ("future_role_manager", "pendingRoleManager"))

# Setter name with underscores removed and lowercased → the role it transfers.
_CONTROL_SETTERS: dict[str, _Role] = {
    "setmanagement": _MANAGEMENT,
    "transfermanagement": _MANAGEMENT,
    "setpendingmanagement": _MANAGEMENT,
    "transferownership": _OWNER,
    "setowner": _OWNER,
    "setpendingowner": _OWNER,
    "setgovernance": _GOVERNANCE,
    "transfergovernance": _GOVERNANCE,
    "setpendinggovernance": _GOVERNANCE,
    "setadmin": _ADMIN,
    "changeadmin": _ADMIN,
    "transferadmin": _ADMIN,
    "setpendingadmin": _ADMIN,
    "setrolemanager": _ROLE_MANAGER,
    "transferrolemanager": _ROLE_MANAGER,
}

# Setters that can only nominate: the role moves when the nominee accepts.
_NOMINATE_SETTERS = frozenset({"setpendingmanagement", "setpendingowner", "setpendinggovernance", "setpendingadmin"})

# BoringOwnable: `direct = true` sets the owner immediately and clears the pending slot.
_BORING_TRANSFER_OWNERSHIP = "transferOwnership(address,bool,bool)"

# Getters that name a contract's own controller, tried in order for the one-hop follow.
_CONTROLLER_GETTERS = ("management", "owner", "governance", "admin")


@dataclass(frozen=True)
class ControllerInfo:
    """What an address that holds (or would hold) control actually is."""

    address: str
    # "none" (zero address), "eoa", "eip7702", "safe", or "contract".
    kind: str
    label: str = ""
    threshold: int | None = None
    owner_count: int | None = None
    # For a contract: the getter naming its own controller, and that controller.
    controller_getter: str = ""
    controlled_by: "ControllerInfo | None" = None
    # The contract lets an operator whitelist act through it.
    has_operators: bool = False

    @property
    def safe_threshold(self) -> tuple[int, int] | None:
        """(threshold, owners) of the multisig that ultimately signs for this controller."""
        if self.kind == "safe" and self.threshold is not None and self.owner_count is not None:
            return self.threshold, self.owner_count
        if self.controlled_by is not None:
            return self.controlled_by.safe_threshold
        return None

    def describe(self) -> str:
        """One-line plain-text description, following a contract one hop."""
        name = f" ({self.label})" if self.label else ""
        if self.kind == "none":
            return f"{self.address} — the zero address: control is renounced"
        if self.kind == "eoa":
            return f"{self.address}{name} — an EOA (single private key)"
        if self.kind == "eip7702":
            return f"{self.address}{name} — an EOA with EIP-7702 delegated code (single private key)"
        if self.kind == "safe":
            return f"{self.address}{name} — Safe multisig, {self.threshold}-of-{self.owner_count} owners"
        parts = [f"{self.address}{name} — contract"]
        if self.controlled_by is not None:
            parts.append(f"its {self.controller_getter}() is {self.controlled_by.describe()}")
        if self.has_operators:
            parts.append("it keeps an operator whitelist, so addresses it approves can act through it")
        return "; ".join(parts)


@dataclass(frozen=True)
class ControlTransferContext:
    """One call that transfers control of ``target``."""

    target: str
    target_label: str
    signature: str
    role: str
    proposed: ControllerInfo
    current: ControllerInfo | None = None
    # True: this call only nominates. False: it transfers immediately.
    # None: the target has a pending slot, but this call's effect is unknown.
    two_step: bool | None = False
    # The target exposes a pending slot for the role.
    pending_slot: bool = False
    # For grantRole: the role hash being granted.
    role_hash: str = ""

    @property
    def addresses(self) -> list[str]:
        """Addresses this context introduces to the report."""
        found = [self.target]
        for info in (self.current, self.proposed):
            while info is not None:
                found.append(info.address)
                info = info.controlled_by
        return found

    @property
    def labels(self) -> dict[str, str]:
        """Labels for Safes the transfer involves, e.g. ``Safe 2-of-4``."""
        labels: dict[str, str] = {}
        for info in (self.current, self.proposed):
            while info is not None:
                if info.kind == "safe" and info.threshold is not None:
                    labels[info.address] = f"Safe {info.threshold}-of-{info.owner_count}"
                info = info.controlled_by
        return labels

    def threshold_change(self) -> str:
        """State how the signing threshold behind control changes, when both sides resolve to a Safe."""
        before = self.current.safe_threshold if self.current else None
        after = self.proposed.safe_threshold
        if before is None or after is None:
            return ""
        if after[0] < before[0]:
            verdict = "a LOWER signing threshold"
        elif after[0] > before[0]:
            verdict = "a higher signing threshold"
        else:
            verdict = "the same signing threshold"
        return (
            f"Signing threshold behind {self.role} goes from {before[0]}-of-{before[1]} to "
            f"{after[0]}-of-{after[1]} — {verdict}."
        )

    def lines(self) -> list[str]:
        """Plain-text facts for the prompt."""
        target = f"{self.target} ({self.target_label})" if self.target_label else self.target
        if self.role_hash:
            head = f"{self.signature} on {target} grants role {self.role_hash} to {self.proposed.address}."
        else:
            head = f"{self.signature} on {target} hands {self.role} to {self.proposed.address}."
        lines = [head]
        if self.current is not None:
            lines.append(f"Current {self.role}: {self.current.describe()}.")
        lines.append(f"{'Grantee' if self.role_hash else f'Proposed {self.role}'}: {self.proposed.describe()}.")
        timing = self.timing_note()
        if timing:
            lines.append(timing)
        change = self.threshold_change()
        if change:
            lines.append(change + (" Effective once accepted." if self.two_step else ""))
        return lines

    def timing_note(self) -> str:
        """When control moves: after acceptance, immediately, or undetermined."""
        if self.two_step:
            return "Two-step: this call only nominates; control moves when the nominee accepts."
        if self.two_step is None:
            return (
                f"The target keeps a pending {self.role} slot, but whether this call nominates or transfers "
                "immediately could not be determined."
            )
        if self.pending_slot:
            return "Immediate: this call transfers control directly, bypassing the target's pending slot."
        return ""


def _code_hex(client: Web3Client, address: str) -> str:
    """Deployed bytecode as bare lowercase hex ("" for an EOA)."""
    code = client.eth.get_code(address)
    return bytes(code).hex().lower()


def describe_controller(chain_id: int, client: Web3Client, address: str, hops: int = 1) -> ControllerInfo:
    """Classify ``address`` and, for a plain contract, follow its own controller ``hops`` deep."""
    address = to_checksum_address(address)
    if address == ZERO_ADDRESS:
        return ControllerInfo(address=address, kind="none")
    label = get_contract_label(chain_id, address)
    code = _code_hex(client, address)
    if not code:
        return ControllerInfo(address=address, kind="eoa", label=label)
    if code.startswith(_EIP7702_PREFIX):
        return ControllerInfo(address=address, kind="eip7702", label=label)

    threshold = call_view(client, address, "getThreshold()", "uint256")
    owners = call_view(client, address, "getOwners()", "address[]")
    if isinstance(threshold, int) and isinstance(owners, (list, tuple)):
        return ControllerInfo(
            address=address, kind="safe", label=label, threshold=int(threshold), owner_count=len(owners)
        )

    controlled_by: ControllerInfo | None = None
    controller_getter = ""
    if hops > 0:
        for getter in _CONTROLLER_GETTERS:
            holder = call_view(client, address, f"{getter}()", "address")
            if isinstance(holder, str) and int(holder, 16) != 0 and holder.lower() != address.lower():
                controlled_by = describe_controller(chain_id, client, holder, hops - 1)
                controller_getter = getter
                break
    has_operators = exposes(chain_id, address, {"operators"})
    return ControllerInfo(
        address=address,
        kind="contract",
        label=label,
        controller_getter=controller_getter,
        controlled_by=controlled_by,
        has_operators=has_operators,
    )


def _current_holder(client: Web3Client, target: str, role: _Role) -> str | None:
    """Address currently holding ``role`` on ``target``, read from its getter."""
    for getter in role.getters:
        holder = call_view(client, target, f"{getter}()", "address")
        if isinstance(holder, str):
            return holder
    return None


def _has_pending_slot(client: Web3Client, target: str, role: _Role) -> bool:
    """Whether ``target`` exposes a pending slot for ``role``."""
    return any(call_view(client, target, f"{getter}()", "address") is not None for getter in role.pending_getters)


def _slot_key(name: str) -> str:
    """Compare storage names across conventions: ``_pendingOwner`` ~ ``pending_owner`` ~ ``pendingOwner``."""
    return name.replace("_", "").lower()


def _two_step(chain_id: int, target: str, call: DecodedCall, role: _Role, pending_slot: bool) -> bool | None:
    """Whether this call only nominates (True), transfers immediately (False), or is undetermined (None).

    A pending slot on the target is not enough: BoringOwnable's
    ``transferOwnership(owner, direct, renounce)`` has one yet transfers
    immediately when ``direct`` is true. The call itself decides.
    """
    normalized = _slot_key(call.function_name or "")
    if normalized in _NOMINATE_SETTERS:
        return True
    if call.signature == _BORING_TRANSFER_OWNERSHIP and len(call.params) >= 2:
        return not bool(call.params[1][1])
    if not pending_slot:
        return False

    source = resolve_function_source(chain_id, target, call.function_name)
    writes = {_slot_key(name) for name in find_state_var_writes(source, call.function_name, True)} if source else set()
    writes_pending = bool(writes & {_slot_key(g) for g in role.pending_getters})
    writes_role = bool(writes & {_slot_key(g) for g in role.getters})
    if writes_pending and not writes_role:
        return True
    if writes_role and not writes_pending:
        return False
    return None


def _first_address(call: DecodedCall) -> str | None:
    """First address-typed argument of a call — the nominee for every setter handled here."""
    for type_str, value in call.params:
        if type_str == "address" and isinstance(value, str):
            return value
    return None


def _resolve_one(chain_id: int, client: Web3Client, target: str, call: DecodedCall) -> ControlTransferContext | None:
    """Build the context for one call, or None when it does not transfer control."""
    normalized = (call.function_name or "").replace("_", "").lower()
    target = to_checksum_address(target)
    if normalized == "grantrole" and call.signature == "grantRole(bytes32,address)":
        grantee = _first_address(call)
        role_value = call.params[0][1]
        role_hash = "0x" + role_value.hex() if isinstance(role_value, bytes) else str(role_value)
        if grantee is None:
            return None
        return ControlTransferContext(
            target=target,
            target_label=get_contract_label(chain_id, target),
            signature=call.signature,
            role="role",
            proposed=describe_controller(chain_id, client, grantee),
            role_hash=role_hash,
        )

    role = _CONTROL_SETTERS.get(normalized)
    new_holder = _first_address(call) if role else None
    if role is None or new_holder is None:
        return None
    current = _current_holder(client, target, role)
    pending_slot = _has_pending_slot(client, target, role)
    return ControlTransferContext(
        target=target,
        target_label=get_contract_label(chain_id, target),
        signature=call.signature,
        role=role.name,
        proposed=describe_controller(chain_id, client, new_holder),
        current=describe_controller(chain_id, client, current) if current else None,
        two_step=_two_step(chain_id, target, call, role, pending_slot),
        pending_slot=pending_slot,
    )


def resolve_control_transfer_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[ControlTransferContext]:
    """Describe the controllers behind every control-transferring call in one alert.

    ``protocol`` is unused: handing over control means the same thing whoever governs.
    """
    del protocol
    candidates = [
        (target, call)
        for target, call in targets_and_calls
        if (call.function_name or "").replace("_", "").lower() in {*_CONTROL_SETTERS, "grantrole"}
    ]
    if not candidates:
        return []
    client = ChainManager.get_client(Chain.from_chain_id(chain_id))

    contexts: list[ControlTransferContext] = []
    seen: set[tuple[str, str, str]] = set()
    for target, call in candidates:
        key = (target.lower(), call.signature, str(call.params))
        if key in seen:
            continue
        seen.add(key)
        try:
            context = _resolve_one(chain_id, client, target, call)
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Control-transfer context failed for %s.%s: %s", target, call.function_name, error)
            continue
        if context is not None:
            contexts.append(context)
    return contexts


def format_control_transfer_prompt(contexts: list[ControlTransferContext]) -> str:
    """Render verified control-transfer facts for the LLM prompt."""
    return "\n\n".join("\n".join(context.lines()) for context in contexts)


def describe_controller_markdown(info: ControllerInfo, chain_id: int, labels: dict[str, str]) -> str:
    """Markdown twin of ``ControllerInfo.describe`` with explorer links."""
    link = address_link(info.address, chain_id, labels)
    if info.kind == "none":
        return f"{link} — zero address: control is **renounced**"
    if info.kind == "eoa":
        return f"{link} — **EOA** (single private key)"
    if info.kind == "eip7702":
        return f"{link} — **EOA with EIP-7702 delegated code** (single private key)"
    if info.kind == "safe":
        return f"{link} — Safe multisig, **{info.threshold}-of-{info.owner_count}**"
    parts = [f"{link} — contract"]
    if info.controlled_by is not None:
        parts.append(
            f"its `{info.controller_getter}()` is {describe_controller_markdown(info.controlled_by, chain_id, labels)}"
        )
    if info.has_operators:
        parts.append("keeps an **operator whitelist** (approved addresses act through it)")
    return "; ".join(parts)


def format_control_transfer_report(
    contexts: list[ControlTransferContext],
    chain_id: int,
    labels: dict[str, str],
) -> str:
    """Render the deterministic control-transfer section for the gist report."""
    sections: list[str] = []
    for context in contexts:
        target = address_link(context.target, chain_id, labels)
        if context.role_hash:
            lines = [f"**`{context.signature}`** on {target} — grants role `{context.role_hash}`"]
        else:
            lines = [f"**`{context.signature}`** on {target} — hands over **{context.role}**"]
        if context.current is not None:
            lines.append(
                f"- **Current {context.role}:** {describe_controller_markdown(context.current, chain_id, labels)}"
            )
        who = "Grantee" if context.role_hash else f"Proposed {context.role}"
        lines.append(f"- **{who}:** {describe_controller_markdown(context.proposed, chain_id, labels)}")
        timing = context.timing_note()
        if timing:
            heading, _, detail = timing.partition(": ")
            lines.append(f"- **{heading}:** {detail}" if detail else f"- **Timing:** {timing}")
        change = context.threshold_change()
        if change:
            lines.append(f"- **Threshold:** {change}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)

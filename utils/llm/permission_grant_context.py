"""Describe what an allowlist entry lets its holder do.

``setKeeper(0x4E44…, true)`` names a mapping entry, not a capability. A report
once called the scope of a yHaaS relayer keeper "not shown" although the
verified source is 70 lines and one keeper function, ``forwardCall(address,
bytes)``, makes any call as the relayer — so a keeper holds every permission
the relayer holds.

For a call that writes an address-keyed allowlist (a mapping or an OpenZeppelin
``EnumerableSet``), this adapter reads the target's verified Solidity source and
lists the external functions that check that allowlist against ``msg.sender`` —
directly, through a modifier, or through a helper such as
``isExecutor(msg.sender)`` — flagging the ones that forward arbitrary calldata.
The holder is classified (EOA / Safe / contract); for a contract, its own
arbitrary-call functions are listed too, since whoever can drive it inherits the
entry. When the contract exposes an ``address[]`` getter for the allowlist, the
holders before and after the call are listed.

It keys on call shape and verified source, not protocol. Vyper sources are
skipped: their access checks don't follow these Solidity shapes.
"""

import re
from dataclasses import dataclass

from eth_utils import to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.llm.control_transfer_context import ControllerInfo, describe_controller, describe_controller_markdown
from utils.llm.report import address_link
from utils.logger import get_logger
from utils.solidity_text import FunctionDef, contract_functions
from utils.source_context import fetch_verified_contract, get_contract_label
from utils.storage_scope import inheritance_chain
from utils.verified_contract import VerifiedContract
from utils.web3_wrapper import ChainManager, Web3Client

logger = get_logger("utils.llm.permission_grant_context")

# Setter-name prefixes that add or remove an allowlist entry, and the entry's
# resulting state when the call carries no bool saying so.
_GRANT_PREFIXES = ("add", "grant", "allow", "enable", "whitelist", "approve")
_REVOKE_PREFIXES = ("remove", "revoke", "disallow", "disable", "unwhitelist", "blacklist")
_NEUTRAL_PREFIXES = ("set", "update", "toggle")

# ERC20/ERC721 approvals write per-owner allowances, not caller allowlists.
_EXCLUDED = frozenset({"approve", "setapprovalforall", "grantrole", "revokerole"})

_SENDER = r"(?:msg\.sender|_msgSender\(\s*\))"
# `keepers[x] = …`, `allowed[a][b] = …` (not `==`).
_INDEXED_WRITE_RE = re.compile(r"\b([A-Za-z_]\w*)\s*(?:\[[^\[\]]*\])+\s*=(?!=)")
# `_executors.add(x)` / `.remove(x)` on an EnumerableSet.
_SET_WRITE_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\.\s*(?:add|remove)\s*\(")
_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_ARBITRARY_CALL_RE = re.compile(r"\.call\s*[({]|functionCall\w*\s*\(")
_DELEGATECALL_RE = re.compile(r"\.delegatecall\s*\(|functionDelegateCall\s*\(")


@dataclass(frozen=True)
class GatedFunction:
    """An external entry point that checks the allowlist against ``msg.sender``."""

    signature: str
    via: str  # "modifier onlyKeepers", "helper isExecutor", or "inline check"
    arbitrary_call: str = ""  # "call" / "delegatecall" when it forwards arbitrary calldata


@dataclass(frozen=True)
class PermissionGrantContext:
    """One call that adds or removes an allowlist entry on ``target``."""

    target: str
    target_label: str
    signature: str
    allowlist: str
    holder: ControllerInfo
    # True: the entry is enabled; False: removed; None: could not be told from the call.
    enabled: bool | None
    gated: tuple[GatedFunction, ...]
    # The holder's own arbitrary-call entry points, with their source modifiers.
    holder_forwarders: tuple[str, ...] = ()
    # Allowlist members before the call, when the contract exposes an address[] getter.
    holders_before: tuple[str, ...] | None = None

    @property
    def addresses(self) -> list[str]:
        """Addresses this context introduces to the report."""
        found = [self.target]
        info: ControllerInfo | None = self.holder
        while info is not None:
            found.append(info.address)
            info = info.controlled_by
        return [*found, *(self.holders_before or ())]

    @property
    def labels(self) -> dict[str, str]:
        """Labels for Safes behind the holder."""
        labels: dict[str, str] = {}
        info: ControllerInfo | None = self.holder
        while info is not None:
            if info.kind == "safe" and info.threshold is not None:
                labels[info.address] = f"Safe {info.threshold}-of-{info.owner_count}"
            info = info.controlled_by
        return labels

    @property
    def verb(self) -> str:
        """What the call does to the entry."""
        if self.enabled is None:
            return "CHANGES"
        return "GRANTS" if self.enabled else "REVOKES"

    def holders_after(self) -> tuple[str, ...] | None:
        """Allowlist members once the call applies, when the members are enumerable."""
        if self.holders_before is None or self.enabled is None:
            return None
        others = tuple(a for a in self.holders_before if a.lower() != self.holder.address.lower())
        return (*others, self.holder.address) if self.enabled else others

    def scope_line(self) -> str:
        """Which entry points the allowlist opens, grouped by how they check it."""
        by_via: dict[str, list[str]] = {}
        for fn in self.gated:
            by_via.setdefault(fn.via, []).append(fn.signature)
        groups = "; ".join(f"{via} gates {', '.join(signatures)}" for via, signatures in by_via.items())
        return f"Verified source: `{self.allowlist}` is checked against msg.sender — {groups}."

    def forwarder_line(self) -> str:
        """Warn when an allowlisted caller can make the target call anything."""
        forwarders = [fn for fn in self.gated if fn.arbitrary_call]
        if not forwarders:
            return ""
        names = ", ".join(f"{fn.signature} ({fn.arbitrary_call})" for fn in forwarders)
        target = self.target_label or self.target
        return (
            f"ARBITRARY CALL: {names} forwards any calldata to any address as {target}, so an allowlisted "
            f"caller can use every permission {target} itself holds (keeper and role grants on other "
            "contracts, its token balances and approvals) — the entry is not limited to the named functions."
        )

    def lines(self) -> list[str]:
        """Plain-text facts for the prompt."""
        target = f"{self.target} ({self.target_label})" if self.target_label else self.target
        lines = [
            f"{self.signature} on {target} {self.verb} {self.holder.address} an entry in `{self.allowlist}`.",
            self.scope_line(),
        ]
        forwarder = self.forwarder_line()
        if forwarder:
            lines.append(forwarder)
        lines.append(f"Holder: {self.holder.describe()}.")
        if self.holder_forwarders:
            lines.append(
                "The holder itself forwards arbitrary calls — "
                f"{', '.join(self.holder_forwarders)} — so whoever passes its checks acts with this entry."
            )
        after = self.holders_after()
        if self.holders_before is not None and after is not None:
            lines.append(
                f"`{self.allowlist}` members before: {', '.join(self.holders_before) or 'none'}; "
                f"after: {', '.join(after) or 'none'}."
            )
        return lines


def _client(chain_id: int) -> Web3Client:
    return ChainManager.get_client(Chain.from_chain_id(chain_id))


def _entry_state(call: DecodedCall) -> bool | None:
    """Whether the call enables (True) or removes (False) the entry, from a bool arg or its name."""
    for type_str, value in call.params:
        if type_str == "bool":
            return bool(value)
    name = (call.function_name or "").lower()
    if name.startswith(_REVOKE_PREFIXES):
        return False
    if name.startswith(_GRANT_PREFIXES):
        return True
    return None


def _is_candidate(call: DecodedCall) -> bool:
    """Setter-shaped call with an address argument."""
    name = (call.function_name or "").replace("_", "").lower()
    if name in _EXCLUDED or not name.startswith((*_GRANT_PREFIXES, *_REVOKE_PREFIXES, *_NEUTRAL_PREFIXES)):
        return False
    return any(type_str == "address" for type_str, _ in call.params)


def _first_address(call: DecodedCall) -> str | None:
    for type_str, value in call.params:
        if type_str == "address" and isinstance(value, str):
            return value
    return None


def _functions(contract: VerifiedContract) -> list[FunctionDef]:
    """Functions and modifiers of the deployed contract and every base it inherits."""
    if not contract.contract_file or contract.language.lower() != "solidity":
        return []
    found: list[FunctionDef] = []
    for name, path in inheritance_chain(contract, contract.contract_file):
        found.extend(contract_functions(contract.sources[path], name) or [])
    return found


def _source_functions(chain_id: int, address: str, function_name: str) -> list[FunctionDef]:
    """Verified functions of ``address`` — or of its EIP-1967 implementation when it lacks ``function_name``."""
    contract = fetch_verified_contract(chain_id, address)
    functions = _functions(contract) if contract else []
    if any(fn.name == function_name for fn in functions):
        return functions

    from utils.proxy import get_current_implementation

    implementation = get_current_implementation(address, chain_id)
    if not implementation or implementation.lower() == address.lower():
        return []
    contract = fetch_verified_contract(chain_id, implementation)
    functions = _functions(contract) if contract else []
    return functions if any(fn.name == function_name for fn in functions) else []


def _by_name(functions: list[FunctionDef]) -> dict[str, list[FunctionDef]]:
    named: dict[str, list[FunctionDef]] = {}
    for fn in functions:
        named.setdefault(fn.name, []).append(fn)
    return named


def _written_allowlists(setter: FunctionDef, named: dict[str, list[FunctionDef]]) -> list[str]:
    """Indexed or EnumerableSet state the setter writes, following one internal call."""
    bodies = [setter.body]
    for callee in _CALL_RE.findall(setter.body):
        bodies.extend(fn.body for fn in named.get(callee, []) if fn.visibility in ("internal", "private"))
    written: list[str] = []
    for body in bodies:
        for regex in (_INDEXED_WRITE_RE, _SET_WRITE_RE):
            for name in regex.findall(body):
                if name not in written:
                    written.append(name)
    return written


def _arbitrary_call(fn: FunctionDef) -> str:
    """ "call"/"delegatecall" when ``fn`` takes an address and bytes and forwards them, else ""."""
    params = fn.params.split(",")
    if "address" not in params or not any(p in ("bytes", "bytes[]") for p in params):
        return ""
    if _DELEGATECALL_RE.search(fn.body):
        return "delegatecall"
    return "call" if _ARBITRARY_CALL_RE.search(fn.body) else ""


def _gated_functions(functions: list[FunctionDef], allowlist: str) -> list[GatedFunction]:
    """External entry points that check ``allowlist`` against ``msg.sender``."""
    var = re.escape(allowlist)
    reads_sender = re.compile(rf"\b{var}\s*\[\s*{_SENDER}\s*\]|\b{var}\s*\.\s*contains\s*\(\s*{_SENDER}\s*\)")
    # Functions that look an address up in the allowlist: `isExecutor(a)` → `_executors.contains(a)`.
    lookups = {
        fn.name
        for fn in functions
        if fn.kind == "function" and re.search(rf"\b{var}\s*(?:\[|\.\s*contains\s*\()", fn.body)
    }
    # Internal helpers that check the caller themselves: `_checkKeeper()` → `keepers[msg.sender]`.
    checkers = {
        fn.name
        for fn in functions
        if fn.kind == "function" and fn.visibility in ("internal", "private") and reads_sender.search(fn.body)
    }

    def check(body: str) -> str:
        if reads_sender.search(body):
            return "inline check"
        for name in sorted(lookups):
            if re.search(rf"\b{re.escape(name)}\s*\(\s*{_SENDER}\s*\)", body):
                return f"helper {name}"
        for name in sorted(checkers):
            if re.search(rf"\b{re.escape(name)}\s*\(", body):
                return f"helper {name}"
        return ""

    modifiers: dict[str, str] = {}
    for fn in functions:
        how = check(fn.body) if fn.kind == "modifier" else ""
        if how:
            modifiers[fn.name] = f"modifier {fn.name}" if how == "inline check" else f"modifier {fn.name} ({how})"
    gated: list[GatedFunction] = []
    seen: set[str] = set()
    for fn in functions:
        if fn.kind not in ("function", "fallback") or fn.visibility not in ("external", "public", ""):
            continue
        if fn.signature in seen:
            continue
        via = next((modifiers[m.split("(")[0]] for m in fn.modifiers if m.split("(")[0] in modifiers), "") or check(
            fn.body
        )
        if via:
            seen.add(fn.signature)
            gated.append(GatedFunction(fn.signature, via, _arbitrary_call(fn)))
    return gated


def _holder_forwarders(chain_id: int, holder: ControllerInfo) -> tuple[str, ...]:
    """Arbitrary-call entry points on a contract holder, with their source modifiers."""
    if holder.kind != "contract":
        return ()
    contract = fetch_verified_contract(chain_id, holder.address)
    found: list[str] = []
    for fn in _functions(contract) if contract else []:
        if fn.kind == "function" and fn.visibility in ("external", "public") and _arbitrary_call(fn):
            guards = [m for m in fn.modifiers if m not in ("payable", "view", "pure", "nonpayable")]
            found.append(f"{fn.signature} [{', '.join(guards) or 'no modifier'}]")
    return tuple(dict.fromkeys(found))


def _enumerated_members(chain_id: int, client: Web3Client, target: str, allowlist: str) -> tuple[str, ...] | None:
    """Members of the allowlist from a no-arg ``address[]`` getter named after it, e.g. ``getExecutors()``."""
    stem = allowlist.lstrip("_").lower()
    contract = fetch_verified_contract(chain_id, target)
    for entry in contract.abi if contract else []:
        name = str(entry.get("name") or "")
        outputs = entry.get("outputs") or []
        if (
            entry.get("type") == "function"
            and entry.get("stateMutability") in ("view", "pure")
            and not entry.get("inputs")
            and len(outputs) == 1
            and outputs[0].get("type") == "address[]"
            and stem in name.lower()
        ):
            try:
                members = client.get_contract(target, [entry]).functions[name]().call()
            except Exception:  # noqa: BLE001 - optional getter
                return None
            return tuple(to_checksum_address(str(m)) for m in members)
    return None


def _resolve_one(chain_id: int, client: Web3Client, target: str, call: DecodedCall) -> PermissionGrantContext | None:
    holder_address = _first_address(call)
    if holder_address is None:
        return None
    functions = _source_functions(chain_id, target, call.function_name)
    named = _by_name(functions)
    setter_params = ",".join(type_str for type_str, _ in call.params)
    setter = next((fn for fn in named.get(call.function_name, []) if fn.params == setter_params), None)
    if setter is None:
        return None

    for allowlist in _written_allowlists(setter, named):
        gated = _gated_functions(functions, allowlist)
        if not gated:
            continue
        target = to_checksum_address(target)
        holder = describe_controller(chain_id, client, holder_address)
        return PermissionGrantContext(
            target=target,
            target_label=get_contract_label(chain_id, target),
            signature=call.signature,
            allowlist=allowlist,
            holder=holder,
            enabled=_entry_state(call),
            gated=tuple(gated),
            holder_forwarders=_holder_forwarders(chain_id, holder),
            holders_before=_enumerated_members(chain_id, client, target, allowlist),
        )
    return None


def resolve_permission_grant_context(
    protocol: str,
    chain_id: int,
    targets_and_calls: list[tuple[str, DecodedCall]],
) -> list[PermissionGrantContext]:
    """Describe the scope of every allowlist entry an alert's calls add or remove.

    ``protocol`` is unused: an allowlist means the same thing whoever governs.
    """
    del protocol
    candidates = [(target, call) for target, call in targets_and_calls if _is_candidate(call)]
    if not candidates:
        return []
    client = _client(chain_id)

    contexts: list[PermissionGrantContext] = []
    seen: set[tuple[str, str, str]] = set()
    for target, call in candidates:
        key = (target.lower(), call.signature, str(call.params))
        if key in seen:
            continue
        seen.add(key)
        try:
            context = _resolve_one(chain_id, client, target, call)
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Permission-grant context failed for %s.%s: %s", target, call.function_name, error)
            continue
        if context is not None:
            contexts.append(context)
    return contexts


def format_permission_grant_prompt(contexts: list[PermissionGrantContext]) -> str:
    """Render verified allowlist-scope facts for the LLM prompt."""
    return "\n\n".join("\n".join(context.lines()) for context in contexts)


def format_permission_grant_report(
    contexts: list[PermissionGrantContext],
    chain_id: int,
    labels: dict[str, str],
) -> str:
    """Render the deterministic allowlist-scope section for the gist report."""
    sections: list[str] = []
    for context in contexts:
        lines = [
            f"**`{context.signature}`** on {address_link(context.target, chain_id, labels)} — "
            f"{context.verb.lower()} an entry in `{context.allowlist}`",
            f"- **Holder:** {describe_controller_markdown(context.holder, chain_id, labels)}",
            "- **Entry points it opens:**",
        ]
        for fn in context.gated:
            flag = f" — **arbitrary {fn.arbitrary_call}**" if fn.arbitrary_call else ""
            lines.append(f"  - `{fn.signature}` ({fn.via}){flag}")
        if any(fn.arbitrary_call for fn in context.gated):
            lines.append(
                "- **Scope:** an arbitrary-call entry point lets the holder use every permission the target holds."
            )
        if context.holder_forwarders:
            forwarders = ", ".join(f"`{f}`" for f in context.holder_forwarders)
            lines.append(f"- **Holder forwards arbitrary calls:** {forwarders}")
        after = context.holders_after()
        if context.holders_before is not None and after is not None:
            before_links = ", ".join(address_link(a, chain_id, labels) for a in context.holders_before) or "none"
            after_links = ", ".join(address_link(a, chain_id, labels) for a in after) or "none"
            lines.append(f"- **`{context.allowlist}` members:** {before_links} → {after_links}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)

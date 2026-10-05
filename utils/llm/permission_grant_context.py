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
A forwarder whose body or modifiers check the target or calldata is reported as
restricted, not arbitrary. Each check's polarity is read too: an entry that is
refused (``require(!blocked[msg.sender])``) makes a denylist, and a list whose
checks disagree or can't be read is skipped rather than guessed.
The holder is classified (EOA / Safe / contract); for a contract, its own
arbitrary-call functions are listed too, since whoever can drive it inherits the
entry. When the contract exposes an ``address[]`` getter for the allowlist, the
holders before and after the call are listed, carried through earlier calls
of the same batch.

It keys on call shape and verified source, not protocol. Vyper sources are
skipped: their access checks don't follow these Solidity shapes.
"""

import re
from dataclasses import dataclass, replace

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
# Conditions that can restrict a forwarded call or decide a caller check.
_CONDITION_RE = re.compile(r"\b(require|assert|if)\s*\(")
_REVERT_AFTER_RE = re.compile(r"\s*\{?\s*revert\b")


@dataclass(frozen=True)
class GatedFunction:
    """An external entry point that checks the allowlist against ``msg.sender``."""

    signature: str
    via: str  # "modifier onlyKeepers", "helper isExecutor", or "inline check"
    arbitrary_call: str = ""  # "call" / "delegatecall" when it forwards arbitrary calldata
    # The checks that limit a forwarded call's target or calldata, when it has any.
    restricted_by: str = ""
    # True: a listed caller passes; False: a listed caller is refused; None: could not be told.
    allows: bool | None = True


@dataclass(frozen=True)
class PermissionGrantContext:
    """One call that adds or removes an allowlist entry on ``target``."""

    target: str
    target_label: str
    signature: str
    allowlist: str
    holder: ControllerInfo
    # True: the entry is set; False: cleared; None: could not be told from the call.
    enabled: bool | None
    gated: tuple[GatedFunction, ...]
    # The holder's own arbitrary-call entry points, with their source modifiers.
    holder_forwarders: tuple[str, ...] = ()
    # Allowlist members before the call, when the contract exposes an address[] getter.
    holders_before: tuple[str, ...] | None = None
    # A listed caller is refused rather than let through (`require(!blocked[msg.sender])`).
    denylist: bool = False

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
    def access(self) -> bool | None:
        """Whether the holder can call the gated entry points after the call."""
        return None if self.enabled is None else self.enabled != self.denylist

    @property
    def verb(self) -> str:
        """What the call does to the holder's access."""
        if self.enabled is None:
            return "CHANGES"
        if self.denylist:
            return "BLOCKS" if self.enabled else "UNBLOCKS"
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
        polarity = "a denylist: listed callers are refused" if self.denylist else "listed callers pass"
        return f"Verified source: `{self.allowlist}` is checked against msg.sender ({polarity}) — {groups}."

    def forwarder_line(self) -> str:
        """Warn when an allowlisted caller can make the target call anything."""
        forwarders = [fn for fn in self.gated if fn.arbitrary_call]
        if not forwarders:
            return ""
        names = ", ".join(f"{fn.signature} ({fn.arbitrary_call})" for fn in forwarders)
        target = self.target_label or self.target
        return (
            f"ARBITRARY CALL: {names} forwards any calldata to any address as {target}, so a caller that "
            f"passes this check can use every permission {target} itself holds (keeper and role grants on "
            "other contracts, its token balances and approvals) — the entry is not limited to the named functions."
        )

    def restricted_line(self) -> str:
        """Forwarders whose target or calldata are checked: their reach is only what the checks allow."""
        restricted = [fn for fn in self.gated if fn.restricted_by]
        if not restricted:
            return ""
        names = "; ".join(f"{fn.signature} checks {fn.restricted_by}" for fn in restricted)
        return (
            f"RESTRICTED FORWARD: {names}. These forward calls, but only to the targets and calldata those "
            "checks allow — not arbitrary calls."
        )

    def lines(self) -> list[str]:
        """Plain-text facts for the prompt."""
        target = f"{self.target} ({self.target_label})" if self.target_label else self.target
        lines = [
            f"{self.signature} on {target} {self.verb} {self.holder.address} an entry in `{self.allowlist}`.",
            self.scope_line(),
        ]
        lines.extend(line for line in (self.forwarder_line(), self.restricted_line()) if line)
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


def _entry_state(call: DecodedCall, denylist: bool) -> bool | None:
    """Whether the call sets (True) or clears (False) the entry, from a bool arg or its name.

    A name says what happens to access (``revoke``, ``blacklist``), which is the
    entry's value only on an allowlist. On a denylist ``addBlocked`` and
    ``blacklist`` both set the entry, so a name alone is left undecided there.
    """
    for type_str, value in call.params:
        if type_str == "bool":
            return bool(value)
    if denylist:
        return None
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


def _conditions(body: str) -> list[tuple[str, int, int]]:
    """(keyword, start, end) of every ``require``/``assert``/``if`` condition, ``start:end`` inside its parens."""
    found: list[tuple[str, int, int]] = []
    for match in _CONDITION_RE.finditer(body):
        depth = 0
        for i in range(match.end() - 1, len(body)):
            depth += {"(": 1, ")": -1}.get(body[i], 0)
            if depth == 0:
                found.append((match.group(1), match.end(), i))
                break
    return found


def _forwarded_call(fn: FunctionDef) -> tuple[str, str, str] | None:
    """(kind, target param, data param) when ``fn`` sends its own bytes argument to its own address argument."""
    names = fn.param_names
    types = fn.params.split(",")
    targets = [n for n, t in zip(names, types, strict=False) if n and t in ("address", "address payable")]
    payloads = [n for n, t in zip(names, types, strict=False) if n and t == "bytes"]
    for target in targets:
        for data in payloads:
            a, d = re.escape(target), re.escape(data)
            low_level = re.search(rf"\b{a}\s*\.\s*(call|delegatecall)\s*(?:\{{[^{{}}]*\}})?\s*\(\s*{d}\s*[,)]", fn.body)
            if low_level:
                return low_level.group(1), target, data
            helper = re.search(rf"\bfunction(Delegate)?Call\w*\s*\(\s*{a}\s*,\s*{d}\s*[,)]", fn.body)
            if helper:
                return ("delegatecall" if helper.group(1) else "call"), target, data
    return None


def _forward_restrictions(fn: FunctionDef, params: tuple[str, ...]) -> str:
    """The checks in ``fn``'s body or modifiers that read a forwarded parameter, e.g. ``require(s == strategy)``."""
    found: list[str] = []
    for param in params:
        mention = re.compile(rf"\b{re.escape(param)}\b")
        for keyword, start, end in _conditions(fn.body):
            if mention.search(fn.body[start:end]):
                found.append(f"`{keyword}({fn.body[start:end].strip()})`")
        found.extend(f"modifier `{m}`" for m in fn.modifiers if "(" in m and mention.search(m.split("(", 1)[1]))
    return ", ".join(dict.fromkeys(found))


def _arbitrary_call(fn: FunctionDef) -> tuple[str, str]:
    """("call"/"delegatecall", restricting checks) when ``fn`` forwards its calldata to its target, else ("", "").

    The forward counts as arbitrary only when nothing checks the target or the
    calldata; a function that forwards ``report()`` to one strategy is restricted.
    """
    forwarded = _forwarded_call(fn)
    if forwarded is None:
        return "", ""
    kind, target, data = forwarded
    return kind, _forward_restrictions(fn, (target, data))


def _negated(body: str, start: int, end: int) -> bool:
    """Whether the expression at ``start:end`` is negated: ``!x``, ``x == false`` or ``x != true``."""
    before = body[:start].rstrip()
    after = body[end:]
    return before.endswith("!") != bool(re.match(r"\s*(?:==\s*false|!=\s*true)\b", after))


def _passes_when_true(body: str, start: int, end: int) -> bool | None:
    """Whether the expression at ``start:end`` being true lets the caller through.

    Read from the innermost ``require``/``assert`` (true passes) or
    ``if (…) revert`` (true is refused) around it, or a ``return`` (the value
    itself); None when the check's outcome can't be read from the text.
    """
    enclosing = [(kw, s, e) for kw, s, e in _conditions(body) if s <= start and end <= e]
    if enclosing:
        keyword, _, close = max(enclosing, key=lambda c: c[1])
        if keyword != "if":
            truthy = True
        elif _REVERT_AFTER_RE.match(body[close + 1 :]):
            truthy = False
        else:
            return None
    elif re.search(r"\breturn\b[^;]*$", body[:start]):
        truthy = True
    else:
        return None
    return truthy != _negated(body, start, end)


def _agree(polarities: list[bool | None]) -> bool | None:
    """The shared polarity of several checks, None when any is unknown or they disagree."""
    unique = set(polarities)
    return unique.pop() if len(unique) == 1 else None


def _gated_functions(functions: list[FunctionDef], allowlist: str) -> list[GatedFunction]:
    """External entry points that check ``allowlist`` against ``msg.sender``, with each check's polarity."""
    var = re.escape(allowlist)
    reads_sender = re.compile(rf"\b{var}\s*\[\s*{_SENDER}\s*\]|\b{var}\s*\.\s*contains\s*\(\s*{_SENDER}\s*\)")
    reads_any = re.compile(rf"\b{var}\s*\[[^\[\]]*\]|\b{var}\s*\.\s*contains\s*\([^()]*\)")
    # Functions that look an address up in the allowlist — `isExecutor(a)` → `_executors.contains(a)` —
    # and whether the value they return means "listed".
    lookups = {
        fn.name: _agree([_passes_when_true(fn.body, m.start(), m.end()) for m in reads_any.finditer(fn.body)])
        for fn in functions
        if fn.kind == "function" and reads_any.search(fn.body)
    }

    def check(body: str) -> tuple[str, bool | None]:
        inline = list(reads_sender.finditer(body))
        if inline:
            return "inline check", _agree([_passes_when_true(body, m.start(), m.end()) for m in inline])
        for name in sorted(lookups):
            calls = list(re.finditer(rf"\b{re.escape(name)}\s*\(\s*{_SENDER}\s*\)", body))
            if calls:
                # A helper returning "not listed" (`return !blocked[a]`) flips what its call site means.
                listed = lookups[name]
                sites = [_passes_when_true(body, m.start(), m.end()) for m in calls]
                if listed is None or None in sites:
                    return f"helper {name}", None
                return f"helper {name}", _agree([site == listed for site in sites])
        for name in sorted(checkers):
            if re.search(rf"\b{re.escape(name)}\s*\(", body):
                return f"helper {name}", checkers[name]
        return "", None

    # Internal helpers that check the caller themselves: `_checkKeeper()` → `require(keepers[msg.sender])`.
    checkers: dict[str, bool | None] = {}
    for fn in functions:
        if fn.kind == "function" and fn.visibility in ("internal", "private") and reads_sender.search(fn.body):
            checkers[fn.name] = check(fn.body)[1]

    modifiers: dict[str, tuple[str, bool | None]] = {}
    for fn in functions:
        how, allows = check(fn.body) if fn.kind == "modifier" else ("", None)
        if how:
            via = f"modifier {fn.name}" if how == "inline check" else f"modifier {fn.name} ({how})"
            modifiers[fn.name] = (via, allows)
    gated: list[GatedFunction] = []
    seen: set[str] = set()
    for fn in functions:
        if fn.kind not in ("function", "fallback") or fn.visibility not in ("external", "public", ""):
            continue
        if fn.signature in seen:
            continue
        via, allows = next(
            (modifiers[m.split("(")[0]] for m in fn.modifiers if m.split("(")[0] in modifiers), ("", None)
        )
        if not via:
            via, allows = check(fn.body)
        if via:
            seen.add(fn.signature)
            kind, restricted_by = _arbitrary_call(fn)
            gated.append(
                GatedFunction(
                    fn.signature,
                    via,
                    arbitrary_call="" if restricted_by else kind,
                    restricted_by=restricted_by,
                    allows=allows,
                )
            )
    return gated


def _holder_forwarders(chain_id: int, holder: ControllerInfo) -> tuple[str, ...]:
    """Arbitrary-call entry points on a contract holder, with their source modifiers."""
    if holder.kind != "contract":
        return ()
    contract = fetch_verified_contract(chain_id, holder.address)
    found: list[str] = []
    for fn in _functions(contract) if contract else []:
        if fn.kind != "function" or fn.visibility not in ("external", "public"):
            continue
        kind, restricted_by = _arbitrary_call(fn)
        if kind and not restricted_by:
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
        polarity = _agree([fn.allows for fn in gated])
        if not gated or polarity is None:
            # Unchecked, or checks that disagree or can't be read: no claim beats a wrong one.
            continue
        denylist = not polarity
        target = to_checksum_address(target)
        holder = describe_controller(chain_id, client, holder_address)
        return PermissionGrantContext(
            target=target,
            target_label=get_contract_label(chain_id, target),
            signature=call.signature,
            allowlist=allowlist,
            holder=holder,
            enabled=_entry_state(call, denylist),
            gated=tuple(gated),
            holder_forwarders=_holder_forwarders(chain_id, holder),
            holders_before=_enumerated_members(chain_id, client, target, allowlist),
            denylist=denylist,
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
    # Members after the latest call to each (target, allowlist): on-chain reads predate the whole batch.
    members: dict[tuple[str, str], tuple[str, ...] | None] = {}
    for target, call in candidates:
        try:
            context = _resolve_one(chain_id, client, target, call)
        except Exception as error:  # noqa: BLE001 - enrichment must never block an alert
            logger.info("Permission-grant context failed for %s.%s: %s", target, call.function_name, error)
            continue
        if context is None:
            continue
        key = (context.target.lower(), context.allowlist)
        if key in members:
            context = replace(context, holders_before=members[key])
        members[key] = context.holders_after()
        contexts.append(context)
    return contexts


def format_permission_grant_prompt(contexts: list[PermissionGrantContext]) -> str:
    """Render verified allowlist-scope facts for the LLM prompt."""
    return "\n\n".join("\n".join(context.lines()) for context in contexts)


# What the call does to the gated entry points, keyed by the holder's access afterwards.
_EFFECT: dict[bool | None, str] = {True: "opens", False: "closes", None: "controls"}


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
            f"- **Entry points it {_EFFECT[context.access]}:**",
        ]
        for fn in context.gated:
            flag = f" — **arbitrary {fn.arbitrary_call}**" if fn.arbitrary_call else ""
            if fn.restricted_by:
                flag = f" — forwards calls, restricted by {fn.restricted_by}"
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

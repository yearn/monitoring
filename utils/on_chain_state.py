"""Read the current on-chain value of state variables a setter will modify.

For setter calls like ``setMaxSlippage(0.99e18)`` or ``setCoverageCap(agent, cap)``,
fetch what the value is *right now* so the LLM can reason about before→after
deltas instead of just seeing the new value.

v1 scope:
- Simple state vars (uint*, int*, address, bool, bytes*, string).
- Single-key mappings where the setter's args include the mapping key type
  (e.g., ``mapping(address => uint256) public coverageCap`` + ``setCoverageCap(address, uint256)``).
- Skips: nested mappings, arrays, struct-valued mappings, anything else.
"""

import re
from dataclasses import dataclass
from typing import Any

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from utils.calldata.decoder import DecodedCall
from utils.chains import Chain
from utils.logger import get_logger
from utils.source_context import (
    extract_state_var_snippet,
    fetch_source,
    find_state_var_writes,
)
from utils.web3_wrapper import ChainManager

logger = get_logger("utils.on_chain_state")

# Simple Solidity value types whose auto-generated getter takes no args.
_SIMPLE_VALUE_TYPES = frozenset(
    {
        "address",
        "bool",
        "string",
        "bytes",
    }
)


def _is_simple_uint(type_str: str) -> bool:
    return bool(
        re.fullmatch(
            r"u?int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?",
            type_str,
        )
    )


def _is_simple_bytes(type_str: str) -> bool:
    return bool(re.fullmatch(r"bytes(?:[1-9]|[12][0-9]|3[0-2])", type_str))


def _is_simple_type(type_str: str) -> bool:
    return type_str in _SIMPLE_VALUE_TYPES or _is_simple_uint(type_str) or _is_simple_bytes(type_str)


@dataclass(frozen=True)
class StateRead:
    """A single var-name → current-value pair read from chain."""

    var_name: str
    type_str: str  # e.g. "uint256", "mapping(address => uint256)"
    value: Any  # raw decoded value
    key_args: tuple[Any, ...] = ()  # for mapping reads, the key(s) used


def _parse_var_declaration(snippet: str, var_name: str) -> tuple[str, list[str]] | None:
    """From a state-var snippet, return (value_type, mapping_key_types).

    For ``uint256 public maxSlippage`` returns ``("uint256", [])``.
    For ``mapping(address => uint256) public coverageCap`` returns ``("uint256", ["address"])``.
    Returns None for unsupported shapes (nested mapping, struct, array).
    """
    # Strip natspec lines to isolate the declaration line
    decl_lines = [line for line in snippet.splitlines() if not line.strip().startswith(("///", "*", "/**"))]
    decl = " ".join(line.strip() for line in decl_lines).strip()
    decl = decl.rstrip(";")

    # Mapping case: mapping(K => V) public name
    m = re.match(r"mapping\s*\(\s*(\w+)\s*=>\s*(.+?)\s*\)\s+(?:public|external)\s+\w+\s*$", decl)
    if m:
        key_type = m.group(1).strip()
        value_type = m.group(2).strip()
        if "mapping" in value_type or "[" in value_type:
            return None  # nested mapping or array value — skip
        if not _is_simple_type(value_type):
            return None  # struct or unknown — skip
        return (value_type, [key_type])

    # Simple type case: <type> public <name>
    m = re.match(r"(\w+)(?:\s+(?:public|external|immutable|constant))+\s+\w+\s*$", decl)
    if m:
        value_type = m.group(1).strip()
        if _is_simple_type(value_type):
            return (value_type, [])
        return None

    return None


def _is_externally_readable(snippet: str) -> bool:
    """Whether a state-var declaration exposes a compiler-generated getter.

    Only ``public`` (and ``external``) state variables get an auto-generated
    getter callable via eth_call. ``internal``/``private`` vars have no getter,
    so a read always reverts (e.g. Compound's Configurator declares
    ``mapping(address => Configuration) internal configuratorParams``).
    """
    decl_lines = [line for line in snippet.splitlines() if not line.strip().startswith(("///", "*", "/**"))]
    decl = " ".join(line.strip() for line in decl_lines)
    return bool(re.search(r"\b(?:public|external)\b", decl))


def _match_key_value_from_params(decoded_call: DecodedCall, key_type: str) -> Any | None:
    """Pick the first call param whose type matches the mapping key type.

    Heuristic: many setters take the mapping key as a leading argument
    (e.g., ``setCoverageCap(address agent, uint256 cap)`` for
    ``mapping(address => uint256) public coverageCap``). Returns None if no
    arg matches or if the matching arg is an array.
    """
    for type_str, value in decoded_call.params:
        if type_str.endswith("[]"):
            continue  # arrays not handled in v1
        if type_str == key_type:
            return value
        # Allow uint variants to match (uint256 keys are common)
        if key_type == "uint256" and _is_simple_uint(type_str):
            return value
    return None


def _call_getter(
    chain_id: int,
    contract_address: str,
    var_name: str,
    value_type: str,
    key_types: list[str],
    key_values: list[Any],
) -> Any | None:
    """Build calldata for the auto-generated getter and decode the response."""
    sig = f"{var_name}({','.join(key_types)})"
    selector = function_signature_to_4byte_selector(sig)
    try:
        encoded_args = abi_encode(key_types, key_values) if key_types else b""
    except Exception as e:  # noqa: BLE001 - skip on invalid args (e.g. non-checksum address)
        logger.info("Could not encode args for %s.%s: %s", contract_address, var_name, e)
        return None
    calldata = "0x" + (selector + encoded_args).hex()

    try:
        chain = Chain.from_chain_id(chain_id)
    except ValueError:
        return None

    try:
        to_addr = to_checksum_address(contract_address)
    except Exception as e:  # noqa: BLE001
        logger.info("Invalid address %s: %s", contract_address, e)
        return None

    try:
        client = ChainManager.get_client(chain)
        raw = client.eth.call({"to": to_addr, "data": calldata})
    except Exception as e:  # noqa: BLE001 - eth_call failures are expected (no getter, revert)
        logger.info("eth_call for %s.%s failed: %s", contract_address, var_name, e)
        return None

    if not raw:
        return None
    try:
        decoded = abi_decode([value_type], bytes(raw))
        return decoded[0]
    except Exception as e:  # noqa: BLE001
        logger.info("Could not decode %s as %s: %s", raw.hex(), value_type, e)
        return None


def _guess_getter_from_setter(decoded_call: DecodedCall) -> tuple[str, list[str], list[Any]] | None:
    """Fallback for diamond-storage or non-public-var setters.

    When we can't find an explicit ``<type> public <name>;`` declaration for the
    var being written (e.g., it lives inside a storage struct), guess that the
    contract exposes a custom getter ``<name>(<keys>) returns (<value>)`` whose
    signature mirrors the setter's args: last arg = value type, others = key types.
    """
    if not decoded_call.params:
        return None

    *key_params, value_param = decoded_call.params
    all_params = key_params + [value_param]
    if any(t.endswith("[]") for t, _ in all_params):
        return None

    value_type = value_param[0]
    if not _is_simple_type(value_type):
        return None

    return value_type, [t for t, _ in key_params], [v for _, v in key_params]


def _resolve_source_for_function(chain_id: int, target: str, function_name: str) -> str | None:
    """Return the source where `function_name` is defined, following the proxy if needed."""
    fetched = fetch_source(chain_id, target)
    if fetched and find_state_var_writes(fetched[1], function_name):
        return fetched[1]

    from utils.proxy import get_current_implementation

    impl = get_current_implementation(target, chain_id)
    if not impl or impl.lower() == target.lower():
        return fetched[1] if fetched else None

    fetched_impl = fetch_source(chain_id, impl)
    return fetched_impl[1] if fetched_impl else (fetched[1] if fetched else None)


def read_before_state(
    chain_id: int,
    target: str,
    decoded_call: DecodedCall,
) -> list[StateRead]:
    """Read current values of state vars the function will write.

    Best-effort: returns [] on any failure (no source, no key args, no getter).
    For proxy targets, follows the EIP-1967 implementation slot to find where
    `function_name` is actually defined, but issues eth_calls against the
    original target (which holds the storage).
    """
    if not target or not decoded_call.function_name:
        return []

    source = _resolve_source_for_function(chain_id, target, decoded_call.function_name)
    if not source:
        return []

    var_names = find_state_var_writes(source, decoded_call.function_name)
    if not var_names:
        return []

    reads: list[StateRead] = []
    for var_name in var_names:
        snippet = extract_state_var_snippet(source, var_name)
        if snippet and not _is_externally_readable(snippet):
            # The var is declared at top level but internal/private, so no
            # compiler getter exists and a read would always revert. Skip it
            # rather than guessing a getter from the setter signature.
            logger.info("Skipping non-public state var %s.%s", target, var_name)
            continue
        parsed = _parse_var_declaration(snippet, var_name) if snippet else None

        key_values: list[Any] = []
        if parsed:
            value_type, key_types = parsed
            if key_types:
                for key_type in key_types:
                    matched = _match_key_value_from_params(decoded_call, key_type)
                    if matched is None:
                        key_values = []
                        break
                    key_values.append(matched)
                if not key_values:
                    continue
        else:
            # Diamond-storage / non-public-var fallback: guess the getter from the
            # setter signature (last arg = value, leading args = keys).
            guessed = _guess_getter_from_setter(decoded_call)
            if not guessed:
                continue
            value_type, key_types, key_values = guessed

        value = _call_getter(chain_id, target, var_name, value_type, key_types, key_values)
        if value is None:
            continue

        reads.append(
            StateRead(
                var_name=var_name,
                type_str=value_type if not key_types else f"mapping({key_types[0]} => {value_type})",
                value=value,
                key_args=tuple(key_values),
            )
        )

    return reads


def _fmt_value(v: Any) -> str:
    """Render a value for the prompt: bytes as hex, others stringified."""
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    return str(v)


def format_state_reads(reads: list[StateRead]) -> str:
    """Format StateRead list for prompt injection."""
    if not reads:
        return ""
    lines: list[str] = []
    for r in reads:
        value = _fmt_value(r.value)
        if r.key_args:
            keys = ", ".join(_fmt_value(k) for k in r.key_args)
            lines.append(f"  {r.var_name}({keys}) = {value}  // current value, type: {r.type_str}")
        else:
            lines.append(f"  {r.var_name} = {value}  // current value, type: {r.type_str}")
    return "\n".join(lines)

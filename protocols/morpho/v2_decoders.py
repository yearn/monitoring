"""Decode Morpho VaultV2 / adapter Submit calldata into human-readable strings.

Morpho V2 timelocks are keyed by raw calldata (`executableAt[bytes data]`), so the
only practical way to surface pending changes is to listen to `Submit` events and
decode the embedded calldata. This module provides:

* ``SELECTOR_TO_SIG`` — selector hex → solidity signature for every timelocked
  function on VaultV2 and on MorphoMarketV1AdapterV2.
* ``decode_submit(data)`` — turn raw calldata bytes into a friendly string.
* ``decode_id_data(idData)`` — decode the ``bytes idData`` argument of cap
  setters; this is how we recover the underlying Morpho Blue marketId / token /
  adapter being capped.
"""

from typing import Any, Optional

from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address
from web3 import Web3

from utils.calldata.decoder import resolve_selector
from utils.logging import get_logger

logger = get_logger("morpho.v2_decoders")

WAD = 10**18

# Timelocked selectors on VaultV2 (per VaultV2.sol). All require curator submit.
_VAULT_V2_SIGS: list[str] = [
    "setIsAllocator(address,bool)",
    "setReceiveSharesGate(address)",
    "setSendSharesGate(address)",
    "setReceiveAssetsGate(address)",
    "setSendAssetsGate(address)",
    "setAdapterRegistry(address)",
    "addAdapter(address)",
    "removeAdapter(address)",
    "increaseTimelock(bytes4,uint256)",
    "decreaseTimelock(bytes4,uint256)",
    "abdicate(bytes4)",
    "setPerformanceFee(uint256)",
    "setManagementFee(uint256)",
    "setPerformanceFeeRecipient(address)",
    "setManagementFeeRecipient(address)",
    "increaseAbsoluteCap(bytes,uint256)",
    "increaseRelativeCap(bytes,uint256)",
    "setForceDeallocatePenalty(address,uint256)",
]

# Timelocked selectors on MorphoMarketV1AdapterV2.
_ADAPTER_SIGS: list[str] = [
    "setSkimRecipient(address)",
    "burnShares(bytes32)",
    "increaseTimelock(bytes4,uint256)",
    "decreaseTimelock(bytes4,uint256)",
    "abdicate(bytes4)",
]


def _build_selector_map(sigs: list[str]) -> dict[str, str]:
    return {bytes(Web3.keccak(text=sig)[:4]).hex().lower(): sig for sig in sigs}


# Lower-case hex selector (no 0x prefix) → signature.
SELECTOR_TO_SIG: dict[str, str] = {**_build_selector_map(_VAULT_V2_SIGS), **_build_selector_map(_ADAPTER_SIGS)}


def _selector_hex(data: bytes) -> str:
    return bytes(data[:4]).hex().lower()


def _function_name(sig: str) -> str:
    return sig.split("(", 1)[0]


def _resolve_inner_selector(selector_bytes: bytes) -> str:
    """Look up a 4-byte selector argument, e.g. for increaseTimelock/abdicate."""
    sel_hex = bytes(selector_bytes).hex().lower()
    sig = SELECTOR_TO_SIG.get(sel_hex)
    if sig:
        return _function_name(sig)
    fallback = resolve_selector("0x" + sel_hex)
    return _function_name(fallback) if fallback else f"0x{sel_hex}"


def _format_address(addr: str) -> str:
    return to_checksum_address(addr)


def _format_wad_pct(value: int) -> str:
    return f"{value / WAD * 100:.4f}%"


def decode_id_data(id_data: bytes) -> str:
    """Decode a cap-setter ``bytes idData`` argument.

    The Morpho V2 cap system uses three id-tag prefixes, ABI-encoded as a leading
    string:

    * ``"this"`` → ``abi.encode(string,address)`` → adapter id
    * ``"collateralToken"`` → ``abi.encode(string,address)`` → collateral token
    * ``"this/marketParams"`` → ``abi.encode(string,address,(address,address,address,address,uint256))``
      → adapter + MarketParams
    """
    if not id_data:
        return "<empty idData>"

    # Try the (string, address, MarketParams) form first because it's the most
    # specific. If decoding fails, fall back to (string, address).
    market_params_type = "(address,address,address,address,uint256)"
    try:
        tag, adapter, market_params = abi_decode(["string", "address", market_params_type], id_data)
    except Exception:
        tag = None
    else:
        if tag == "this/marketParams":
            loan, collateral, oracle, irm, lltv = market_params
            market_id_hex = bytes(Web3.keccak(_encode_market_params(loan, collateral, oracle, irm, lltv))).hex()
            return (
                f"market `0x{market_id_hex}` "
                f"(loan {_format_address(loan)}, collateral {_format_address(collateral)}, "
                f"lltv {lltv / WAD * 100:.2f}%) on adapter {_format_address(adapter)}"
            )

    try:
        tag, addr = abi_decode(["string", "address"], id_data)
    except Exception:
        return f"<unparseable idData 0x{id_data.hex()}>"

    if tag == "this":
        return f"adapterId for adapter {_format_address(addr)}"
    if tag == "collateralToken":
        return f"collateral token {_format_address(addr)}"
    return f"id tag '{tag}' addr {_format_address(addr)}"


def _encode_market_params(loan: str, collateral: str, oracle: str, irm: str, lltv: int) -> bytes:
    """Re-encode MarketParams in the canonical Morpho Blue form for hashing."""
    from eth_abi import encode as abi_encode

    return abi_encode(
        ["address", "address", "address", "address", "uint256"],
        [loan, collateral, oracle, irm, lltv],
    )


def _format_args(sig: str, args: tuple[Any, ...]) -> str:  # noqa: PLR0911,PLR0912
    """Render a decoded argument tuple per signature."""
    name = _function_name(sig)

    if name in ("addAdapter", "removeAdapter"):
        return f"adapter {_format_address(args[0])}"
    if name == "setIsAllocator":
        addr, flag = args
        return f"allocator {_format_address(addr)} = {bool(flag)}"
    if name in (
        "setReceiveSharesGate",
        "setSendSharesGate",
        "setReceiveAssetsGate",
        "setSendAssetsGate",
        "setAdapterRegistry",
    ):
        addr = args[0]
        zero = "0x" + "00" * 20
        if addr.lower() == zero:
            return "disable (0x0)"
        return _format_address(addr)
    if name in ("increaseAbsoluteCap", "increaseRelativeCap"):
        id_data, new_cap = args
        return f"{decode_id_data(id_data)} → cap {new_cap}"
    if name in ("increaseTimelock", "decreaseTimelock"):
        sel_bytes, duration = args
        return f"{_resolve_inner_selector(sel_bytes)} → {duration}s"
    if name == "abdicate":
        return f"{_resolve_inner_selector(args[0])} (irreversibly disabled)"
    if name in ("setPerformanceFee", "setManagementFee"):
        return f"new fee {_format_wad_pct(args[0])}"
    if name in ("setPerformanceFeeRecipient", "setManagementFeeRecipient"):
        return f"recipient {_format_address(args[0])}"
    if name == "setForceDeallocatePenalty":
        adapter, penalty = args
        return f"adapter {_format_address(adapter)} → penalty {_format_wad_pct(penalty)}"
    if name == "setSkimRecipient":
        return f"recipient {_format_address(args[0])}"
    if name == "burnShares":
        market_id = args[0]
        if isinstance(market_id, (bytes, bytearray)):
            market_id = "0x" + market_id.hex()
        return f"market {market_id}"
    return ", ".join(repr(a) for a in args)


def _arg_types(sig: str) -> list[str]:
    """Extract the argument list from a solidity signature."""
    inner = sig[sig.index("(") + 1 : sig.rindex(")")]
    if not inner:
        return []
    return [t.strip() for t in inner.split(",")]


def decode_submit(data: bytes) -> str:
    """Render a Submit/Accept/Revoke ``data`` payload as a function call string.

    Falls back to a Sourcify 4byte lookup for unknown selectors so we never
    crash on novel selectors.
    """
    if len(data) < 4:
        return f"<malformed data 0x{data.hex()}>"
    sel = _selector_hex(data)
    sig = SELECTOR_TO_SIG.get(sel)
    if sig is None:
        # Unknown selector — try the global 4byte database, but we won't be able
        # to ABI-decode the args without the signature, so return just the name.
        fallback = resolve_selector("0x" + sel)
        if fallback:
            return f"{_function_name(fallback)}(<undecoded args 0x{data[4:].hex()}>)"
        return f"<unknown selector 0x{sel}> data 0x{data.hex()}"

    types = _arg_types(sig)
    try:
        args = abi_decode(types, data[4:])
    except Exception as e:
        logger.warning("Failed to ABI-decode %s payload: %s", sig, e)
        return f"{_function_name(sig)}(<undecoded args 0x{data[4:].hex()}>)"

    return f"{_function_name(sig)}({_format_args(sig, args)})"


def submit_data_key(data: bytes) -> str:
    """Stable cache key derived from Submit ``data`` (keccak hex, no 0x prefix)."""
    return bytes(Web3.keccak(data)).hex().lower()


def selector_function_name(selector_bytes: bytes) -> Optional[str]:
    """Reverse-resolve a 4-byte selector to its function name, or None."""
    sig = SELECTOR_TO_SIG.get(bytes(selector_bytes).hex().lower())
    return _function_name(sig) if sig else None

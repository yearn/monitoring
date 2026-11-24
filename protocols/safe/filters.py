"""Per-protocol rules for which queued Safe transactions are worth an alert.

Most watched Safes alert on every queued transaction. A few protocols only
care about a narrow slice: Euler about txs to the vault we use, Pendle about
privileged calls (upgrades and ownership changes). Those rules live here so the
monitor loop stays free of protocol branches.

The Pendle rule reads decoded calls, never substrings: a selector's bytes can
appear inside any argument. It fails open: calldata it cannot fully inspect
(a malformed batch, a wrapper nested deeper than it looks, a DELEGATECALL into
an unknown contract) alerts instead of being skipped.
"""

from collections.abc import Callable

from protocols.safe.multisend import decode_multisend, is_multisend_call
from utils.calldata.wrappers import is_wrapper_call, try_unwrap_calls
from utils.logger import get_logger
from utils.proxy import MAX_WRAPPER_DEPTH

logger = get_logger("safe.filters")

# Euler: only the vault our strategies use.
EULER_WATCHED_TARGETS = frozenset({"0x797dd80692c3b2dadabce8e30c07fde5307d48a9"})

# Calls that change who controls a contract or what code it runs.
PRIVILEGED_SELECTORS: dict[str, str] = {
    "0x3659cfe6": "upgradeTo(address)",
    "0x4f1ef286": "upgradeToAndCall(address,bytes)",
    "0x9623609d": "upgradeAndCall(address,address,bytes)",  # ProxyAdmin
    "0x99a88ec4": "upgrade(address,address)",  # ProxyAdmin (v4)
    "0x7eff275e": "changeProxyAdmin(address,address)",  # ProxyAdmin (v4)
    "0xf2fde38b": "transferOwnership(address)",
    "0x1f931c1c": "diamondCut((address,uint8,bytes4[])[],address,bytes)",  # EIP-2535
}

# (tx, target, hex_data) -> whether to alert
SafeTxFilter = Callable[[dict, str, str], bool]


def _call_is_privileged(data_hex: str, depth: int) -> bool:
    """Whether a call, or any call nested in a supported wrapper, is privileged.

    Args:
        data_hex: 0x-prefixed calldata.
        depth: Wrapper levels already unwrapped.

    Returns:
        True for a privileged selector at any level, and for wrappers that
        cannot be inspected (malformed, or nested past ``MAX_WRAPPER_DEPTH``).
    """
    if data_hex[:10].lower() in PRIVILEGED_SELECTORS:
        return True
    if not is_wrapper_call(data_hex):
        return False
    if depth >= MAX_WRAPPER_DEPTH:
        logger.warning("Wrapper nested deeper than %s levels; treating as privileged", MAX_WRAPPER_DEPTH)
        return True
    inner_calls = try_unwrap_calls(data_hex)
    if inner_calls is None:
        logger.warning("Wrapper calldata %s… does not decode; treating as privileged", data_hex[:10])
        return True
    # A well-formed empty batch (e.g. Pendle aggregate([])) executes nothing.
    return any(_call_is_privileged(inner.data, depth + 1) for inner in inner_calls)


def has_privileged_call(tx: dict, hex_data: str) -> bool:
    """Whether a Safe tx performs a privileged call, directly, in a multisend, or in a wrapper.

    Args:
        tx: Safe Queue Service multisig transaction.
        hex_data: The tx's calldata, already normalized to ``"0x"`` when empty.

    Returns:
        True when the tx should be treated as privileged.
    """
    if int(tx.get("operation", 0) or 0) == 1:
        if not is_multisend_call(tx):
            # A DELEGATECALL into anything but a multisend utility runs arbitrary code as the Safe.
            return True
        try:
            inner_calls = decode_multisend(hex_data)
        except ValueError as e:
            logger.warning("Multisend payload does not decode (%s); treating as privileged", e)
            return True
        # Legacy MultiSend allows inner DELEGATECALLs, which run arbitrary code as the Safe too.
        return any(call["operation"] == "1" or _call_is_privileged(call["data"], 0) for call in inner_calls)
    return _call_is_privileged(hex_data, 0)


def _euler_filter(_tx: dict, target: str, _hex_data: str) -> bool:
    return target.lower() in EULER_WATCHED_TARGETS


def _pendle_filter(tx: dict, _target: str, hex_data: str) -> bool:
    return has_privileged_call(tx, hex_data)


PROTOCOL_FILTERS: dict[str, SafeTxFilter] = {
    "EULER": _euler_filter,
    "PENDLE": _pendle_filter,
}


def should_alert(protocol: str, tx: dict, target: str, hex_data: str) -> bool:
    """Whether a queued tx on a ``protocol`` Safe is worth an alert.

    Args:
        protocol: Protocol key from the watchlist.
        tx: Safe Queue Service multisig transaction.
        target: The tx's destination address.
        hex_data: The tx's calldata, already normalized to ``"0x"`` when empty.

    Returns:
        True when the protocol has no rule, or its rule accepts the tx.
    """
    rule = PROTOCOL_FILTERS.get(protocol)
    return rule is None or rule(tx, target, hex_data)

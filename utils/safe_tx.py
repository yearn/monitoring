"""Helpers for inspecting Gnosis Safe transactions.

The Safe contract wraps every batch via ``execTransaction(to, value, data, ...)``,
so when a Safe schedules a Maple timelock proposal (or any other on-chain
action) the actual call payload lives one layer deep, inside the ``data``
parameter of that outer call.
"""

from dataclasses import dataclass

from utils.calldata.decoder import decode_calldata


@dataclass(frozen=True)
class InnerCall:
    """The inner call wrapped by a Safe ``execTransaction``."""

    target: str
    value: int
    data: str  # hex with 0x prefix
    operation: int  # 0 = CALL, 1 = DELEGATECALL


_EXEC_TRANSACTION_SELECTOR = "0x6a761202"


def unwrap_safe_exec_transaction(input_hex: str) -> InnerCall | None:
    """Decode a Safe ``execTransaction`` call into its inner (target, value, data, operation).

    Returns None if ``input_hex`` is not an ``execTransaction`` payload.
    """
    if not input_hex or len(input_hex) < 10:
        return None
    if input_hex[:10].lower() != _EXEC_TRANSACTION_SELECTOR:
        return None

    decoded = decode_calldata(input_hex)
    if not decoded or len(decoded.params) < 4:
        return None

    target = decoded.params[0][1]
    value = int(decoded.params[1][1])
    data = decoded.params[2][1]
    operation = int(decoded.params[3][1])

    if isinstance(data, bytes):
        data = "0x" + data.hex()
    elif isinstance(data, str) and not data.startswith("0x"):
        data = "0x" + data

    return InnerCall(target=target, value=value, data=data, operation=operation)

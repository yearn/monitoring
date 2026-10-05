"""Read one view function with a raw ``eth_call``, without building a contract ABI."""

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from utils.web3_wrapper import Web3Client


def call_view(client: Web3Client, address: str, signature: str, output: str, args: tuple = ()) -> object | None:
    """Call ``signature`` on ``address`` and decode a single ``output`` value.

    Args:
        client: Chain client to call through.
        address: Contract to call.
        signature: Function signature, e.g. ``"getTimestamp(bytes32)"``.
        output: ABI type of the single return value.
        args: Arguments matching the signature's parameter types.

    Returns:
        The decoded value, or None when the call reverts, the getter is missing
        or the result doesn't decode — absent getters are expected when probing.
    """
    types = signature[signature.index("(") + 1 : -1]
    try:
        data = function_signature_to_4byte_selector(signature)
        if args:
            data += abi_encode(types.split(","), list(args))
        raw = client.eth.call({"to": to_checksum_address(address), "data": "0x" + data.hex()})
        return abi_decode([output], bytes(raw))[0] if raw else None
    except Exception:  # noqa: BLE001 - absent getters and reverts are expected
        return None

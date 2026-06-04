import json
from typing import List, Tuple

from eth_abi import decode
from eth_utils import to_checksum_address
from web3 import Web3

from utils.logging import get_logger

logger = get_logger("safe.specific")


class Call:
    def __init__(self, target: str, value: int, call_data: bytes):
        self.target = target
        self.value = value
        self.call_data = call_data

    def __str__(self):
        return f"Call(target={self.target}, value={self.value}, call_data={self.call_data.hex()})"


def parse_input_parameters(hex_data: str) -> List[Call]:
    # Remove '0x' prefix if present
    hex_data = hex_data.removeprefix("0x")
    # Convert hex string to bytes
    data = bytes.fromhex(hex_data)
    # Decode the array of Call structs
    decoded = decode(["(address,uint256,bytes)[]"], data)[0]

    calls = []
    for call_tuple in decoded:
        target, value, call_data = call_tuple
        calls.append(Call(to_checksum_address(target), value, call_data))
    return calls


def get_contract_name(w3: Web3, address: str) -> str:
    # ABI for the name() function
    name_abi = [
        {
            "constant": True,
            "inputs": [],
            "name": "name",
            "outputs": [{"name": "", "type": "string"}],
            "type": "function",
        }
    ]

    contract = w3.eth.contract(address=address, abi=name_abi)
    try:
        name = contract.functions.name().call()
        return name
    except Exception:
        return f"Unknown Contract at {address}"


def get_function_selector_and_params(call_data: bytes) -> Tuple[str, dict]:
    function_selector = call_data[:4].hex()
    params = {"raw_data": call_data[4:].hex()}
    return function_selector, params


def handle_pendle(provider_url, hex_encoded_data: str) -> str:
    # Set up Web3 connection (replace with your own Ethereum node URL)
    w3 = Web3(Web3.HTTPProvider(provider_url))

    aggreate_function_signature = "0x1acaa198"
    # Remove '0x' prefix if present
    if not hex_encoded_data.startswith(aggreate_function_signature):
        logger.warning("Invalid input data")
        return ""

    hex_encoded_data = hex_encoded_data.removeprefix(aggreate_function_signature)
    parsed_calls = parse_input_parameters(hex_encoded_data)

    number_of_calls = len(parsed_calls)
    if number_of_calls == 0:
        logger.warning("No function calls found")
        return ""

    message = "-" * 20
    message += f"\nAggregate {number_of_calls} function calls"

    for i, call in enumerate(parsed_calls):
        message += f"\nCall {i + 1}:"
        message += f"\nTarget: {call.target}"
        contract_name = get_contract_name(w3, call.target)
        message += f"\nContract Name: {contract_name}"
        message += f"\nValue: {call.value}"
        function_selector, params = get_function_selector_and_params(call.call_data)
        message += f"\nFunction Selector: {function_selector}"
        message += f"\nDecoded Parameters: {json.dumps(params, indent=2)}"
    return message


# if __name__ == "__main__":
#     # Example usage
#     hex_encoded_data = "0x1acaa198000000000000000000000000000000000000000000000000000000000000002000000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000020000000000000000000000000ae754a3b4553ea2ea4794d0171a56ac12fdb27820000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000006000000000000000000000000000000000000000000000000000000000000000043f4ba83a00000000000000000000000000000000000000000000000000000000"
#     handle_pendle(hex_encoded_data)

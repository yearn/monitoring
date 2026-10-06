"""Tests for utils/revert_decoder.py."""

import unittest

from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector

from utils.revert_decoder import decode_revert

REUSD = "0x5086bf358635B81D8C47C66d1C8b9E567Db70c72"
ABI = [
    {"type": "error", "name": "InvalidOracle", "inputs": [{"name": "_asset", "type": "address"}]},
    {
        "type": "error",
        "name": "InvalidSlippage",
        "inputs": [{"name": "_configuredSlipage", "type": "uint256"}, {"name": "_maxSlippage", "type": "uint256"}],
    },
    {"type": "error", "name": "SwapCooldown", "inputs": []},
    {"type": "function", "name": "price", "inputs": [], "outputs": []},
]


def _payload(signature: str, types: list[str], values: list[object]) -> str:
    return "0x" + function_signature_to_4byte_selector(signature).hex() + encode(types, values).hex()


class TestDecodeRevert(unittest.TestCase):
    def test_custom_error_with_named_argument(self) -> None:
        """The SwapFarmV2.enableAssets revert Tenderly reported as a bare 'reverted'."""
        data = "0x1f9360170000000000000000000000005086bf358635b81d8c47c66d1c8b9e567db70c72"
        self.assertEqual(decode_revert(data, ABI), f"InvalidOracle(_asset={REUSD})")

    def test_custom_error_with_several_arguments_and_none(self) -> None:
        data = _payload("InvalidSlippage(uint256,uint256)", ["uint256", "uint256"], [1, 2])
        self.assertEqual(decode_revert(data, ABI), "InvalidSlippage(_configuredSlipage=1, _maxSlippage=2)")
        self.assertEqual(decode_revert(_payload("SwapCooldown()", [], []), ABI), "SwapCooldown()")

    def test_require_string_and_panic_need_no_abi(self) -> None:
        self.assertEqual(
            decode_revert(_payload("Error(string)", ["string"], ["not allowed"])), 'require: "not allowed"'
        )
        self.assertEqual(
            decode_revert(_payload("Panic(uint256)", ["uint256"], [0x11])), "Panic(0x11: arithmetic overflow/underflow)"
        )

    def test_unknown_or_malformed_payloads_decode_to_empty(self) -> None:
        self.assertEqual(decode_revert("0xdeadbeef", ABI), "")
        self.assertEqual(decode_revert("", ABI), "")
        self.assertEqual(decode_revert("0x1f936017", ABI), "")  # selector without its argument
        self.assertEqual(decode_revert("not-hex", ABI), "")


if __name__ == "__main__":
    unittest.main()

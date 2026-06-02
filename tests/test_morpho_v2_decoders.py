"""Golden tests for morpho/v2_decoders.py.

These hermetic tests synthesize Submit calldata payloads with ``eth_abi.encode``
and assert that ``decode_submit`` produces a sensible human-readable string for
each timelocked selector on VaultV2 and MorphoMarketV1AdapterV2.
"""

import unittest

from eth_abi import encode as abi_encode
from web3 import Web3

from morpho.v2_decoders import (
    SELECTOR_TO_SIG,
    decode_id_data,
    decode_submit,
    selector_function_name,
    submit_data_key,
)

ZERO_ADDR = "0x" + "00" * 20
A1 = "0x" + "11" * 20
A2 = "0x" + "22" * 20
A3 = "0x" + "33" * 20
A4 = "0x" + "44" * 20
A5 = "0x" + "55" * 20


def _selector(sig: str) -> bytes:
    return bytes(Web3.keccak(text=sig)[:4])


def _build(sig: str, types: list[str], values: list) -> bytes:
    return _selector(sig) + abi_encode(types, values)


class TestSelectorMap(unittest.TestCase):
    def test_map_contains_all_v2_selectors(self):
        # Spot-check: addAdapter/removeAdapter/abdicate/decreaseTimelock should
        # all be reverse-resolvable.
        for sig in [
            "addAdapter(address)",
            "removeAdapter(address)",
            "setIsAllocator(address,bool)",
            "increaseTimelock(bytes4,uint256)",
            "decreaseTimelock(bytes4,uint256)",
            "abdicate(bytes4)",
            "setPerformanceFee(uint256)",
            "increaseAbsoluteCap(bytes,uint256)",
            "increaseRelativeCap(bytes,uint256)",
            "burnShares(bytes32)",
        ]:
            self.assertIn(_selector(sig).hex(), SELECTOR_TO_SIG)
            self.assertEqual(selector_function_name(_selector(sig)), sig.split("(", 1)[0])


class TestDecodeSubmit(unittest.TestCase):
    def test_add_adapter(self):
        data = _build("addAdapter(address)", ["address"], [A1])
        self.assertEqual(
            decode_submit(data),
            f"addAdapter(adapter {Web3.to_checksum_address(A1)})",
        )

    def test_remove_adapter(self):
        data = _build("removeAdapter(address)", ["address"], [A2])
        self.assertEqual(
            decode_submit(data),
            f"removeAdapter(adapter {Web3.to_checksum_address(A2)})",
        )

    def test_set_is_allocator_grant(self):
        data = _build("setIsAllocator(address,bool)", ["address", "bool"], [A1, True])
        self.assertEqual(
            decode_submit(data),
            f"setIsAllocator(allocator {Web3.to_checksum_address(A1)} = True)",
        )

    def test_set_is_allocator_revoke(self):
        data = _build("setIsAllocator(address,bool)", ["address", "bool"], [A2, False])
        self.assertIn("False", decode_submit(data))

    def test_set_receive_assets_gate_disable(self):
        data = _build("setReceiveAssetsGate(address)", ["address"], [ZERO_ADDR])
        self.assertEqual(decode_submit(data), "setReceiveAssetsGate(disable (0x0))")

    def test_set_receive_assets_gate_enable(self):
        data = _build("setReceiveAssetsGate(address)", ["address"], [A3])
        self.assertEqual(
            decode_submit(data),
            f"setReceiveAssetsGate({Web3.to_checksum_address(A3)})",
        )

    def test_increase_timelock_known_inner_selector(self):
        inner = _selector("addAdapter(address)")
        data = _build("increaseTimelock(bytes4,uint256)", ["bytes4", "uint256"], [inner, 86400])
        self.assertEqual(decode_submit(data), "increaseTimelock(addAdapter → 86400s)")

    def test_decrease_timelock(self):
        inner = _selector("setIsAllocator(address,bool)")
        data = _build("decreaseTimelock(bytes4,uint256)", ["bytes4", "uint256"], [inner, 0])
        self.assertEqual(decode_submit(data), "decreaseTimelock(setIsAllocator → 0s)")

    def test_abdicate(self):
        inner = _selector("setPerformanceFee(uint256)")
        data = _build("abdicate(bytes4)", ["bytes4"], [inner])
        self.assertEqual(
            decode_submit(data),
            "abdicate(setPerformanceFee (irreversibly disabled))",
        )

    def test_set_performance_fee(self):
        # 10% in WAD
        data = _build("setPerformanceFee(uint256)", ["uint256"], [10**17])
        self.assertEqual(decode_submit(data), "setPerformanceFee(new fee 10.0000%)")

    def test_set_performance_fee_recipient(self):
        data = _build("setPerformanceFeeRecipient(address)", ["address"], [A4])
        self.assertEqual(
            decode_submit(data),
            f"setPerformanceFeeRecipient(recipient {Web3.to_checksum_address(A4)})",
        )

    def test_set_force_deallocate_penalty(self):
        # 0.5% in WAD
        data = _build(
            "setForceDeallocatePenalty(address,uint256)",
            ["address", "uint256"],
            [A1, 5 * 10**15],
        )
        self.assertEqual(
            decode_submit(data),
            f"setForceDeallocatePenalty(adapter {Web3.to_checksum_address(A1)} → penalty 0.5000%)",
        )

    def test_burn_shares(self):
        market_id = bytes.fromhex("ab" * 32)
        data = _build("burnShares(bytes32)", ["bytes32"], [market_id])
        decoded = decode_submit(data)
        self.assertIn("burnShares", decoded)
        self.assertIn("0x" + "ab" * 32, decoded)

    def test_set_skim_recipient(self):
        data = _build("setSkimRecipient(address)", ["address"], [A2])
        self.assertEqual(
            decode_submit(data),
            f"setSkimRecipient(recipient {Web3.to_checksum_address(A2)})",
        )


class TestDecodeIdData(unittest.TestCase):
    def test_this_tag_is_adapter_id(self):
        id_data = abi_encode(["string", "address"], ["this", A1])
        self.assertEqual(
            decode_id_data(id_data),
            f"adapterId for adapter {Web3.to_checksum_address(A1)}",
        )

    def test_collateral_token_tag(self):
        id_data = abi_encode(["string", "address"], ["collateralToken", A2])
        self.assertEqual(
            decode_id_data(id_data),
            f"collateral token {Web3.to_checksum_address(A2)}",
        )

    def test_market_params_tag(self):
        market_params = (A1, A2, A3, A4, 86 * 10**16)  # 86% lltv
        id_data = abi_encode(
            ["string", "address", "(address,address,address,address,uint256)"],
            ["this/marketParams", A5, market_params],
        )
        decoded = decode_id_data(id_data)
        self.assertIn("market `0x", decoded)
        self.assertIn(f"loan {Web3.to_checksum_address(A1)}", decoded)
        self.assertIn(f"collateral {Web3.to_checksum_address(A2)}", decoded)
        self.assertIn("lltv 86.00%", decoded)
        self.assertIn(f"adapter {Web3.to_checksum_address(A5)}", decoded)

    def test_increase_absolute_cap_with_market_params(self):
        market_params = (A1, A2, A3, A4, 91 * 10**16)
        id_data = abi_encode(
            ["string", "address", "(address,address,address,address,uint256)"],
            ["this/marketParams", A5, market_params],
        )
        data = _build(
            "increaseAbsoluteCap(bytes,uint256)",
            ["bytes", "uint256"],
            [id_data, 1_000_000 * 10**6],
        )
        decoded = decode_submit(data)
        self.assertIn("increaseAbsoluteCap", decoded)
        self.assertIn("lltv 91.00%", decoded)
        self.assertIn(f"cap {1_000_000 * 10**6}", decoded)

    def test_increase_relative_cap_with_collateral_tag(self):
        id_data = abi_encode(["string", "address"], ["collateralToken", A1])
        data = _build(
            "increaseRelativeCap(bytes,uint256)",
            ["bytes", "uint256"],
            [id_data, 5 * 10**17],
        )
        decoded = decode_submit(data)
        self.assertIn("increaseRelativeCap", decoded)
        self.assertIn(f"collateral token {Web3.to_checksum_address(A1)}", decoded)


class TestSubmitDataKey(unittest.TestCase):
    def test_stable_for_identical_data(self):
        data = _build("addAdapter(address)", ["address"], [A1])
        self.assertEqual(submit_data_key(data), submit_data_key(data))

    def test_differs_per_payload(self):
        d1 = _build("addAdapter(address)", ["address"], [A1])
        d2 = _build("addAdapter(address)", ["address"], [A2])
        self.assertNotEqual(submit_data_key(d1), submit_data_key(d2))

    def test_no_0x_prefix(self):
        data = _build("addAdapter(address)", ["address"], [A1])
        key = submit_data_key(data)
        self.assertFalse(key.startswith("0x"))
        self.assertEqual(len(key), 64)


class TestUnknownSelector(unittest.TestCase):
    def test_renders_safely(self):
        data = b"\xde\xad\xbe\xef" + b"\x00" * 32
        decoded = decode_submit(data)
        # Should never raise; should mention either the selector or a friendly name.
        self.assertTrue(decoded)
        self.assertIsInstance(decoded, str)


if __name__ == "__main__":
    unittest.main()

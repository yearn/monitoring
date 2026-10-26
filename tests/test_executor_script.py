"""Tests for decoding Yearn governance Executor ``execute(bytes)`` scripts.

The script packs each call as a 32-byte ``target | calldata size`` header plus
the calldata, so it is not ABI-encoded and the generic nested-bytes decoding
can't see inside it. Fixture: yChad nonce 2299, which had the Executor accept
management of five reward distributors and the Funding Distributor.
"""

import unittest
from unittest.mock import patch

from eth_abi import encode
from eth_utils import to_checksum_address as _cs

from utils.calldata.decoder import DecodedCall, ScriptCall, decode_calldata
from utils.calldata.wrappers import InnerCall, split_packed_script, unwrap_calls, unwrap_executed_calls
from utils.llm.ai_explainer import _extract_address_args, _format_decoded_calls
from utils.llm.report import _format_params, _iter_reference_arguments

ACCEPT_MANAGEMENT = "0x759be10c"
FUNDING_DISTRIBUTOR = _cs("0xbcc932e4750c3e465a7e54a06a34f9edf8f6116b")
TARGETS = [
    _cs("0x95547ede56cf74b73dd78a37f547127dffda6113"),
    _cs("0x952b31960c97e76362ac340d07d183ada15e3d6e"),
    _cs("0x2548bf65916fdabb5a5673fc4225011ff29ee884"),
    _cs("0x7efc3953bed2fc20b9f825ebffab1cc8b072a000"),
    _cs("0xa139250bcb3880a97c75fd4287d5bf2a6caf98a3"),
    FUNDING_DISTRIBUTOR,
]
# The `_script` argument exactly as queued in yChad nonce 2299.
NONCE_2299_SCRIPT = bytes.fromhex(
    "95547ede56cf74b73dd78a37f547127dffda6113000000000000000000000004759be10c"
    "952b31960c97e76362ac340d07d183ada15e3d6e000000000000000000000004759be10c"
    "2548bf65916fdabb5a5673fc4225011ff29ee884000000000000000000000004759be10c"
    "7efc3953bed2fc20b9f825ebffab1cc8b072a000000000000000000000000004759be10c"
    "a139250bcb3880a97c75fd4287d5bf2a6caf98a3000000000000000000000004759be10c"
    "bcc932e4750c3e465a7e54a06a34f9edf8f6116b000000000000000000000004759be10c"
)


def script_entry(target: str, data: bytes) -> bytes:
    """One packed call: ``target (160 bits) | size (96 bits)`` header, then calldata."""
    return ((int(target, 16) << 96) | len(data)).to_bytes(32, "big") + data


def execute_call(script: bytes) -> str:
    return "0x09c5eabe" + encode(["bytes"], [script]).hex()


class TestSplitPackedScript(unittest.TestCase):
    def test_nonce_2299_script_is_six_accept_management_calls(self) -> None:
        pairs = split_packed_script(NONCE_2299_SCRIPT)
        self.assertEqual([_cs(t) for t, _ in pairs], TARGETS)
        self.assertEqual({"0x" + d.hex() for _, d in pairs}, {ACCEPT_MANAGEMENT})

    def test_variable_length_calldata(self) -> None:
        set_management = bytes.fromhex("fd066ecc") + encode(["address"], [FUNDING_DISTRIBUTOR])
        script = script_entry(TARGETS[0], set_management) + script_entry(TARGETS[1], b"")
        self.assertEqual(split_packed_script(script), [(TARGETS[0].lower(), set_management), (TARGETS[1].lower(), b"")])

    def test_malformed_scripts_raise(self) -> None:
        cases = {
            "empty": b"",
            "truncated header": NONCE_2299_SCRIPT[:-5][:31],
            "calldata overruns": NONCE_2299_SCRIPT[:-1],
            "trailing bytes": NONCE_2299_SCRIPT + b"\x00",
            "zero target": script_entry("0x0", b"\x75\x9b\xe1\x0c"),
            "too many calls": script_entry(TARGETS[0], b"") * 65,
        }
        for name, script in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                split_packed_script(script)

    def test_abi_encoded_calldata_is_not_a_script(self) -> None:
        abi_payload = bytes.fromhex("fd066ecc") + encode(["address"], [FUNDING_DISTRIBUTOR])
        with self.assertRaises(ValueError):
            split_packed_script(abi_payload)


class TestUnwrapExecutorScript(unittest.TestCase):
    def test_execute_runs_its_script_calls_now(self) -> None:
        expected = [InnerCall(t, ACCEPT_MANAGEMENT, f"execute call {i}") for i, t in enumerate(TARGETS, start=1)]
        self.assertEqual(unwrap_calls(execute_call(NONCE_2299_SCRIPT)), expected)
        self.assertEqual(unwrap_executed_calls(execute_call(NONCE_2299_SCRIPT)), expected)

    def test_execute_bytes_that_is_not_a_script_unwraps_to_nothing(self) -> None:
        self.assertEqual(unwrap_calls(execute_call(b"\x01\x02\x03")), [])


class TestDecodeScriptCalls(unittest.TestCase):
    def test_decoder_attaches_decoded_script_calls_offline(self) -> None:
        with patch("utils.calldata.decoder.fetch_json", side_effect=AssertionError("network")):
            decoded = decode_calldata(execute_call(NONCE_2299_SCRIPT))
        assert decoded is not None
        self.assertEqual(decoded.signature, "execute(bytes)")
        self.assertEqual([sc.target for sc in decoded.script_calls], TARGETS)
        self.assertEqual({sc.decoded.signature for sc in decoded.script_calls if sc.decoded}, {"accept_management()"})

    def test_inner_calls_use_the_inner_target_abi_when_chain_is_known(self) -> None:
        with patch("utils.calldata.decoder._resolve_signature_via_abi", return_value="accept_management()") as resolve:
            decode_calldata(execute_call(script_entry(TARGETS[0], bytes.fromhex("759be10c"))), chain_id=1)
        self.assertIn((1, TARGETS[0], ACCEPT_MANAGEMENT), [c.args for c in resolve.call_args_list])

    def test_unknown_inner_selector_stays_undecoded_without_network(self) -> None:
        script = script_entry(TARGETS[0], bytes.fromhex("12345678"))
        with (
            patch("utils.calldata.decoder.is_selector_resolvable_offline", side_effect=lambda s: s == "0x09c5eabe"),
            patch("utils.calldata.decoder.fetch_json", side_effect=AssertionError("network")),
        ):
            decoded = decode_calldata(execute_call(script))
        assert decoded is not None
        self.assertEqual(decoded.script_calls, (ScriptCall(TARGETS[0], "0x12345678", None),))

    def test_other_calls_have_no_script_calls(self) -> None:
        decoded = decode_calldata("0x759be10c")
        assert decoded is not None
        self.assertEqual(decoded.script_calls, ())


class TestScriptRendering(unittest.TestCase):
    def setUp(self) -> None:
        decoded = decode_calldata(execute_call(NONCE_2299_SCRIPT))
        assert decoded is not None
        self.decoded = decoded

    def test_prompt_lists_each_script_call_with_its_target(self) -> None:
        rendered = _format_decoded_calls([self.decoded], {FUNDING_DISTRIBUTOR: "Funding Distributor"}, [["_script"]])
        self.assertIn("Executor script, 6 calls; each is made by the Executor", rendered)
        self.assertIn(f"Script call 6 → {FUNDING_DISTRIBUTOR} (Funding Distributor): accept_management()", rendered)
        self.assertNotIn(NONCE_2299_SCRIPT.hex()[:40], rendered)

    def test_prompt_marks_undecoded_script_calls(self) -> None:
        call = DecodedCall(
            self.decoded.function_name,
            self.decoded.signature,
            self.decoded.params,
            (
                ScriptCall(TARGETS[0], "0x12345678", None),
                ScriptCall(TARGETS[1], "0x", None),
                ScriptCall(TARGETS[2], "0x010203", None),
            ),
        )
        rendered = _format_decoded_calls([call])
        self.assertIn(f"Script call 1 → {TARGETS[0]}: UNDECODED (selector 0x12345678", rendered)
        self.assertIn(f"Script call 2 → {TARGETS[1]}: empty calldata", rendered)
        self.assertIn(f"Script call 3 → {TARGETS[2]}: UNDECODED (short payload 0x010203, no full selector)", rendered)

    def test_report_keeps_short_payload_apart_from_empty_calldata(self) -> None:
        call = DecodedCall(
            self.decoded.function_name,
            self.decoded.signature,
            self.decoded.params,
            (ScriptCall(TARGETS[0], "0x", None), ScriptCall(TARGETS[1], "0x010203", None)),
        )
        lines = _format_params(call, 1, {}, ["_script"], "")
        self.assertTrue(lines[1].startswith("  - Script call 1: empty calldata (no function selector) on "))
        self.assertTrue(
            lines[2].startswith("  - Script call 2: undecoded short payload `0x010203` (no full selector) on ")
        )

    def test_report_links_each_script_target(self) -> None:
        lines = _format_params(self.decoded, 1, {}, ["_script"], "")
        self.assertEqual(lines[0], "- `bytes _script`: ↳ Executor script, 6 calls made by the Executor")
        self.assertIn(
            f"  - Script call 6: `accept_management()` on "
            f"[`{FUNDING_DISTRIBUTOR}`](https://etherscan.io/address/{FUNDING_DISTRIBUTOR})",
            lines,
        )

    def test_script_targets_get_labels_and_references(self) -> None:
        self.assertEqual(_extract_address_args(self.decoded), TARGETS)
        self.assertEqual(list(_iter_reference_arguments(self.decoded)), TARGETS)


if __name__ == "__main__":
    unittest.main()

"""Tests for explainer state."""

import unittest
from unittest.mock import MagicMock, patch

from utils.calldata.decoder import DecodedCall, decode_calldata
from utils.llm.ai_explainer import (
    MAX_STATE_READ_KEYS_PER_SIGNATURE,
    _collect_safety_checks,
    _collect_state_reads,
    explain_batch_transaction,
)

from .helpers import make_address, make_provider, make_set_cap


class TestCollectSafetyChecks(unittest.TestCase):
    """Tests for _collect_safety_checks (seatbelt-style deterministic checks)."""

    @patch("utils.llm.ai_explainer.get_function_state_mutability", return_value=None)
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=False)
    def test_unverified_target_flagged(self, _mut: MagicMock, _ver: MagicMock) -> None:
        call = DecodedCall(function_name="pause", signature="pause()")
        notes = _collect_safety_checks([("0xT", call, 0)], chain_id=1)
        self.assertEqual(len(notes), 1)
        self.assertIn("UNVERIFIED", notes[0])

    @patch("utils.llm.ai_explainer.get_function_state_mutability", return_value=None)
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=True)
    def test_verified_target_no_note(self, _mut: MagicMock, _ver: MagicMock) -> None:
        call = DecodedCall(function_name="pause", signature="pause()")
        self.assertEqual(_collect_safety_checks([("0xT", call, 0)], chain_id=1), [])

    @patch("utils.llm.ai_explainer.get_function_state_mutability", return_value=None)
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=None)
    def test_unknown_verification_no_note(self, _mut: MagicMock, _ver: MagicMock) -> None:
        # None (no API key / fetch error) must not cry wolf.
        call = DecodedCall(function_name="pause", signature="pause()")
        self.assertEqual(_collect_safety_checks([("0xT", call, 0)], chain_id=1), [])

    @patch("utils.llm.ai_explainer.get_function_state_mutability", return_value="nonpayable")
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=True)
    def test_value_to_nonpayable_flagged(self, _ver: MagicMock, _mut: MagicMock) -> None:
        call = DecodedCall(function_name="setConfig", signature="setConfig(uint256)")
        notes = _collect_safety_checks([("0xT", call, 10**18)], chain_id=1)
        self.assertEqual(len(notes), 1)
        self.assertIn("nonpayable", notes[0])
        self.assertIn("revert", notes[0])

    def test_value_to_view_or_pure_flagged(self) -> None:
        # view and pure functions are also non-payable; value to them reverts.
        for mut in ("view", "pure"):
            with self.subTest(mut=mut):
                with patch("utils.llm.ai_explainer.get_verification_status", return_value=True):
                    with patch("utils.llm.ai_explainer.get_function_state_mutability", return_value=mut):
                        call = DecodedCall(function_name="getConfig", signature="getConfig()")
                        notes = _collect_safety_checks([("0xT", call, 10**18)], chain_id=1)
                self.assertEqual(len(notes), 1)
                self.assertIn(mut, notes[0])

    @patch("utils.llm.ai_explainer.get_function_state_mutability", return_value="payable")
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=True)
    def test_value_to_payable_not_flagged(self, _ver: MagicMock, _mut: MagicMock) -> None:
        call = DecodedCall(function_name="deposit", signature="deposit()")
        self.assertEqual(_collect_safety_checks([("0xT", call, 10**18)], chain_id=1), [])

    @patch("utils.llm.ai_explainer.get_function_state_mutability")
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=False)
    def test_unknown_call_still_flags_unverified_without_dummy_decoded(
        self, _ver: MagicMock, mock_mut: MagicMock
    ) -> None:
        notes = _collect_safety_checks([("0xT", None, 10**18)], chain_id=1)
        self.assertEqual(len(notes), 1)
        self.assertIn("UNVERIFIED", notes[0])
        mock_mut.assert_not_called()

    @patch("utils.llm.ai_explainer.get_function_state_mutability")
    @patch("utils.llm.ai_explainer.get_verification_status", return_value=False)
    def test_empty_calldata_does_not_flag_unverified(self, mock_ver: MagicMock, mock_mut: MagicMock) -> None:
        notes = _collect_safety_checks(
            [("0xT", None, 10**18)],
            chain_id=1,
            decode_statuses=["empty_calldata"],
        )
        self.assertEqual(notes, [])
        mock_ver.assert_not_called()
        mock_mut.assert_not_called()


class TestCollectRoleNames(unittest.TestCase):
    """Tests for _collect_role_names (bytes32 role → name resolution).

    The call is built with the real `decode_calldata` rather than hand-written
    params on purpose: `eth_abi` hands back a bytes32 as 32 raw bytes, and an
    earlier version of this collector stringified that into a Python repr, so
    every role silently failed to resolve while string-based tests passed.
    """

    MINTER_HASH = "0x615a688d53344290b742a2e72e4f187e5b88227c01f9d77ce2406d32f8bd0eda"

    # Real call 0 of tx 0xcfa148be… — grantRole(RECEIPT_TOKEN_MINTER, OutlandVault).
    GRANT = decode_calldata(
        "0x2f2ff15d"
        "615a688d53344290b742a2e72e4f187e5b88227c01f9d77ce2406d32f8bd0eda"
        "000000000000000000000000a69e4155f62c097ce92daadef7a925dd40907c0c"
    )
    assert GRANT is not None

    def test_decoded_role_param_is_raw_bytes(self) -> None:
        """Guards the assumption the collector depends on."""
        type_str, value = self.GRANT.params[0]
        self.assertEqual(type_str, "bytes32")
        self.assertIsInstance(value, bytes)

    @patch("utils.llm.ai_explainer.resolve_role_names")
    def test_passes_normalized_hash_from_decoded_bytes(self, mock_resolve: MagicMock) -> None:
        """The hash handed to the resolver must be hex, not a bytes repr."""
        from utils.llm.ai_explainer import _collect_role_names

        mock_resolve.return_value = {}
        _collect_role_names([("0xCore", self.GRANT)], chain_id=1)

        passed = mock_resolve.call_args[0][0]
        self.assertEqual(passed, [self.MINTER_HASH])

    @patch("utils.llm.ai_explainer.resolve_role_names")
    def test_resolves_role_arguments(self, mock_resolve: MagicMock) -> None:
        from utils.llm.ai_explainer import _collect_role_names, _format_role_name_notes

        mock_resolve.return_value = {self.MINTER_HASH: "RECEIPT_TOKEN_MINTER"}
        resolved = _collect_role_names([("0xCore", self.GRANT)], chain_id=1)

        self.assertEqual(resolved["0xcore"][self.MINTER_HASH], "RECEIPT_TOKEN_MINTER")
        self.assertIn("is the role RECEIPT_TOKEN_MINTER", "\n".join(_format_role_name_notes(resolved)))

    @patch("utils.llm.ai_explainer.resolve_role_names")
    def test_ignores_bytes32_on_non_role_functions(self, mock_resolve: MagicMock) -> None:
        """A timelock's all-zero predecessor/salt must not become DEFAULT_ADMIN_ROLE."""
        from utils.llm.ai_explainer import _collect_role_names

        schedule = DecodedCall(
            function_name="scheduleBatch",
            signature="scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)",
            params=[("bytes32", b"\x00" * 32), ("bytes32", b"\x00" * 32), ("uint256", 604800)],
        )
        self.assertEqual(_collect_role_names([("0xTimelock", schedule)], chain_id=1), {})
        mock_resolve.assert_not_called()

    @patch("utils.llm.ai_explainer.resolve_role_names")
    def test_unresolved_roles_are_omitted(self, mock_resolve: MagicMock) -> None:
        from utils.llm.ai_explainer import _collect_role_names

        mock_resolve.return_value = {}
        self.assertEqual(_collect_role_names([("0xCore", self.GRANT)], chain_id=1), {})

    @patch("utils.llm.ai_explainer.resolve_role_names")
    def test_resolution_failure_never_raises(self, mock_resolve: MagicMock) -> None:
        from utils.llm.ai_explainer import _collect_role_names

        mock_resolve.side_effect = RuntimeError("etherscan down")
        self.assertEqual(_collect_role_names([("0xCore", self.GRANT)], chain_id=1), {})


class TestMappingKeyStateReads(unittest.TestCase):
    @patch("utils.llm.ai_explainer.read_before_state")
    def test_distinct_keys_are_read_and_duplicates_deduped(self, mock_read: MagicMock) -> None:
        mock_read.side_effect = lambda _chain, _target, decoded: [
            __import__("utils.on_chain_state", fromlist=["StateRead"]).StateRead(
                var_name="cap",
                type_str="mapping(address => uint256)",
                value=1,
                key_args=_setter_key(decoded),
            )
        ]
        a, b = make_address(1), make_address(2)
        result = _collect_state_reads(
            [
                ("0xT", make_set_cap(a, 10)),
                ("0xT", make_set_cap(b, 20)),
                ("0xT", make_set_cap(a, 99)),
            ],
            chain_id=1,
        )
        self.assertEqual(mock_read.call_count, 2)
        rendered = result.prompt_text()
        self.assertIn(a, rendered)
        self.assertIn(b, rendered)

    @patch("utils.llm.ai_explainer.read_before_state")
    def test_single_arg_setters_share_one_scalar_read(self, mock_read: MagicMock) -> None:
        from utils.on_chain_state import StateRead

        mock_read.return_value = [StateRead(var_name="maxSlippage", type_str="uint256", value=10**18, key_args=())]

        def _set_slippage(value: int) -> DecodedCall:
            return DecodedCall(
                function_name="setMaxSlippage",
                signature="setMaxSlippage(uint256)",
                params=[("uint256", value)],
            )

        result = _collect_state_reads(
            [("0xT", _set_slippage(99 * 10**16)), ("0xT", _set_slippage(98 * 10**16))],
            chain_id=1,
        )
        self.assertEqual(mock_read.call_count, 1)
        rendered = result.prompt_text()
        self.assertIn("maxSlippage = 1000000000000000000", rendered)
        self.assertNotIn("maxSlippage(", rendered)

    @patch("utils.llm.ai_explainer.read_before_state")
    def test_forty_keys_cap_at_twelve_and_report_omitted(self, mock_read: MagicMock) -> None:
        from utils.on_chain_state import StateRead

        mock_read.side_effect = lambda _chain, _target, decoded: [
            StateRead(
                var_name="cap",
                type_str="mapping(address => uint256)",
                value=1,
                key_args=(decoded.params[0][1],),
            )
        ]
        calls = [("0xT", make_set_cap(make_address(i))) for i in range(1, 41)]
        result = _collect_state_reads(calls, chain_id=1)
        self.assertEqual(mock_read.call_count, MAX_STATE_READ_KEYS_PER_SIGNATURE)
        self.assertIn("28 additional mapping keys skipped", result.prompt_text())
        self.assertIn(f"(limit: {MAX_STATE_READ_KEYS_PER_SIGNATURE})", result.report_text())
        unavailable = [r for _, reads in result.by_target for r in reads if not r.available]
        self.assertEqual(len(unavailable), 28)
        self.assertTrue(all(r.var_name == "cap" for r in unavailable))
        md = result.report_text(chain_id=1)
        self.assertIn("- On", md)
        self.assertIn("_unavailable_", md)

    @patch("utils.llm.ai_explainer.get_source_context", return_value=None)
    @patch("utils.llm.ai_explainer.get_contract_label", return_value="")
    @patch("utils.llm.ai_explainer.read_before_state")
    @patch("utils.llm.ai_explainer.get_llm_provider")
    @patch("utils.llm.ai_explainer.simulate_transaction", return_value=None)
    @patch("utils.llm.ai_explainer.decode_calldata")
    def test_batch_retains_all_calls_when_keys_are_capped(
        self,
        mock_decode: MagicMock,
        _mock_simulate: MagicMock,
        mock_get_provider: MagicMock,
        mock_read: MagicMock,
        _mock_label: MagicMock,
        _mock_source: MagicMock,
    ) -> None:
        from utils.on_chain_state import StateRead

        mock_decode.side_effect = [make_set_cap(make_address(i)) for i in range(1, 41)]
        mock_read.side_effect = lambda _chain, _target, decoded: [
            StateRead(
                var_name="cap",
                type_str="mapping(address => uint256)",
                value=1,
                key_args=(decoded.params[0][1],),
            )
        ]
        provider = make_provider("TLDR: sets many caps. LOW.\n\nDETAIL:\nanalysis.")
        mock_get_provider.return_value = provider

        result = explain_batch_transaction(
            [{"target": "0xT", "data": f"0x{i:08x}", "value": "0"} for i in range(1, 41)],
            chain_id=1,
            refine=False,
        )
        assert result is not None
        self.assertEqual(mock_read.call_count, MAX_STATE_READ_KEYS_PER_SIGNATURE)
        self.assertIn("1. **`setCap(address,uint256)`**", result.report)
        self.assertIn("40. **`setCap(address,uint256)`**", result.report)
        self.assertIn("28 additional mapping keys skipped", result.report)
        self.assertIn("28 additional mapping keys skipped", provider.complete.call_args[0][0])

    @patch("utils.llm.ai_explainer.read_before_state")
    def test_overloads_have_separate_caps(self, mock_read: MagicMock) -> None:
        from utils.on_chain_state import StateRead

        mock_read.return_value = [StateRead(var_name="cap", type_str="uint256", value=1)]
        addr_calls = [("0xT", make_set_cap(make_address(i))) for i in range(1, 14)]
        bytes_calls = [
            (
                "0xT",
                DecodedCall(
                    function_name="setCap",
                    signature="setCap(bytes32,uint256)",
                    params=[("bytes32", bytes([i]) * 32), ("uint256", 1)],
                ),
            )
            for i in range(1, 5)
        ]
        result = _collect_state_reads(addr_calls + bytes_calls, chain_id=1)
        self.assertEqual(mock_read.call_count, 12 + 4)
        self.assertIn("1 additional mapping key skipped for `setCap(address,uint256)`", result.prompt_text())

    @patch("utils.llm.ai_explainer.read_before_state")
    def test_worker_failure_marked_unavailable_against_a_known_getter(self, mock_read: MagicMock) -> None:
        """A key whose read fails is still reported, named by the getter a sibling found."""
        from utils.on_chain_state import StateRead

        def read(_chain: int, _target: str, decoded: DecodedCall) -> list[StateRead]:
            if decoded.params[0][1] == make_address(2):
                raise RuntimeError("rpc down")
            return [StateRead(var_name="cap", type_str="uint256", value=5, key_args=(decoded.params[0][1],))]

        mock_read.side_effect = read
        result = _collect_state_reads(
            [("0xT", make_set_cap(make_address(1))), ("0xT", make_set_cap(make_address(2)))], chain_id=1
        )
        reads = result.by_target[0][1]
        unavailable = [r for r in reads if not r.available]
        self.assertEqual(len(unavailable), 1)
        self.assertEqual(unavailable[0].var_name, "cap")
        self.assertEqual(mock_read.call_count, 2)

    @patch("utils.llm.ai_explainer.read_before_state", return_value=[])
    def test_unidentified_getter_reports_nothing_rather_than_the_setter(self, mock_read: MagicMock) -> None:
        """OApp's setPeer delegates its write, so no state var is found.

        Emitting ``setPeer(30183) = unavailable`` would name a getter that does
        not exist, which is worse than saying nothing.
        """
        result = _collect_state_reads(
            [("0xT", make_set_cap(make_address(1))), ("0xT", make_set_cap(make_address(2)))], chain_id=1
        )
        self.assertEqual(result.by_target, [])
        self.assertEqual(result.prompt_text(), "")
        self.assertEqual(mock_read.call_count, 2)


def _setter_key(decoded: DecodedCall) -> tuple:
    return (decoded.params[0][1],)

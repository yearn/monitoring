"""Role names retain their call and target scope through governance wrappers."""

import unittest
from unittest.mock import MagicMock, patch

from eth_utils import function_signature_to_4byte_selector

from tests.ai_explainer.helpers import make_provider
from tests.test_calldata_wrappers import ZERO32, encode_call, execute_batch
from utils.calldata.decoder import MAX_BYTES_RECURSION_DEPTH, DecodedCall, decode_calldata
from utils.calldata.role_names import DEFAULT_ADMIN_ROLE
from utils.calldata.wrappers import unwrap_call_values, unwrap_calls
from utils.llm.ai_explainer import _collect_role_names, explain_batch_transaction, explain_transaction
from utils.llm.protocol_context import ResolvedProtocolContext
from utils.llm.report import CallEntry, ReportContext, format_call_flow

CORE = "0xF6d48735EcCf12bDC1DF2674b1ce3fcb3bD25490"
OTHER = "0x20f1B8FB379d4cc07b917C68Ee855a1F35ef3Ed6"
TIMELOCK = "0x3D18480CC32B6AB3B833dCabD80E76CfD41c48a9"
MINTER = "0x615a688d53344290b742a2e72e4f187e5b88227c01f9d77ce2406d32f8bd0eda"
BURNER = "0xc843f0b739b89f713c29d5349e88538222378b9e38ad958b090593af5e3d0fd9"
SOURCE = (
    'bytes32 internal constant RECEIPT_TOKEN_MINTER = keccak256("RECEIPT_TOKEN_MINTER");\n'
    'bytes32 internal constant RECEIPT_TOKEN_BURNER = keccak256("RECEIPT_TOKEN_BURNER");'
)
GRANT_SIGNATURE = "grantRoles(bytes32[],address[])"


def grant_data(role: str = MINTER) -> str:
    """Encode a batch grant with one role and one recipient."""
    return encode_call(GRANT_SIGNATURE, ["bytes32[]", "address[]"], [[bytes.fromhex(role[2:])], [OTHER]])


def decoded(data: str) -> DecodedCall:
    """Decode test calldata, failing immediately if its signature was not registered."""
    call = decode_calldata(data)
    assert call is not None
    return call


def scheduled(data: str, target: str = CORE) -> str:
    """Wrap one payload in a timelock schedule call."""
    return encode_call(
        "schedule(address,uint256,bytes,bytes32,bytes32,uint256)",
        ["address", "uint256", "bytes", "bytes32", "bytes32", "uint256"],
        [target, 0, bytes.fromhex(data[2:]), ZERO32, ZERO32, 604800],
    )


class TestRoleContext(unittest.TestCase):
    """Exercise source resolution and report rendering with real encoded wrappers."""

    def setUp(self) -> None:
        """Keep custom role signatures and verified source entirely offline."""
        signature_patch = patch.dict(
            "utils.calldata.decoder.KNOWN_SELECTORS",
            {
                "0x" + function_signature_to_4byte_selector(signature).hex(): signature
                for signature in (GRANT_SIGNATURE, "queueTransaction(address,uint256,string,bytes,uint256)")
            },
        )
        signature_patch.start()
        self.addCleanup(signature_patch.stop)
        source_patch = patch("utils.source_context.fetch_source", return_value=("Core", SOURCE))
        self.source = source_patch.start()
        self.addCleanup(source_patch.stop)
        network_patch = patch(
            "utils.calldata.decoder.fetch_json", side_effect=AssertionError("Unexpected network lookup")
        )
        network_patch.start()
        self.addCleanup(network_patch.stop)

    def _flow(self, data: str) -> str:
        """Resolve a wrapped grant and render using each nested target's own roles."""
        call = decoded(data)
        roles = _collect_role_names([(TIMELOCK, call)], 1)
        self.assertEqual(roles, {CORE.lower(): {MINTER: "RECEIPT_TOKEN_MINTER"}})
        return format_call_flow(
            ReportContext(entries=[CallEntry(target=TIMELOCK, call=call)], chain_id=1, role_names_by_target=roles)
        )

    def test_executed_and_scheduled_grants_are_resolved(self) -> None:
        """Single, batch, and nested wrappers all expose their proposed role grants."""
        grant = grant_data()
        schedule_batch = encode_call(
            "scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)",
            ["address[]", "uint256[]", "bytes[]", "bytes32", "bytes32", "uint256"],
            [[CORE], [0], [bytes.fromhex(grant[2:])], ZERO32, ZERO32, 604800],
        )
        for data in (
            scheduled(grant),
            schedule_batch,
            execute_batch([CORE], [grant]),
            execute_batch([TIMELOCK], [scheduled(grant)]),
        ):
            with self.subTest(selector=data[:10]):
                self.assertIn(f"`{MINTER}` — role **RECEIPT_TOKEN_MINTER**", self._flow(data))

    def test_compound_signature_is_restored_for_nested_grant(self) -> None:
        """Compound queues store selector-free arguments when a signature is supplied."""
        data = encode_call(
            "queueTransaction(address,uint256,string,bytes,uint256)",
            ["address", "uint256", "string", "bytes", "uint256"],
            [CORE, 0, GRANT_SIGNATURE, bytes.fromhex(grant_data()[10:]), 1800000000],
        )
        self.assertIn("role **RECEIPT_TOKEN_MINTER**", self._flow(data))

    def test_executor_script_uses_each_inner_target(self) -> None:
        """Equal hashes on different script targets never share source-derived labels."""
        payload = bytes.fromhex(grant_data()[2:])
        script = b"".join(
            ((int(target, 16) << 96) | len(payload)).to_bytes(32, "big") + payload for target in (CORE, OTHER)
        )
        data = encode_call("execute(bytes)", ["bytes"], [script])
        call = decoded(data)
        roles = _collect_role_names([(TIMELOCK, call)], 1)
        self.assertEqual(set(roles), {CORE.lower(), OTHER.lower()})
        roles[OTHER.lower()][MINTER] = "OTHER_TARGET_ROLE"
        flow = format_call_flow(
            ReportContext(entries=[CallEntry(target=TIMELOCK, call=call)], chain_id=1, role_names_by_target=roles)
        )
        self.assertEqual(flow.count("role **RECEIPT_TOKEN_MINTER**"), 1)
        self.assertEqual(flow.count("role **OTHER_TARGET_ROLE**"), 1)

    def test_batch_payloads_with_equal_calldata_keep_target_scope(self) -> None:
        """Match payloads by their wrapper order, including duplicate calldata."""
        data = execute_batch([CORE, OTHER], [grant_data(), grant_data()])
        roles: dict[str, dict[str, str]] = {CORE.lower(): {MINTER: "CORE_ROLE"}, OTHER.lower(): {MINTER: "OTHER_ROLE"}}
        flow = format_call_flow(
            ReportContext(
                entries=[CallEntry(target=TIMELOCK, call=decoded(data))], chain_id=1, role_names_by_target=roles
            )
        )
        self.assertEqual(flow.count("role **CORE_ROLE**"), 1)
        self.assertEqual(flow.count("role **OTHER_ROLE**"), 1)

    def test_generic_bytes_without_known_target_do_not_inherit_roles(self) -> None:
        """Recognizing a nested selector does not establish which contract it targets."""
        call = DecodedCall("relay", "relay(bytes)", [("bytes", bytes.fromhex(grant_data()[2:]))])
        roles: dict[str, dict[str, str]] = {TIMELOCK.lower(): {MINTER: "PARENT_ROLE"}}
        flow = format_call_flow(
            ReportContext(entries=[CallEntry(target=TIMELOCK, call=call)], chain_id=1, role_names_by_target=roles)
        )
        self.assertIn(GRANT_SIGNATURE, flow)
        self.assertNotIn("role **", flow)

    def test_failed_nested_decode_keeps_raw_payload(self) -> None:
        """A decoding outage must neither abort the report nor discard the calldata."""
        call = decoded(scheduled(grant_data()))
        with patch("utils.llm.report.decode_calldata", side_effect=RuntimeError("lookup unavailable")):
            flow = format_call_flow(ReportContext(entries=[CallEntry(target=TIMELOCK, call=call)], chain_id=1))
        self.assertIn(grant_data(), flow)
        with patch("utils.llm.ai_explainer.decode_calldata", side_effect=RuntimeError("lookup unavailable")):
            self.assertEqual(_collect_role_names([(TIMELOCK, call)], 1), {})

    def test_non_role_salts_arrays_and_tuples_are_not_annotated(self) -> None:
        """A resolved zero admin role must not label salts or unrelated composite fields."""
        roles = {DEFAULT_ADMIN_ROLE: "DEFAULT_ADMIN_ROLE"}
        call = DecodedCall(
            "setConfig",
            "setConfig(bytes32,bytes32[],(bytes32,address))",
            [("bytes32", ZERO32), ("bytes32[]", (ZERO32,)), ("(bytes32,address)", (ZERO32, OTHER))],
        )
        entries = [
            CallEntry(
                target=CORE,
                call=decoded(encode_call("grantRole(bytes32,address)", ["bytes32", "address"], [ZERO32, OTHER])),
            ),
            CallEntry(target=CORE, call=call),
            CallEntry(target=CORE, call=decoded(scheduled(grant_data()))),
        ]
        flow = format_call_flow(ReportContext(entries=entries, chain_id=1, role_names_by_target={CORE.lower(): roles}))
        self.assertEqual(flow.count("role **DEFAULT_ADMIN_ROLE**"), 1)

    def test_struct_and_nested_array_roles_are_resolved(self) -> None:
        """Visit all bytes32 leaves while leaving malformed values unresolved."""
        for type_str, value in (
            ("(bytes32,address)[]", [(bytes.fromhex(MINTER[2:]), OTHER)]),
            ("bytes32[][]", [[bytes.fromhex(MINTER[2:]), b"short"], []]),
            ("(address,(bytes32[1],uint256))", (OTHER, ((bytes.fromhex(MINTER[2:]),), 1))),
        ):
            with self.subTest(type=type_str):
                call = DecodedCall("setRoles", f"setRoles({type_str})", [(type_str, value)])
                roles = _collect_role_names([(CORE, call)], 1)
                flow = format_call_flow(
                    ReportContext(entries=[CallEntry(target=CORE, call=call)], chain_id=1, role_names_by_target=roles)
                )
                self.assertIn("role **RECEIPT_TOKEN_MINTER**", flow)

    def test_mixed_case_target_roles_are_merged_before_resolution(self) -> None:
        """One source lookup must retain the union of roles for an address."""
        roles = _collect_role_names([(CORE, decoded(grant_data())), (CORE.lower(), decoded(grant_data(BURNER)))], 1)
        self.assertEqual(roles[CORE.lower()], {MINTER: "RECEIPT_TOKEN_MINTER", BURNER: "RECEIPT_TOKEN_BURNER"})
        self.source.assert_called_once()

    def test_wrapper_depth_is_bounded(self) -> None:
        """Adversarial wrapper nesting stops at the shared decoding depth limit."""
        data = grant_data()
        for _ in range(MAX_BYTES_RECURSION_DEPTH + 1):
            data = scheduled(data, TIMELOCK)
        self.assertEqual(_collect_role_names([(TIMELOCK, decoded(data))], 1), {})
        self.source.assert_not_called()

    def test_decoded_wrapper_extraction_matches_raw_and_ignores_malformed(self) -> None:
        """The raw and decoded wrapper paths preserve identical target/payload pairs."""
        data = execute_batch([CORE, OTHER], [grant_data(), grant_data(BURNER)])
        call = decoded(data)
        self.assertEqual(
            unwrap_call_values(call.signature, tuple(value for _, value in call.params)), unwrap_calls(data)
        )
        self.assertEqual(unwrap_call_values(call.signature, ()), [])
        self.assertEqual(unwrap_call_values("unknown(bytes)", (b"",)), [])

    @patch("utils.llm.ai_explainer._collect_address_labels", return_value={})
    @patch("utils.llm.ai_explainer._collect_related_tokens", return_value=[])
    @patch("utils.llm.ai_explainer.resolve_protocol_context", return_value=ResolvedProtocolContext())
    @patch("utils.llm.ai_explainer.get_llm_provider")
    def test_public_single_and_batch_explainers_keep_nested_roles(
        self, provider: MagicMock, _protocol: MagicMock, _tokens: MagicMock, _labels: MagicMock
    ) -> None:
        """Both entrypoints feed wrapped role names into the prompt and final report."""
        data = execute_batch([CORE], [grant_data()])
        for batch in (False, True):
            with self.subTest(batch=batch):
                mock_provider = make_provider("TLDR: grants a role. MEDIUM.\n\nDETAIL:\nPermission update.")
                provider.return_value = mock_provider
                result = (
                    explain_batch_transaction(
                        calls=[{"target": TIMELOCK, "data": data, "value": "0"}],
                        chain_id=1,
                        skip_simulation=True,
                        refine=False,
                    )
                    if batch
                    else explain_transaction(
                        target=TIMELOCK,
                        calldata=data,
                        chain_id=1,
                        skip_simulation=True,
                        refine=False,
                    )
                )
                assert result is not None
                self.assertIn("is the role RECEIPT_TOKEN_MINTER", mock_provider.complete.call_args_list[0].args[0])
                self.assertIn("role **RECEIPT_TOKEN_MINTER**", result.report)

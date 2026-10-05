"""Tests for the protocol-context adapter registry."""

import unittest
from unittest.mock import patch

from tests.test_calldata_wrappers import CUSD, ORACLE, ZERO32, encode_call, execute_batch, upgrade_to_and_call
from utils.calldata.decoder import DecodedCall
from utils.llm import protocol_context
from utils.llm.protocol_context import _Adapter, expand_executed_calls, resolve_protocol_context

TARGET = "0x6b276A2A7dd8b629adBA8A06AD6573d01C84f34E"
TOKEN = "0x333333330522F64EE8d0b3039c460b41670e3404"


class _FakeContext:
    def __init__(self, addresses: list[str], labels: dict[str, str]) -> None:
        self.addresses = addresses
        self.labels = labels


def _call() -> DecodedCall:
    return DecodedCall("setConfig", "setConfig(bytes32,uint256)", [("uint256", 1)])


def _adapter(name: str, contexts: list[_FakeContext]) -> _Adapter:
    return _Adapter(
        name=name,
        resolve=lambda protocol, chain_id, calls: contexts,
        format_prompt=lambda ctxs: f"{name} prompt",
        format_report=lambda ctxs, chain_id, labels: f"{name} report",
    )


class TestResolveProtocolContext(unittest.TestCase):
    """Every registered adapter contributes; a failing one is skipped."""

    def test_adapters_are_combined(self) -> None:
        adapters = (
            _adapter("alpha", [_FakeContext([TARGET], {TARGET: "Config"})]),
            _adapter("beta", [_FakeContext([TOKEN], {TOKEN: "Token"})]),
        )
        with patch.object(protocol_context, "_ADAPTERS", adapters):
            resolved = resolve_protocol_context("3JANE", 1, [(TARGET, _call())])

        self.assertEqual(resolved.prompt, "alpha prompt\n\nbeta prompt")
        self.assertEqual(resolved.report, "alpha report\n\nbeta report")
        self.assertEqual(resolved.addresses, [TARGET, TOKEN])
        self.assertEqual(resolved.labels, {TARGET: "Config", TOKEN: "Token"})

    def test_empty_adapter_contributes_nothing(self) -> None:
        with patch.object(protocol_context, "_ADAPTERS", (_adapter("alpha", []),)):
            resolved = resolve_protocol_context("3JANE", 1, [(TARGET, _call())])

        self.assertEqual(resolved.prompt, "")
        self.assertEqual(resolved.report, "")
        self.assertEqual(resolved.addresses, [])

    def test_failing_adapter_does_not_block_the_others(self) -> None:
        def explode(protocol: str, chain_id: int, calls: list) -> list:
            raise RuntimeError("etherscan down")

        broken = _Adapter("broken", explode, lambda c: "x", lambda contexts, chain_id, labels: "x")
        adapters = (broken, _adapter("beta", [_FakeContext([TOKEN], {})]))
        with patch.object(protocol_context, "_ADAPTERS", adapters):
            resolved = resolve_protocol_context("3JANE", 1, [(TARGET, _call())])

        self.assertEqual(resolved.prompt, "beta prompt")

    def test_existing_labels_reach_the_report_renderer(self) -> None:
        seen: dict[str, str] = {}

        def capture(contexts: list, chain_id: int, labels: dict[str, str]) -> str:
            seen.update(labels)
            return "report"

        adapter = _Adapter("alpha", lambda p, c, t: [_FakeContext([TOKEN], {TOKEN: "Token"})], lambda c: "p", capture)
        with patch.object(protocol_context, "_ADAPTERS", (adapter,)):
            resolve_protocol_context("3JANE", 1, [(TARGET, _call())], {TARGET: "Timelock"})

        self.assertEqual(seen, {TARGET: "Timelock", TOKEN: "Token"})

    def test_registered_adapters_cover_the_known_protocols(self) -> None:
        self.assertEqual(
            {adapter.name for adapter in protocol_context._ADAPTERS},
            {
                "infinifi",
                "infinifi-outland",
                "3jane",
                "pendle",
                "yearn-v3",
                "control-transfer",
                "permission-grant",
                "timelock-execution",
            },
        )


class TestExpandExecutedCalls(unittest.TestCase):
    """Adapters see the inner calls of an executed timelock batch, in order, after the wrapper."""

    def setUp(self) -> None:
        self.inner_a = DecodedCall("add_strategy", "add_strategy(address,bool)", [("address", TOKEN), ("bool", True)])
        self.inner_b = DecodedCall("update_debt", "update_debt(address,uint256)", [("address", TOKEN), ("uint256", 1)])
        self.wrapper = DecodedCall("executeBatch", "executeBatch(address[],uint256[],bytes[],bytes32,bytes32)", [])
        self.top = DecodedCall("set_default_queue", "set_default_queue(address[])", [("address[]", [TOKEN])])

    def _decode(self, data: str, chain_id: int | None = None, target: str | None = None) -> DecodedCall | None:
        return {ORACLE: self.inner_a, CUSD: self.inner_b}.get(target or "")

    def test_inner_calls_follow_their_wrapper(self) -> None:
        data = execute_batch([ORACLE, CUSD], [upgrade_to_and_call(TARGET), upgrade_to_and_call(TARGET)])
        with patch.object(protocol_context, "decode_calldata", side_effect=self._decode):
            expanded = expand_executed_calls(1, [(TARGET, data, self.wrapper), (TOKEN, "0x", self.top)])
        self.assertEqual(
            expanded,
            [(TARGET, self.wrapper), (ORACLE, self.inner_a), (CUSD, self.inner_b), (TOKEN, self.top)],
        )

    def test_scheduled_calls_are_not_expanded(self) -> None:
        data = encode_call(
            "scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)",
            ["address[]", "uint256[]", "bytes[]", "bytes32", "bytes32", "uint256"],
            [[ORACLE], [0], [bytes.fromhex(upgrade_to_and_call(TARGET)[2:])], ZERO32, ZERO32, 86400],
        )
        with patch.object(protocol_context, "decode_calldata", side_effect=self._decode):
            self.assertEqual(expand_executed_calls(1, [(TARGET, data, self.wrapper)]), [(TARGET, self.wrapper)])

    def test_undecoded_calls_are_dropped(self) -> None:
        self.assertEqual(expand_executed_calls(1, [(TARGET, "0x", None)]), [])


if __name__ == "__main__":
    unittest.main()

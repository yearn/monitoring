"""Tests for the protocol-context adapter registry."""

import unittest
from unittest.mock import patch

from utils.calldata.decoder import DecodedCall
from utils.llm import protocol_context
from utils.llm.protocol_context import _Adapter, resolve_protocol_context

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
        self.assertEqual({adapter.name for adapter in protocol_context._ADAPTERS}, {"infinifi", "3jane"})


if __name__ == "__main__":
    unittest.main()

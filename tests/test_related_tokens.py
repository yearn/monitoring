"""Tests for utils/related_tokens.py (token discovery from a contract's own getters)."""

import unittest
from unittest.mock import patch

import utils.related_tokens as related_tokens
from utils.erc20_metadata import ERC20Metadata
from utils.related_tokens import (
    MAX_GETTER_CALLS,
    RelatedToken,
    _address_getter_names,
    format_related_tokens_block,
    resolve_related_tokens,
)

DISTRIBUTOR = "0xaC6985D4dBcd89CCAD71DB9bf0309eaF57F064e8"
JANE = "0x333333330522F64EE8d0b3039c460b41670e3404"
TIMELOCK = "0x1dCcD4628d48a50C1A7adEA3848bcC869f08f8C2"


def _fn(name: str, mutability: str = "view", inputs=None, outputs=(("", "address"),)) -> dict:
    return {
        "type": "function",
        "name": name,
        "stateMutability": mutability,
        "inputs": inputs or [],
        "outputs": [{"name": n, "type": t} for n, t in outputs],
    }


class TestAddressGetterNames(unittest.TestCase):
    def test_selects_zero_arg_address_views(self) -> None:
        abi = [
            _fn("jane"),
            _fn("owner"),
            _fn("balanceOf", inputs=[{"name": "a", "type": "address"}]),  # takes args
            _fn("merkleRoot", outputs=(("", "bytes32"),)),  # wrong output type
            _fn("setRoot", mutability="nonpayable"),  # not a view
            _fn("pair", outputs=(("a", "address"), ("b", "address"))),  # two outputs
            {"type": "event", "name": "token"},  # not a function
        ]
        self.assertEqual(_address_getter_names(abi), ["jane", "owner"])

    def test_empty_abi(self) -> None:
        self.assertEqual(_address_getter_names([]), [])


class TestResolveRelatedTokens(unittest.TestCase):
    def setUp(self) -> None:
        related_tokens._cache.clear()

    def test_keeps_erc20_getters_and_drops_the_rest(self) -> None:
        """owner() must filter itself out because it isn't a token — no name blocklist."""
        metadata = {JANE.lower(): ERC20Metadata(symbol="JANE", decimals=18)}
        with (
            patch.object(related_tokens, "fetch_abi_entries", return_value=[_fn("jane"), _fn("owner")]),
            patch.object(related_tokens, "_call_address_getters", return_value={"jane": JANE, "owner": TIMELOCK}),
            patch.object(related_tokens, "fetch_erc20_metadata", side_effect=lambda _c, a: metadata.get(a.lower())),
        ):
            tokens = resolve_related_tokens(1, DISTRIBUTOR)
        self.assertEqual(tokens, [RelatedToken(getter="jane", address=JANE, symbol="JANE", decimals=18)])
        self.assertEqual(tokens[0].source, "jane()")

    def test_target_that_is_itself_a_token(self) -> None:
        with (
            patch.object(related_tokens, "fetch_abi_entries", return_value=[]),
            patch.object(related_tokens, "fetch_erc20_metadata", return_value=ERC20Metadata(symbol="USDC", decimals=6)),
        ):
            tokens = resolve_related_tokens(1, JANE)
        self.assertEqual(tokens[0].getter, "self")
        self.assertEqual(tokens[0].source, "the target itself")

    def test_no_getters_returns_empty(self) -> None:
        with (
            patch.object(related_tokens, "fetch_abi_entries", return_value=[_fn("owner")]),
            patch.object(related_tokens, "_call_address_getters", return_value={"owner": TIMELOCK}),
            patch.object(related_tokens, "fetch_erc20_metadata", return_value=None),
        ):
            self.assertEqual(resolve_related_tokens(1, DISTRIBUTOR), [])

    def test_getter_calls_are_capped(self) -> None:
        abi = [_fn(f"token{i}") for i in range(MAX_GETTER_CALLS + 5)]
        with (
            patch.object(related_tokens, "fetch_abi_entries", return_value=abi),
            patch.object(related_tokens, "fetch_erc20_metadata", return_value=None),
            patch.object(related_tokens, "_call_address_getters", return_value={}) as mock_call,
        ):
            resolve_related_tokens(1, DISTRIBUTOR)
        self.assertEqual(len(mock_call.call_args[0][2]), MAX_GETTER_CALLS)

    def test_failure_is_swallowed(self) -> None:
        with (
            patch.object(related_tokens, "fetch_erc20_metadata", return_value=None),
            patch.object(related_tokens, "fetch_abi_entries", side_effect=RuntimeError("etherscan down")),
        ):
            self.assertEqual(resolve_related_tokens(1, DISTRIBUTOR), [])

    def test_result_is_cached_per_target(self) -> None:
        with (
            patch.object(related_tokens, "fetch_erc20_metadata", return_value=None),
            patch.object(related_tokens, "fetch_abi_entries", return_value=[]) as mock_abi,
        ):
            resolve_related_tokens(1, DISTRIBUTOR)
            resolve_related_tokens(1, DISTRIBUTOR)
        self.assertEqual(mock_abi.call_count, 1)

    def test_empty_target(self) -> None:
        self.assertEqual(resolve_related_tokens(1, ""), [])


class TestFormatRelatedTokensBlock(unittest.TestCase):
    def test_renders_getter_provenance_and_decimals(self) -> None:
        token = RelatedToken(getter="jane", address=JANE, symbol="JANE", decimals=18)
        block = format_related_tokens_block([(DISTRIBUTOR, [token])], {DISTRIBUTOR: "RewardsDistributor"})
        self.assertIn(f"{DISTRIBUTOR} (RewardsDistributor):", block)
        self.assertIn(f"  jane() -> {JANE} (JANE, 18 decimals)", block)

    def test_empty_when_nothing_resolved(self) -> None:
        self.assertEqual(format_related_tokens_block([(DISTRIBUTOR, [])]), "")
        self.assertEqual(format_related_tokens_block([]), "")


if __name__ == "__main__":
    unittest.main()

"""Tests for 3Jane-specific LLM governance context."""

import unittest
from unittest.mock import patch

from eth_utils import keccak

from utils.calldata.decoder import DecodedCall
from utils.llm import threejane_context
from utils.llm.threejane_context import (
    HashedLabelContext,
    RewardsDistributorContext,
    _bytes32_arguments,
    _requested_epochs,
    format_threejane_prompt,
    format_threejane_report,
    resolve_threejane_context,
)

DISTRIBUTOR = "0xaC6985D4dBcd89CCAD71DB9bf0309eaF57F064e8"
JANE = "0x333333330522F64EE8d0b3039c460b41670e3404"
PROTOCOL_CONFIG = "0x6b276A2A7dd8b629adBA8A06AD6573d01C84f34E"
SAFE = "0x33333333Bd7045F1A601A1E289D7AB21036fB5EF"

WAD = 10**18


def _set_emissions_call(epoch: int = 45, emissions: int = 5_564_323 * WAD) -> DecodedCall:
    return DecodedCall(
        function_name="setEpochEmissions",
        signature="setEpochEmissions(uint256,uint256)",
        params=[("uint256", epoch), ("uint256", emissions)],
    )


def _set_config_call(key: str = "MAX_LTV", value: int = 4 * 10**17) -> DecodedCall:
    return DecodedCall(
        function_name="setConfig",
        signature="setConfig(bytes32,uint256)",
        params=[("bytes32", keccak(text=key)), ("uint256", value)],
    )


def _distributor_context(use_mint: bool = True, is_minter: bool = True) -> RewardsDistributorContext:
    return RewardsDistributorContext(
        distributor_address=DISTRIBUTOR,
        token_address=JANE,
        token_symbol="JANE",
        token_decimals=18,
        use_mint=use_mint,
        distributor_is_minter=is_minter,
        token_transferable=False,
        token_total_supply_raw=38_919_583 * WAD,
        distributor_balance_raw=0,
        merkle_root="0x" + "9a" * 32,
        max_claimable_raw=84_649_011 * WAD,
        total_claimed_raw=38_919_583 * WAD,
        current_epoch=45,
        epoch_emissions=((43, 5_369_214 * WAD), (44, 5_499_673 * WAD), (45, 0)),
    )


class TestGuards(unittest.TestCase):
    """The adapter only claims 3Jane mainnet alerts."""

    def test_other_protocol_resolves_nothing(self) -> None:
        self.assertEqual(resolve_threejane_context("INFINIFI", 1, [(PROTOCOL_CONFIG, _set_config_call())]), [])

    def test_other_chain_resolves_nothing(self) -> None:
        self.assertEqual(resolve_threejane_context("3JANE", 8453, [(PROTOCOL_CONFIG, _set_config_call())]), [])

    def test_protocol_name_is_case_insensitive(self) -> None:
        with (
            patch.object(threejane_context, "_read_distributor_context", return_value=None) as distributor,
            patch.object(threejane_context, "_resolve_hashed_labels", return_value=[]),
        ):
            resolve_threejane_context("3jane", 1, [(PROTOCOL_CONFIG, _set_config_call())])
        distributor.assert_called_once()

    def test_resolution_failure_does_not_raise(self) -> None:
        with patch.object(threejane_context, "_read_distributor_context", side_effect=RuntimeError("rpc down")):
            self.assertEqual(resolve_threejane_context("3JANE", 1, [(DISTRIBUTOR, _set_emissions_call())]), [])


class TestArgumentParsing(unittest.TestCase):
    """bytes32 arguments and epoch numbers are read out of decoded calls."""

    def test_bytes32_argument_normalized_to_hex(self) -> None:
        call = _set_config_call("IS_PAUSED", 1)
        self.assertEqual(_bytes32_arguments(call), ["0x" + keccak(text="IS_PAUSED").hex()])

    def test_hex_string_argument_accepted(self) -> None:
        as_hex = "0x" + keccak(text="DEBT_CAP").hex().upper()
        call = DecodedCall("setConfig", "setConfig(bytes32,uint256)", [("bytes32", as_hex), ("uint256", 1)])
        self.assertEqual(_bytes32_arguments(call), [as_hex.lower()])

    def test_undersized_bytes_ignored(self) -> None:
        call = DecodedCall("setConfig", "setConfig(bytes32,uint256)", [("bytes32", b"\x01\x02"), ("uint256", 1)])
        self.assertEqual(_bytes32_arguments(call), [])

    def test_epoch_taken_from_call(self) -> None:
        self.assertEqual(_requested_epochs([_set_emissions_call(epoch=45)], current_epoch=12), [45])

    def test_epoch_falls_back_to_current(self) -> None:
        call = DecodedCall("updateRoot", "updateRoot(bytes32)", [("bytes32", b"\x00" * 32)])
        self.assertEqual(_requested_epochs([call], current_epoch=45), [45])


class TestAbiProbeCaching(unittest.TestCase):
    """One alert probes a target for several shapes; the slot is read once."""

    def setUp(self) -> None:
        threejane_context.reset_cache()

    def tearDown(self) -> None:
        threejane_context.reset_cache()

    def test_implementation_is_read_once_per_address(self) -> None:
        proxy_abi = [{"type": "function", "name": "upgradeToAndCall"}]
        impl_abi = [{"type": "function", "name": "config"}]

        def abi_for(chain_id: int, address: str) -> list[dict]:
            return impl_abi if address == "0ximpl" else proxy_abi

        with (
            patch.object(threejane_context, "fetch_abi_entries", side_effect=abi_for),
            patch("utils.proxy.get_current_implementation", return_value="0ximpl") as lookup,
        ):
            self.assertFalse(threejane_context._exposes(1, PROTOCOL_CONFIG, {"useMint", "merkleRoot"}))
            self.assertTrue(threejane_context._exposes(1, PROTOCOL_CONFIG, {"config"}))

        lookup.assert_called_once()

    def test_non_proxy_never_reads_the_slot(self) -> None:
        own_abi = [{"type": "function", "name": "useMint"}]
        with (
            patch.object(threejane_context, "fetch_abi_entries", return_value=own_abi),
            patch("utils.proxy.get_current_implementation") as lookup,
        ):
            self.assertTrue(threejane_context._exposes(1, DISTRIBUTOR, {"useMint"}))

        lookup.assert_not_called()


class TestCheckedInAbis(unittest.TestCase):
    """The JSON ABIs cover exactly the getters the adapter reads."""

    def test_distributor_abi_covers_the_detection_getters(self) -> None:
        names = {entry["name"] for entry in threejane_context._abi("RewardsDistributor")}
        self.assertTrue(threejane_context._DISTRIBUTOR_GETTERS.issubset(names))
        self.assertIn("epoch", names)

    def test_jane_abi_covers_the_token_reads(self) -> None:
        names = {entry["name"] for entry in threejane_context._abi("Jane")}
        self.assertEqual(names, {"totalSupply", "transferable", "balanceOf", "hasRole"})

    def test_protocol_config_abi_exposes_config(self) -> None:
        names = {entry["name"] for entry in threejane_context._abi("ProtocolConfig")}
        self.assertIn("config", names)


class TestHashedLabelRendering(unittest.TestCase):
    """Known hashes are named; only config keys carry a stored value."""

    def test_config_key_prompt_states_name_and_value(self) -> None:
        context = HashedLabelContext(
            target=PROTOCOL_CONFIG,
            argument_hex="0x" + keccak(text="MAX_LTV").hex(),
            name="MAX_LTV",
            note="maximum loan-to-value accepted when setting a credit line (WAD)",
            is_config_key=True,
            current_value=350000000000000000,
        )
        prompt = format_threejane_prompt([context])
        self.assertIn('keccak256("MAX_LTV")', prompt)
        self.assertIn("value stored on-chain right now: 350000000000000000", prompt)

    def test_role_hash_has_no_value_line(self) -> None:
        context = HashedLabelContext(
            target=JANE,
            argument_hex="0x" + keccak(text="MINTER_ROLE").hex(),
            name="MINTER_ROLE",
            note="minter role: can mint new JANE",
        )
        prompt = format_threejane_prompt([context])
        report = format_threejane_report([context], 1, {})
        self.assertIn('keccak256("MINTER_ROLE")', prompt)
        self.assertNotIn("value stored on-chain", prompt)
        self.assertNotIn("Value stored on-chain", report)

    def test_report_links_the_target(self) -> None:
        context = HashedLabelContext(PROTOCOL_CONFIG, "0xabc", "DEBT_CAP", "ceiling", True, 63_366_281_225_814)
        report = format_threejane_report([context], 1, {PROTOCOL_CONFIG: "3Jane ProtocolConfig"})
        self.assertIn(f"https://etherscan.io/address/{PROTOCOL_CONFIG}", report)
        self.assertIn("3Jane ProtocolConfig", report)
        self.assertIn("`63,366,281,225,814`", report)

    def test_every_known_label_hashes_to_its_own_entry(self) -> None:
        for as_hex, (name, note) in threejane_context._LABELS_BY_HASH.items():
            self.assertEqual(as_hex, "0x" + keccak(text=name).hex())
            self.assertTrue(note, f"{name} has no explanatory note")


class TestDistributorRendering(unittest.TestCase):
    """The distribution mode is stated instead of hedged."""

    def test_mint_mode_names_the_authority_and_dismisses_balance(self) -> None:
        prompt = format_threejane_prompt([_distributor_context()])
        self.assertIn("claims MINT new JANE", prompt)
        self.assertIn("holds MINTER_ROLE", prompt)
        self.assertIn("not the funding source", prompt)

    def test_mint_mode_without_minter_role_is_flagged(self) -> None:
        prompt = format_threejane_prompt([_distributor_context(is_minter=False)])
        self.assertIn("does NOT hold MINTER_ROLE", prompt)

    def test_transfer_mode_points_at_the_balance(self) -> None:
        prompt = format_threejane_prompt([_distributor_context(use_mint=False)])
        self.assertIn("claims TRANSFER", prompt)
        self.assertNotIn("MINT new JANE", prompt)

    def test_emission_history_is_included_for_comparison(self) -> None:
        prompt = format_threejane_prompt([_distributor_context()])
        self.assertIn("epoch 43: 5,369,214 JANE", prompt)
        self.assertIn("epoch 44: 5,499,673 JANE", prompt)
        self.assertIn("epoch 45: 0 JANE", prompt)

    def test_amounts_are_truncated_to_whole_tokens(self) -> None:
        context = _distributor_context()
        self.assertEqual(context.amount(5_564_323 * WAD + 764_853_960_935_076_906), "5,564,323 JANE")
        self.assertEqual(context.amount(0), "0 JANE")
        self.assertEqual(context.amount(WAD // 2), "0.5 JANE")
        self.assertEqual(context.amount(1), "<0.1 JANE")

    def test_outstanding_is_allocated_minus_claimed(self) -> None:
        self.assertEqual(_distributor_context().outstanding_raw, (84_649_011 - 38_919_583) * WAD)

    def test_report_lists_accounting_and_links_token(self) -> None:
        report = format_threejane_report([_distributor_context()], 1, {})
        self.assertIn("**Distribution mode:**", report)
        self.assertIn("maxClaimable 84,649,011 JANE", report)
        self.assertIn(f"https://etherscan.io/address/{JANE}", report)

    def test_context_contributes_addresses_and_labels(self) -> None:
        context = _distributor_context()
        self.assertEqual(context.addresses, [DISTRIBUTOR, JANE])
        self.assertEqual(context.labels[JANE], "JANE token")


if __name__ == "__main__":
    unittest.main()

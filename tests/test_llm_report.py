"""Tests for the gist report renderer (utils/llm/report.py)."""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from utils.calldata.decoder import MAX_BYTES_RECURSION_DEPTH, DecodedCall
from utils.llm.report import (
    CallEntry,
    ReportContext,
    address_link,
    array_element_type,
    build_report,
    build_title,
    explorer_address_url,
    format_address_links_block,
    format_call_flow,
    format_reference_table,
    iter_address_values,
    tuple_component_types,
)
from utils.related_tokens import RelatedToken

REGISTRY = "0xF5f2718708f471e43968271956CC01aaA8c46119"
FARM = "0x79e1b8e45932a7c802ea3dab3844e5dea68d971f"
FARM_CKS = "0x79e1B8e45932A7C802eA3dAb3844e5DEa68d971f"
TIMELOCK = "0x4B174afbeD7b98BA01F50E36109EEE5e6d327c32"
DROP = "0xE4C72b4dE5b0F9ACcEA880Ad0b1F944F85A9dAA0"


def _add_farms_ctx(**overrides) -> ReportContext:
    call = DecodedCall(
        function_name="addFarms",
        signature="addFarms(uint256,address[])",
        params=[("uint256", 2), ("address[]", (FARM,))],
    )
    defaults = {
        "entries": [CallEntry(target=REGISTRY, call=call, param_names=["_type", "_farms"])],
        "chain_id": 1,
        "labels": {REGISTRY: "FarmRegistry"},
        "protocol": "INFINIFI",
        "label": "Infinifi Shorttimelock",
        "from_address": TIMELOCK,
    }
    defaults.update(overrides)
    return ReportContext(**defaults)  # type: ignore[arg-type]


class TestAddressLink(unittest.TestCase):
    def test_full_checksummed_address_is_linked(self) -> None:
        result = address_link(FARM, 1)
        self.assertEqual(result, f"[`{FARM_CKS}`](https://etherscan.io/address/{FARM_CKS})")

    def test_label_is_appended(self) -> None:
        self.assertTrue(address_link(REGISTRY, 1, {REGISTRY: "FarmRegistry"}).endswith("(FarmRegistry)"))

    def test_chain_specific_explorer(self) -> None:
        self.assertIn("arbiscan.io", address_link(FARM, 42161))
        self.assertIn("basescan.org", address_link(FARM, 8453))

    def test_unknown_chain_falls_back_to_plain_code(self) -> None:
        self.assertEqual(address_link(FARM, 999999), f"`{FARM_CKS}`")
        self.assertEqual(explorer_address_url(999999, FARM), "")

    def test_non_address_passes_through(self) -> None:
        self.assertEqual(address_link("not-an-address", 1), "not-an-address")


class TestAddressLinksBlock(unittest.TestCase):
    def test_lists_one_markdown_link_per_address(self) -> None:
        block = format_address_links_block([REGISTRY, FARM], 1, {REGISTRY: "FarmRegistry"})
        self.assertIn(f"- [`{REGISTRY}`](https://etherscan.io/address/{REGISTRY}) (FarmRegistry)", block)
        self.assertIn(f"- [`{FARM_CKS}`](https://etherscan.io/address/{FARM_CKS})", block)

    def test_empty_when_chain_has_no_explorer(self) -> None:
        self.assertEqual(format_address_links_block([REGISTRY], 999999), "")


class TestFormatCallFlow(unittest.TestCase):
    def test_renders_sender_target_and_params(self) -> None:
        flow = format_call_flow(_add_farms_ctx())
        self.assertIn(f"**From:** [`{TIMELOCK}`](https://etherscan.io/address/{TIMELOCK})", flow)
        self.assertIn("1. **`addFarms(uint256,address[])`**", flow)
        self.assertIn(f"on [`{REGISTRY}`](https://etherscan.io/address/{REGISTRY}) (FarmRegistry)", flow)
        self.assertIn("- `uint256 _type`: `2`", flow)
        self.assertIn(f"     - [`{FARM_CKS}`](https://etherscan.io/address/{FARM_CKS})", flow)

    def test_bare_types_when_param_names_unknown(self) -> None:
        ctx = _add_farms_ctx(
            entries=[
                CallEntry(
                    target=REGISTRY,
                    call=DecodedCall(function_name="setFee", signature="setFee(uint256)", params=[("uint256", 2500)]),
                )
            ]
        )
        self.assertIn("- `uint256`: `2,500`", format_call_flow(ctx))

    def test_no_inputs_marker(self) -> None:
        ctx = _add_farms_ctx(
            entries=[CallEntry(target=REGISTRY, call=DecodedCall(function_name="pause", signature="pause()"))]
        )
        self.assertIn("_no inputs_", format_call_flow(ctx))

    def test_eth_value_shown(self) -> None:
        ctx = _add_farms_ctx(
            entries=[
                CallEntry(
                    target=REGISTRY,
                    call=DecodedCall(function_name="deposit", signature="deposit()"),
                    value=10**18,
                )
            ]
        )
        self.assertIn("**ETH value:** `1.000000` ETH", format_call_flow(ctx))

    def test_zero_sender_omitted(self) -> None:
        ctx = _add_farms_ctx(from_address="0x" + "00" * 20)
        self.assertNotIn("**From:**", format_call_flow(ctx))

    def test_numbered_across_batch(self) -> None:
        call = DecodedCall(function_name="pause", signature="pause()")
        ctx = _add_farms_ctx(
            entries=[CallEntry(target=REGISTRY, call=call), CallEntry(target=FARM, call=call)],
        )
        flow = format_call_flow(ctx)
        self.assertIn("1. **`pause()`**", flow)
        self.assertIn("2. **`pause()`**", flow)

    def test_nested_bytes_decoded(self) -> None:
        # `0x8456cb59` is pause() — a known selector, resolvable offline.
        outer = DecodedCall(
            function_name="upgradeToAndCall",
            signature="upgradeToAndCall(address,bytes)",
            params=[("address", FARM), ("bytes", "0x8456cb59")],
        )
        flow = format_call_flow(_add_farms_ctx(entries=[CallEntry(target=REGISTRY, call=outer)]))
        self.assertIn("↳ `pause()`", flow)

    def test_nested_recursion_capped(self) -> None:
        self_referential = DecodedCall(
            function_name="wrap",
            signature="wrap(bytes)",
            params=[("bytes", "0xfeedfacefeedfacefeedfacefeedfacefeedface")],
        )
        ctx = _add_farms_ctx(entries=[CallEntry(target=REGISTRY, call=self_referential)])
        with patch("utils.llm.report.try_decode_inner_calldata", return_value=self_referential):
            flow = format_call_flow(ctx)
        self.assertEqual(flow.count("↳"), MAX_BYTES_RECURSION_DEPTH)

    def test_empty_without_entries(self) -> None:
        self.assertEqual(format_call_flow(_add_farms_ctx(entries=[])), "")


class TestCompositeParams(unittest.TestCase):
    """Addresses nested in tuple/struct args must still render as explorer links."""

    def _flow(self, signature: str, params: list) -> str:
        call = DecodedCall(function_name=signature.split("(")[0], signature=signature, params=params)
        return format_call_flow(_add_farms_ctx(entries=[CallEntry(target=REGISTRY, call=call)]))

    def test_type_decomposition(self) -> None:
        self.assertEqual(array_element_type("uint256[3]"), "uint256")
        self.assertEqual(array_element_type("(address,uint256)[]"), "(address,uint256)")
        self.assertIsNone(array_element_type("address"))
        self.assertEqual(tuple_component_types("(address,uint256)"), ["address", "uint256"])
        self.assertEqual(tuple_component_types("(address,(address,uint256))"), ["address", "(address,uint256)"])
        self.assertIsNone(tuple_component_types("address[]"))

    def test_tuple_addresses_linked(self) -> None:
        flow = self._flow("configure((address,uint256))", [("(address,uint256)", (FARM, 5))])
        self.assertIn(f"     - `address`: [`{FARM_CKS}`](https://etherscan.io/address/{FARM_CKS})", flow)
        self.assertIn("     - `uint256`: `5`", flow)
        self.assertNotIn(FARM, flow)  # no raw lowercase tuple dump

    def test_array_of_tuples_indexed(self) -> None:
        flow = self._flow("setCaps((address,uint256)[])", [("(address,uint256)[]", ((FARM, 5), (REGISTRY, 9)))])
        self.assertIn("     - `[0]`:", flow)
        self.assertIn("     - `[1]`:", flow)
        self.assertIn(f"https://etherscan.io/address/{FARM_CKS}", flow)
        self.assertIn(f"https://etherscan.io/address/{REGISTRY}", flow)

    def test_nested_tuple_addresses_linked(self) -> None:
        flow = self._flow("init((address,(address,uint256)))", [("(address,(address,uint256))", (REGISTRY, (FARM, 1)))])
        self.assertIn(f"https://etherscan.io/address/{FARM_CKS}", flow)

    def test_scalar_array_rendering_unchanged(self) -> None:
        """Plain address[] keeps its compact, index-free bullets."""
        flow = self._flow("addFarms(address[])", [("address[]", (FARM,))])
        self.assertIn(f"     - [`{FARM_CKS}`](https://etherscan.io/address/{FARM_CKS})", flow)
        self.assertNotIn("`[0]`", flow)

    def test_arity_mismatch_falls_back_to_scalar(self) -> None:
        """A value that doesn't match its tuple type is printed, not crashed on."""
        flow = self._flow("configure((address,uint256))", [("(address,uint256)", (FARM,))])
        self.assertIn("`(address,uint256)`:", flow)

    def test_iter_address_values_walks_composites(self) -> None:
        self.assertEqual(list(iter_address_values("(address,uint256)", (FARM, 5))), [FARM])
        self.assertEqual(list(iter_address_values("(address,uint256)[]", ((FARM, 5), (REGISTRY, 9)))), [FARM, REGISTRY])
        self.assertEqual(list(iter_address_values("uint256", 5)), [])
        self.assertEqual(list(iter_address_values("address", FARM)), [FARM])


class TestAmountAnnotation(unittest.TestCase):
    """Raw amounts get a human-readable hint when the target's token is known."""

    JANE = RelatedToken(getter="jane", address="0x333333330522F64EE8d0b3039c460b41670e3404", symbol="JANE", decimals=18)

    def _flow(self, params: list, token: RelatedToken | None) -> str:
        call = DecodedCall(
            function_name="setEpochEmissions",
            signature="setEpochEmissions(uint256,uint256)",
            params=params,
        )
        entry = CallEntry(target=REGISTRY, call=call, param_names=["_epoch", "emissions"], amount_token=token)
        return format_call_flow(_add_farms_ctx(entries=[entry]))

    def test_large_uint_annotated(self) -> None:
        flow = self._flow([("uint256", 43), ("uint256", 5499673832374850402183062)], self.JANE)
        self.assertIn("`5,499,673,832,374,850,402,183,062` (≈ 5,499,673 JANE)", flow)

    def test_small_uint_not_annotated(self) -> None:
        """An epoch number must not be rendered as 0.000000000000000043 JANE."""
        flow = self._flow([("uint256", 43), ("uint256", 5369214230155537376952673)], self.JANE)
        self.assertIn("- `uint256 _epoch`: `43`\n", flow)

    def test_no_annotation_without_token(self) -> None:
        flow = self._flow([("uint256", 43), ("uint256", 5369214230155537376952673)], None)
        self.assertNotIn("≈", flow)

    def test_sub_token_amount_keeps_one_truncated_decimal(self) -> None:
        usdc = RelatedToken(getter="self", address=REGISTRY, symbol="USDC", decimals=6)
        self.assertIn("(≈ 0.5 USDC)", self._flow([("uint256", 590_000)], usdc))
        self.assertNotIn("≈", self._flow([("uint256", 99_999)], usdc))
        self.assertIn("(≈ 1 USDC)", self._flow([("uint256", 1_000_000)], usdc))

    def test_non_uint_types_untouched(self) -> None:
        call = DecodedCall(function_name="setRoot", signature="setRoot(bytes32)", params=[("bytes32", b"\\x01" * 32)])
        entry = CallEntry(target=REGISTRY, call=call, amount_token=self.JANE)
        self.assertNotIn("≈", format_call_flow(_add_farms_ctx(entries=[entry])))


class TestBuildTitle(unittest.TestCase):
    NOW = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)

    def test_contract_timestamp_and_risk(self) -> None:
        title = build_title(_add_farms_ctx(), "LOW", now=self.NOW)
        self.assertEqual(title, "Infinifi Shorttimelock - 11/08/2026 10:00 - LOW")

    def test_risk_omitted_when_unknown(self) -> None:
        self.assertEqual(build_title(_add_farms_ctx(), now=self.NOW), "Infinifi Shorttimelock - 11/08/2026 10:00")

    def test_falls_back_to_protocol_then_fallback(self) -> None:
        self.assertEqual(
            build_title(_add_farms_ctx(label=""), "LOW", now=self.NOW), "INFINIFI - 11/08/2026 10:00 - LOW"
        )
        self.assertEqual(
            build_title(_add_farms_ctx(label="", protocol=""), "LOW", now=self.NOW, fallback="AI Report"),
            "AI Report - 11/08/2026 10:00 - LOW",
        )


class TestReferenceTable(unittest.TestCase):
    def test_renders_executor_target_argument_and_protocol_context(self) -> None:
        ctx = _add_farms_ctx(
            labels={REGISTRY: "FarmRegistry", FARM_CKS: "New Silver 2 Senior", DROP: "New Silver Series 2 DROP"},
            label_address=TIMELOCK,
            related_addresses=[FARM, DROP],
        )

        table = format_reference_table(ctx)

        self.assertIn("| Address | Label | Role | Description |", table)
        self.assertIn(f"| [`{TIMELOCK}`](https://etherscan.io/address/{TIMELOCK}) | Infinifi Shorttimelock |", table)
        self.assertIn(
            f"| [`{REGISTRY}`](https://etherscan.io/address/{REGISTRY}) | FarmRegistry | Call target |", table
        )
        self.assertIn("Receives `addFarms(uint256,address[])`", table)
        self.assertIn("New Silver 2 Senior | Calldata argument; Protocol context", table)
        self.assertIn("Passed as `_farms` to `addFarms(uint256,address[])`", table)
        self.assertIn("New Silver Series 2 DROP | Protocol context", table)

    def test_deduplicates_repeated_addresses_and_descriptions(self) -> None:
        call = _add_farms_ctx().entries[0]
        ctx = _add_farms_ctx(entries=[call, call], related_addresses=[REGISTRY])

        table = format_reference_table(ctx)

        self.assertEqual(table.count(f"| [`{REGISTRY}`](https://etherscan.io/address/{REGISTRY})"), 1)
        self.assertEqual(table.count("Receives `addFarms(uint256,address[])`"), 1)

    def test_escapes_dynamic_table_text(self) -> None:
        ctx = _add_farms_ctx(labels={REGISTRY: "Farm | Registry\nMain"})
        self.assertIn("Farm \\| Registry Main", format_reference_table(ctx))

    def test_includes_addresses_from_nested_calldata(self) -> None:
        transfer = "0xa9059cbb" + FARM[2:].zfill(64) + f"{1:064x}"
        outer = DecodedCall(function_name="execute", signature="execute(bytes)", params=[("bytes", transfer)])
        ctx = _add_farms_ctx(entries=[CallEntry(target=REGISTRY, call=outer)])

        table = format_reference_table(ctx)

        self.assertIn(f"[`{FARM_CKS}`](https://etherscan.io/address/{FARM_CKS})", table)
        self.assertIn("to `transfer(address,uint256)`", table)

    def test_empty_without_addresses(self) -> None:
        self.assertEqual(format_reference_table(ReportContext()), "")


class TestBuildReport(unittest.TestCase):
    def test_sections_in_order(self) -> None:
        report = build_report("Registers a type-2 farm.", "Long analysis.", _add_farms_ctx(), "MEDIUM")
        self.assertLess(report.index("## Summary"), report.index("## Call Flow"))
        self.assertLess(report.index("## Call Flow"), report.index("## Reference"))
        self.assertLess(report.index("## Reference"), report.index("## Analysis"))

    def test_protocol_context_is_deterministic_section_before_analysis(self) -> None:
        ctx = _add_farms_ctx(protocol_context="- **Farm:** New Silver 2 Senior")
        report = build_report("Updates the rate.", "Long analysis.", ctx, "LOW")
        self.assertIn("## Protocol Context\n\n- **Farm:** New Silver 2 Senior", report)
        self.assertLess(report.index("## Call Flow"), report.index("## Protocol Context"))
        self.assertLess(report.index("## Protocol Context"), report.index("## Reference"))
        self.assertLess(report.index("## Reference"), report.index("## Analysis"))

    def test_metadata_header(self) -> None:
        report = build_report("Summary.", "Analysis.", _add_farms_ctx(label_address=TIMELOCK), "HIGH")
        self.assertIn("- **Protocol:** INFINIFI", report)
        self.assertIn(
            f"- **Contract:** Infinifi Shorttimelock — [`{TIMELOCK}`](https://etherscan.io/address/{TIMELOCK})",
            report,
        )
        self.assertIn("- **Chain:** Mainnet (chain id 1)", report)
        self.assertIn("- **Risk:** HIGH", report)

    def test_contract_unlinked_without_label_address(self) -> None:
        report = build_report("Summary.", "Analysis.", _add_farms_ctx())
        self.assertIn("- **Contract:** Infinifi Shorttimelock\n", report)

    def test_contract_unlinked_on_chain_without_explorer(self) -> None:
        ctx = _add_farms_ctx(chain_id=999999, label_address=TIMELOCK)
        self.assertIn("- **Contract:** Infinifi Shorttimelock\n", build_report("S.", "A.", ctx))

    def test_redundant_analysis_heading_stripped(self) -> None:
        report = build_report("Summary.", "## Detailed Analysis\n\nThe call registers a farm.", _add_farms_ctx())
        self.assertEqual(report.count("Analysis"), 1)
        self.assertIn("The call registers a farm.", report)

    def test_own_subheadings_kept(self) -> None:
        report = build_report("Summary.", "### Call Breakdown\n\nDetails.", _add_farms_ctx())
        self.assertIn("### Call Breakdown", report)

    def test_risk_omitted_when_unknown(self) -> None:
        self.assertNotIn("**Risk:**", build_report("Summary.", "Analysis.", _add_farms_ctx()))

    def test_empty_without_content(self) -> None:
        self.assertEqual(build_report("", "", _add_farms_ctx()), "")


if __name__ == "__main__":
    unittest.main()

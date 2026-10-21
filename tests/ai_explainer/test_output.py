"""Tests for explainer output."""

import unittest
from unittest.mock import MagicMock, patch

from utils.llm.ai_explainer import (
    Explanation,
    _explanation_from_json,
    _generate_explanation,
    _parse_explanation,
    _split_risk_tag,
    format_explanation_line,
)
from utils.llm.base import LLMError, LLMProvider, LLMUsage
from utils.llm.fallback import FallbackProvider
from utils.llm.report import ReportContext


class TestStructuredOutput(unittest.TestCase):
    """Tests for the structured-output draft path and JSON→Explanation mapping."""

    def test_appends_risk_tag_when_missing(self) -> None:
        exp = _explanation_from_json({"summary": "Pauses the vault", "detail": "d", "risk_tag": "MEDIUM"})
        self.assertEqual(exp.summary, "Pauses the vault MEDIUM")
        self.assertEqual(exp.detail, "d")

    def test_normalizes_matching_trailing_tag(self) -> None:
        exp = _explanation_from_json({"summary": "Pauses the vault. LOW.", "detail": "d", "risk_tag": "LOW"})
        self.assertEqual(exp.summary, "Pauses the vault. LOW")

    def test_schema_tag_overrides_inlined_tag(self) -> None:
        # Model put LOW in the prose but the validated risk_tag is HIGH — schema wins.
        exp = _explanation_from_json({"summary": "Grants admin role. LOW.", "detail": "d", "risk_tag": "HIGH"})
        self.assertEqual(exp.summary, "Grants admin role. HIGH")

    def test_strips_colon_lead_in_before_tag(self) -> None:
        # The report shows the prose without the tag; a colon must not be left dangling.
        prose, tag = _split_risk_tag("Swaps the merkle root. Combined mint schedule plus tree replacement: MEDIUM")
        self.assertEqual(prose, "Swaps the merkle root. Combined mint schedule plus tree replacement.")
        self.assertEqual(tag, "MEDIUM")

    def test_strips_risk_label_lead_in(self) -> None:
        self.assertEqual(_split_risk_tag("Pauses the vault. Risk: LOW."), ("Pauses the vault.", "LOW"))
        self.assertEqual(_split_risk_tag("Pauses the vault — HIGH"), ("Pauses the vault.", "HIGH"))

    def test_keeps_risk_word_inside_prose(self) -> None:
        self.assertEqual(_split_risk_tag("Carries low risk: LOW"), ("Carries low risk.", "LOW"))

    def test_colon_lead_in_normalized_with_schema_tag(self) -> None:
        exp = _explanation_from_json({"summary": "Rotates the root: LOW", "detail": "", "risk_tag": "MEDIUM"})
        self.assertEqual(exp.summary, "Rotates the root. MEDIUM")


class TestParseExplanation(unittest.TestCase):
    """Tests for _parse_explanation."""

    def test_heading_containing_keyword_is_not_a_marker(self) -> None:
        """'## Detailed Analysis' must not match the DETAIL marker and get sliced."""
        raw = "## Detailed Analysis\n\nThe call registers a farm."
        result = _parse_explanation(raw)
        self.assertEqual(result.detail, "")
        self.assertIn("Detailed Analysis", result.summary)
        self.assertIn("registers a farm", result.summary)

    def test_both_sections(self) -> None:
        raw = "TLDR: Short summary here.\n\nDETAIL:\nDetailed analysis here."
        result = _parse_explanation(raw)
        self.assertEqual(result.summary, "Short summary here.")
        self.assertEqual(result.detail, "Detailed analysis here.")

    def test_tldr_only(self) -> None:
        raw = "TLDR: Just a summary, no detail."
        result = _parse_explanation(raw)
        self.assertEqual(result.summary, "Just a summary, no detail.")
        self.assertEqual(result.detail, "")

    def test_no_markers_fallback(self) -> None:
        raw = "This is a plain response without markers."
        result = _parse_explanation(raw)
        self.assertEqual(result.summary, "This is a plain response without markers.")
        self.assertEqual(result.detail, "")

    def test_case_insensitive(self) -> None:
        raw = "tldr: Lower case markers.\n\ndetail:\nLower case detail."
        result = _parse_explanation(raw)
        self.assertEqual(result.summary, "Lower case markers.")
        self.assertEqual(result.detail, "Lower case detail.")

    def test_multiline_detail(self) -> None:
        raw = "TLDR: Summary.\n\nDETAIL:\nLine 1.\nLine 2.\n- Risk: HIGH"
        result = _parse_explanation(raw)
        self.assertEqual(result.summary, "Summary.")
        self.assertIn("Line 1.", result.detail)
        self.assertIn("Risk: HIGH", result.detail)


class TestFormatExplanationLine(unittest.TestCase):
    """Tests for format_explanation_line."""

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="https://gist.wavey.info/abc123")
    def test_report_published_when_present(self, mock_gist: MagicMock) -> None:
        """The full report (metadata + call flow + analysis) is what gets uploaded."""
        explanation = Explanation(
            summary="Pauses the vault. HIGH",
            detail="Full detail here.",
            report="## Call Flow\n\n1. pause()",
            title="Yearn Timelock - 11/08/2026 10:00 - HIGH",
        )
        result = format_explanation_line(explanation)
        mock_gist.assert_called_once_with(explanation.report, title=explanation.title)
        self.assertIn("https://gist.wavey.info/abc123", result)

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="https://gist.wavey.info/abc123")
    def test_format_with_detail(self, mock_gist: MagicMock) -> None:
        explanation = Explanation(summary="This pauses the protocol.", detail="Full detail here.")
        result = format_explanation_line(explanation)
        self.assertIn("AI Summary", result)
        self.assertIn("This pauses the protocol.", result)
        self.assertNotIn("Full detail here.", result)
        self.assertIn("https://gist.wavey.info/abc123", result)
        self.assertIn("Full details", result)
        mock_gist.assert_called_once_with("Full detail here.", title="AI Transaction Analysis")

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="")
    def test_format_gist_failure(self, mock_gist: MagicMock) -> None:
        """If gist upload fails, surface a notice instead of a link."""
        explanation = Explanation(summary="This pauses the protocol.", detail="Full detail here.")
        result = format_explanation_line(explanation)
        self.assertIn("AI Summary", result)
        self.assertIn("This pauses the protocol.", result)
        self.assertNotIn("Full details", result)
        self.assertIn("Couldn't post full report", result)

    def test_format_no_detail(self) -> None:
        """If there's no detail and no report, no gist upload is attempted."""
        explanation = Explanation(summary="This pauses the protocol.", detail="")
        result = format_explanation_line(explanation)
        self.assertIn("AI Summary", result)
        self.assertIn("This pauses the protocol.", result)
        self.assertNotIn("Full details", result)

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="https://gist.wavey.info/abc123")
    def test_report_only_uploads_without_detail(self, mock_gist: MagicMock) -> None:
        explanation = Explanation(
            summary="Could not decode 3 calls in this batch.",
            detail="",
            report="## Call Flow\n\n1. **Undecoded calldata**",
            title="Infinifi Shorttimelock - 11/08/2026 10:00",
        )
        result = format_explanation_line(explanation)
        mock_gist.assert_called_once_with(explanation.report, title=explanation.title)
        self.assertIn("Full details", result)
        self.assertIn("https://gist.wavey.info/abc123", result)

    @patch("utils.llm.ai_explainer._spill_unpublished_report", return_value="/tmp/report.md")
    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="")
    def test_report_only_upload_failure_spills(self, mock_gist: MagicMock, mock_spill: MagicMock) -> None:
        explanation = Explanation(
            summary="Could not decode 3 calls in this batch.",
            detail="",
            report="## Call Flow\n\n1. **Undecoded calldata**",
            title="Infinifi Shorttimelock - 11/08/2026 10:00",
        )
        result = format_explanation_line(explanation)
        mock_gist.assert_called_once()
        mock_spill.assert_called_once()
        self.assertIn("Couldn't post full report", result)


class TestUnpublishedReportSpill(unittest.TestCase):
    """A report that can't reach the gist is written to CACHE_DIR.

    It exists only in memory at that point, so without this the LLM output is
    lost and a recovery means regenerating it against a chain state that has
    since moved.
    """

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="")
    def test_failed_upload_spills_report_to_disk(self, _mock_gist: MagicMock) -> None:
        import os

        from utils.cache import cache_path
        from utils.llm.ai_explainer import UNPUBLISHED_REPORTS_DIRNAME, Explanation, format_explanation_line

        explanation = Explanation(
            summary="Grants mint rights.",
            detail="Full detail here.",
            report="# Call flow\n\nEverything worth keeping.",
            title="InfiniFi LongTimelock - MEDIUM",
        )
        result = format_explanation_line(explanation)
        self.assertIn("Couldn't post full report", result)

        directory = cache_path(UNPUBLISHED_REPORTS_DIRNAME)
        spilled = os.listdir(directory)
        self.assertEqual(len(spilled), 1)
        contents = open(os.path.join(directory, spilled[0])).read()
        self.assertIn("Everything worth keeping.", contents)
        self.assertIn("InfiniFi LongTimelock - MEDIUM", contents)

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="https://gist.wavey.info/abc123")
    def test_successful_upload_spills_nothing(self, _mock_gist: MagicMock) -> None:
        import os

        from utils.cache import cache_path
        from utils.llm.ai_explainer import UNPUBLISHED_REPORTS_DIRNAME, Explanation, format_explanation_line

        format_explanation_line(Explanation(summary="ok", detail="d", report="r"))
        self.assertFalse(os.path.exists(cache_path(UNPUBLISHED_REPORTS_DIRNAME)))

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="")
    @patch("utils.llm.ai_explainer.os.makedirs", side_effect=OSError("read-only filesystem"))
    def test_spill_failure_still_returns_alert_line(self, _mock_mkdir: MagicMock, _mock_gist: MagicMock) -> None:
        """A failed spill must never take down the alert, which still has the summary."""
        from utils.llm.ai_explainer import Explanation, format_explanation_line

        result = format_explanation_line(Explanation(summary="Grants mint rights.", detail="d", report="r"))
        self.assertIn("Grants mint rights.", result)
        self.assertIn("Couldn't post full report", result)


class TestTextRefineDetailBudget(unittest.TestCase):
    def test_immediate_pass_keeps_detail_without_extra_call(self) -> None:
        provider = MagicMock(model_name="test")
        provider.supports_structured_output = False
        provider.complete.side_effect = [
            "TLDR: original. LOW.\n\nDETAIL:\nOriginal analysis.",
            "PASS",
        ]
        result = _generate_explanation(provider, "prompt", refine=True)
        self.assertEqual(provider.complete.call_count, 2)
        self.assertEqual(result.detail, "Original analysis.")
        self.assertIn("original. LOW", result.summary)

    def test_multiple_revisions_regenerate_detail_once(self) -> None:
        provider = MagicMock(model_name="test")
        provider.supports_structured_output = False
        provider.complete.side_effect = [
            "TLDR: original. LOW.\n\nDETAIL:\nOriginal analysis.",
            "TLDR: revised once. LOW.",
            "TLDR: revised twice. LOW.",
            "PASS",
            "Fresh detail from the final summary.",
        ]
        result = _generate_explanation(provider, "prompt", refine=True)
        self.assertEqual(provider.complete.call_count, 5)
        self.assertIn("revised twice. LOW", result.summary)
        self.assertEqual(result.detail, "Fresh detail from the final summary.")
        self.assertNotIn("Original analysis", result.detail)

    def test_expansion_failure_keeps_revised_summary_discards_stale_detail(self) -> None:

        provider = MagicMock(model_name="test")
        provider.supports_structured_output = False
        provider.complete.side_effect = [
            "TLDR: original. LOW.\n\nDETAIL:\nOriginal analysis.",
            "TLDR: revised. LOW.",
            "PASS",
            LLMError("boom"),
        ]
        result = _generate_explanation(provider, "prompt", refine=True)
        self.assertIn("revised. LOW", result.summary)
        self.assertEqual(result.detail, "")


class _UsageProvider(LLMProvider):
    """A text provider that reports a fixed usage per call, like a real provider; fails after ``fail_after`` calls."""

    def __init__(self, responses: list[str], fail_after: int | None = None, *, model_name: str = "test") -> None:
        self.responses = responses
        self.fail_after = fail_after
        self.calls = 0
        self._model_name = model_name

    def complete(self, prompt: str, system_prompt: str = "") -> str:
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise LLMError("boom")
        self.calls += 1
        self.last_usage = LLMUsage(1000 * self.calls, 900, None, 100, 0.5)
        return self.responses.pop(0)

    @property
    def model_name(self) -> str:
        return self._model_name


class TestStageUsage(unittest.TestCase):
    """Each LLM call is recorded with its stage, outcome and usage."""

    def test_summary_critique_rounds_and_detail_are_recorded(self) -> None:
        provider = _UsageProvider(
            [
                "TLDR: Pauses the vault. LOW\nDETAIL: d",
                "TLDR: Pauses the vault and blocks deposits. MEDIUM",
                "PASS",
                "The vault is paused.",
            ]
        )
        explanation = _generate_explanation(provider, "prompt", refine=True)
        stages = [(s.stage, s.outcome) for s in explanation.stages]
        self.assertEqual(
            stages, [("summary", "ok"), ("critique-1", "revised"), ("critique-2", "pass"), ("detail", "ok")]
        )
        self.assertEqual(explanation.stages[1].summary, "Pauses the vault and blocks deposits. MEDIUM")
        self.assertEqual([s.usage.input_tokens for s in explanation.stages if s.usage], [1000, 2000, 3000, 4000])
        self.assertEqual([s.model_name for s in explanation.stages], ["test"] * 4)

    def test_failed_call_reports_no_usage(self) -> None:
        provider = _UsageProvider(["TLDR: Pauses the vault. LOW\nDETAIL: d"], fail_after=1)
        explanation = _generate_explanation(provider, "prompt", refine=True)
        self.assertEqual(
            [(s.stage, s.outcome) for s in explanation.stages], [("summary", "ok"), ("critique-1", "error")]
        )
        # The failed call keeps no usage: the provider's last_usage still belongs to the summary call.
        self.assertIsNone(explanation.stages[1].usage)
        self.assertEqual(explanation.stages[1].model_name, "test")


class TestGenerationMetadata(unittest.TestCase):
    """Published reports identify all answering models and total generation time."""

    def test_empty_structured_summary_model_is_omitted_but_usage_is_retained(self) -> None:
        cli = MagicMock(spec=LLMProvider, model_name="empty-cli", supports_structured_output=True)
        cli.complete_structured.return_value = {"summary": "", "risk_tag": "LOW"}
        cli.complete.side_effect = LLMError("text unavailable")
        cli.last_usage = LLMUsage(2000, 900, None, 100, 0.5)
        api = _UsageProvider(["TLDR: Pauses the vault. LOW", "Detailed analysis."], model_name="api")
        provider = FallbackProvider([cli, api])

        explanation = _generate_explanation(provider, "p", report_ctx=ReportContext(chain_id=1))

        self.assertIn("- **Model:** api\n", explanation.report)
        self.assertNotIn("empty-cli", explanation.report)
        self.assertEqual(explanation.stages[0].outcome, "empty")
        self.assertEqual(explanation.stages[0].model_name, "empty-cli")
        self.assertEqual(explanation.stages[0].usage, cli.last_usage)

    @patch("utils.llm.ai_explainer.perf_counter")
    def test_generation_time_is_logged_for_successful_and_empty_summaries(self, clock: MagicMock) -> None:
        for response in ("TLDR: Pauses the vault. LOW\nDETAIL: Detail.", "   "):
            with self.subTest(response=response):
                clock.return_value = 100.0

                def complete(prompt: str, system_prompt: str = "") -> str:
                    clock.return_value = 110.0
                    return response

                provider = MagicMock(
                    spec=LLMProvider, model_name="api", supports_structured_output=False, last_usage=None
                )
                provider.complete.side_effect = complete
                with self.assertLogs("utils.llm.ai_explainer", level="INFO") as logs:
                    explanation = _generate_explanation(provider, "p", report_ctx=ReportContext(chain_id=1))
                self.assertIn("generation time=10.0s", "\n".join(logs.output))
                if explanation.summary:
                    self.assertIn("- **Time:** 10.0s", explanation.report)

    @patch("utils.llm.ai_explainer.upload_to_gist", return_value="https://gist.wavey.info/abc123")
    def test_fallback_models_and_total_time_reach_gist(self, gist: MagicMock) -> None:
        primary = _UsageProvider(
            ["TLDR: Pauses the vault. LOW\nDETAIL: Initial detail."], fail_after=1, model_name="primary"
        )
        fallback = _UsageProvider(
            ["TLDR: Pauses the vault and blocks deposits. MEDIUM", "PASS", "Final detail."], model_name="fallback"
        )
        unused = _UsageProvider([], model_name="unused")
        provider = FallbackProvider([primary, fallback, unused])

        with self.assertLogs("utils.llm.ai_explainer", level="INFO") as logs:
            explanation = _generate_explanation(provider, "prompt", refine=True, report_ctx=ReportContext(chain_id=1))
        format_explanation_line(explanation)

        body = gist.call_args.args[0]
        self.assertIn("- **Chain:** Mainnet (chain id 1)", body)
        self.assertIn("- **Risk:** MEDIUM\n- **Model:** primary / fallback\n- **Time:**", body)
        self.assertRegex(body, r"- \*\*Time:\*\* \d+\.\ds")
        self.assertNotIn("unused", body)
        self.assertIn("Final detail.", body)
        self.assertEqual([stage.model_name for stage in explanation.stages], ["primary"] + ["fallback"] * 3)
        output = "\n".join(logs.output)
        self.assertIn("summary ok model=primary in=1000 cached=900 out=100", output)
        self.assertIn("critique-1 revised model=fallback in=1000 cached=900 out=100", output)
        self.assertIn("generation time=", output)
        self.assertEqual(sum("LLM usage:" in line for line in logs.output), 1)
        self.assertNotIn("LLM stage=", output)


class TestEmptyCritique(unittest.TestCase):
    def test_blank_critique_keeps_the_draft_and_is_not_a_pass(self) -> None:
        provider = _UsageProvider(["TLDR: Pauses the vault. LOW\nDETAIL: d", "   "])
        explanation = _generate_explanation(provider, "prompt", refine=True)
        self.assertEqual(explanation.summary, "Pauses the vault. LOW")
        self.assertEqual(
            [(s.stage, s.outcome) for s in explanation.stages], [("summary", "ok"), ("critique-1", "empty")]
        )

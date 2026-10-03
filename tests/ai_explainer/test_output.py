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
from utils.llm.base import LLMError


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
        provider = MagicMock()
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
        provider = MagicMock()
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

        provider = MagicMock()
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

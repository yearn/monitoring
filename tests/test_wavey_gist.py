"""Tests for utils/wavey_gist.py."""

import unittest
from unittest.mock import MagicMock, patch

import requests

from utils.wavey_gist import DEFAULT_GIST_TITLE, PRIMARY_FILE_NAME, upload_to_gist


def _mock_response(*, post_json: dict | None = None) -> MagicMock:
    """Build a mock Wavey Gist response."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = post_json or {"url": "https://gist.wavey.info/abc123", "id": "abc123"}
    return response


class TestUploadToGist(unittest.TestCase):
    """Tests for upload_to_gist."""

    def test_empty_content_returns_empty(self) -> None:
        self.assertEqual(upload_to_gist(""), "")

    @patch.dict("utils.wavey_gist.os.environ", {"WAVEY_GIST_API_KEY": "test-key"})
    @patch("utils.wavey_gist.requests.post")
    def test_successful_upload(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response()

        result = upload_to_gist("Some **markdown**", title="Test")
        self.assertEqual(result, "https://gist.wavey.info/abc123")

        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["title"], "Test")
        # Wavey Gist now expects a `files` snapshot — the legacy `markdown` field
        # is rejected with HTTP 400. README.md is the preferred primary filename
        # (https://gist.wavey.info/llms.txt).
        self.assertNotIn("markdown", payload)
        self.assertEqual(payload["files"][PRIMARY_FILE_NAME]["content"], "# Test\n\nSome **markdown**")
        self.assertEqual(mock_post.call_args[1]["headers"]["Authorization"], "Bearer test-key")

    @patch.dict("utils.wavey_gist.os.environ", {"WAVEY_GIST_API_KEY": "test-key"})
    @patch("utils.wavey_gist.requests.post")
    def test_no_title_sends_raw_content(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response()

        upload_to_gist("Content only")
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["title"], DEFAULT_GIST_TITLE)
        self.assertEqual(payload["files"][PRIMARY_FILE_NAME]["content"], "Content only")

    @patch.dict("utils.wavey_gist.os.environ", {}, clear=True)
    @patch("utils.wavey_gist.requests.post")
    def test_missing_api_key_returns_empty(self, mock_post: MagicMock) -> None:
        self.assertEqual(upload_to_gist("x"), "")
        mock_post.assert_not_called()

    @patch.dict("utils.wavey_gist.os.environ", {"WAVEY_GIST_API_KEY": "test-key"})
    @patch("utils.wavey_gist.requests.post")
    def test_missing_url_returns_empty(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(post_json={"id": "abc123"})
        self.assertEqual(upload_to_gist("x"), "")

    @patch.dict("utils.wavey_gist.os.environ", {"WAVEY_GIST_API_KEY": "test-key"})
    @patch("utils.wavey_gist.requests.post")
    def test_http_error_returns_empty(self, mock_post: MagicMock) -> None:
        # E.g. the legacy `markdown` field — the API now rejects unknown fields
        # with HTTP 400. The function must keep alerting and return "" so the
        # caller can fall back to the in-Telegram summary.
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("400 Client Error")
        mock_post.return_value = mock_response
        self.assertEqual(upload_to_gist("x"), "")

    @patch.dict("utils.wavey_gist.os.environ", {"WAVEY_GIST_API_KEY": "test-key"})
    @patch("utils.wavey_gist.requests.post")
    def test_request_failure_returns_empty(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = requests.RequestException("Connection error")
        self.assertEqual(upload_to_gist("x"), "")


if __name__ == "__main__":
    unittest.main()

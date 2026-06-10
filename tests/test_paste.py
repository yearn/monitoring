"""Tests for utils/paste.py."""

import unittest
from unittest.mock import MagicMock, patch

import requests

from utils.paste import upload_to_paste


def _mock_response(*, post_json: dict | None = None) -> MagicMock:
    """Build a mock Wavey Gist response."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = post_json or {"url": "https://gist.wavey.info/abc123", "id": "abc123"}
    return response


class TestUploadToPaste(unittest.TestCase):
    """Tests for upload_to_paste."""

    def test_empty_content_returns_empty(self) -> None:
        self.assertEqual(upload_to_paste(""), "")

    @patch.dict("utils.paste.os.environ", {"WAVEY_GIST_API_KEY": "test-key"})
    @patch("utils.paste.requests.post")
    def test_successful_upload(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response()

        result = upload_to_paste("Some **markdown**", title="Test")
        self.assertEqual(result, "https://gist.wavey.info/abc123")

        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["title"], "Test")
        self.assertEqual(payload["markdown"], "# Test\n\nSome **markdown**")
        self.assertEqual(mock_post.call_args[1]["headers"]["Authorization"], "Bearer test-key")

    @patch.dict("utils.paste.os.environ", {"WAVEY_GIST_API_KEY": "test-key"})
    @patch("utils.paste.requests.post")
    def test_no_title_sends_raw_content(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response()

        upload_to_paste("Content only")
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["title"], "Monitoring Details")
        self.assertEqual(payload["markdown"], "Content only")

    @patch.dict("utils.paste.os.environ", {}, clear=True)
    @patch("utils.paste.requests.post")
    def test_missing_api_key_returns_empty(self, mock_post: MagicMock) -> None:
        self.assertEqual(upload_to_paste("x"), "")
        mock_post.assert_not_called()

    @patch.dict("utils.paste.os.environ", {"WAVEY_GIST_API_KEY": "test-key"})
    @patch("utils.paste.requests.post")
    def test_missing_url_returns_empty(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _mock_response(post_json={"id": "abc123"})
        self.assertEqual(upload_to_paste("x"), "")

    @patch.dict("utils.paste.os.environ", {"WAVEY_GIST_API_KEY": "test-key"})
    @patch("utils.paste.requests.post")
    def test_request_failure_returns_empty(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = requests.RequestException("Connection error")
        self.assertEqual(upload_to_paste("x"), "")


if __name__ == "__main__":
    unittest.main()

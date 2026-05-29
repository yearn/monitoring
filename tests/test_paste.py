"""Tests for utils/paste.py."""

import unittest
from unittest.mock import MagicMock, patch

import requests

from utils.paste import upload_to_paste


def _mock_session(*, csrftoken: str = "tok", post_json: dict | None = None) -> MagicMock:
    """Build a mock requests.Session for the rentry CSRF flow."""
    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    session.cookies.get.return_value = csrftoken

    get_resp = MagicMock()
    get_resp.raise_for_status.return_value = None
    session.get.return_value = get_resp

    post_resp = MagicMock()
    post_resp.raise_for_status.return_value = None
    post_resp.json.return_value = post_json or {"status": "200", "url": "https://rentry.co/abc123"}
    session.post.return_value = post_resp
    return session


class TestUploadToPaste(unittest.TestCase):
    """Tests for upload_to_paste."""

    def test_empty_content_returns_empty(self) -> None:
        self.assertEqual(upload_to_paste(""), "")

    @patch("utils.paste.requests.Session")
    def test_successful_upload(self, mock_session_cls: MagicMock) -> None:
        session = _mock_session()
        mock_session_cls.return_value = session

        result = upload_to_paste("Some **markdown**", title="Test")
        self.assertEqual(result, "https://rentry.co/abc123")

        # Title is prepended as a markdown heading, csrftoken echoed back.
        data = session.post.call_args[1]["data"]
        self.assertEqual(data["text"], "# Test\n\nSome **markdown**")
        self.assertEqual(data["csrfmiddlewaretoken"], "tok")
        self.assertEqual(session.post.call_args[1]["headers"]["Referer"], "https://rentry.co")

    @patch("utils.paste.requests.Session")
    def test_no_title_sends_raw_content(self, mock_session_cls: MagicMock) -> None:
        session = _mock_session()
        mock_session_cls.return_value = session

        upload_to_paste("Content only")
        self.assertEqual(session.post.call_args[1]["data"]["text"], "Content only")

    @patch("utils.paste.requests.Session")
    def test_missing_csrftoken_returns_empty(self, mock_session_cls: MagicMock) -> None:
        session = _mock_session(csrftoken=None)
        mock_session_cls.return_value = session

        self.assertEqual(upload_to_paste("x"), "")
        session.post.assert_not_called()

    @patch("utils.paste.requests.Session")
    def test_non_200_status_returns_empty(self, mock_session_cls: MagicMock) -> None:
        session = _mock_session(post_json={"status": "400", "errors": "bad request"})
        mock_session_cls.return_value = session

        self.assertEqual(upload_to_paste("x"), "")

    @patch("utils.paste.requests.Session")
    def test_request_failure_returns_empty(self, mock_session_cls: MagicMock) -> None:
        session = _mock_session()
        session.post.side_effect = requests.RequestException("Connection error")
        mock_session_cls.return_value = session

        self.assertEqual(upload_to_paste("Some content"), "")


if __name__ == "__main__":
    unittest.main()

from unittest.mock import patch

import requests

from protocols.compound.proposals import get_proposals
from utils.alert import AlertSeverity


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_compound_alert_uses_timeout_escapes_title_and_updates_reported_id():
    payload = {
        "data": {
            "proposals": {
                "nodes": [
                    {
                        "onchainId": "42",
                        "status": "queued",
                        "metadata": {"title": "# Update COMP_USDC *market*", "description": ""},
                    },
                    {
                        "onchainId": "41",
                        "status": "active",
                        "metadata": {"title": "Active proposal", "description": ""},
                    },
                ]
            }
        }
    }

    with (
        patch("protocols.compound.proposals.requests.post", return_value=_Response(payload)) as mock_post,
        patch("protocols.compound.proposals.get_last_queued_id_from_file", return_value=40),
        patch("protocols.compound.proposals.send_alert") as mock_send,
        patch("protocols.compound.proposals.write_last_queued_id_to_file") as mock_write,
    ):
        get_proposals()

    assert mock_post.call_args.kwargs["timeout"] == 30
    mock_send.assert_called_once()
    alert = mock_send.call_args.args[0]
    assert alert.protocol == "comp"
    assert alert.severity == AlertSeverity.LOW
    assert "Update COMP\\_USDC \\*market\\*" in alert.message
    assert "Active proposal" not in alert.message
    mock_write.assert_called_once_with("comp", 42)


def test_compound_processing_error_alert_uses_plain_text():
    with (
        patch("protocols.compound.proposals.requests.post", return_value=_Response({"data": {}})),
        patch("protocols.compound.proposals.send_error_message") as mock_send,
    ):
        get_proposals()

    mock_send.assert_called_once()
    message, protocol = mock_send.call_args.args
    assert message.startswith("Error processing compound proposals:")
    assert protocol == "comp"
    assert mock_send.call_args.kwargs == {}


def test_compound_fetch_error_alert_uses_plain_text():
    with (
        patch("protocols.compound.proposals.requests.post", side_effect=requests.Timeout("timed out")),
        patch("protocols.compound.proposals.send_error_message") as mock_send,
    ):
        get_proposals()

    mock_send.assert_called_once_with(
        "Failed to fetch compound proposals: timed out",
        "comp",
    )

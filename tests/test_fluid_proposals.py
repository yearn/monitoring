from unittest.mock import patch

from protocols.fluid.proposals import get_proposals


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fluid_proposal_alert_escapes_api_markdown_and_keeps_link():
    description = (
        "## Summary\n\n"
        "This proposal performs five actions on mainnet: (1) sets vault 142 "
        "(wstUSR / USDtb) wstUSR base withdrawal limit to **24** raw units; "
        "(2) temporarily raises borrow caps on wstUSR vaults **110**, **111**, "
        "**112**, and **133**, runs reserve rebalances on those vaults, then "
        "restores max-restricted (paused) borrow limits; (3) withdraws "
        "**750,000 FLUID** from Treasury to Team Multisig for upcoming rewards; "
        "(4) sets launch limits on PST TYPE_1 vaults **165-166**, TYPE_3 vault "
        "**168**, and PST-USDC DEX **45**."
        "\n\n## Code Changes\n\nMore details."
    )
    payload = {
        "data": [
            {
                "id": 131,
                "title": "wstUSR Vault Maintenance",
                "description": description,
                "queued_at": "2026-05-26T21:15:35.000+00:00",
            }
        ]
    }

    with (
        patch("protocols.fluid.proposals.requests.get", return_value=_Response(payload)),
        patch("protocols.fluid.proposals.get_last_queued_id_from_file", return_value=130),
        patch("protocols.fluid.proposals.write_last_queued_id_to_file") as mock_write,
        patch("protocols.fluid.proposals.send_telegram_message") as mock_send,
    ):
        get_proposals()

    mock_send.assert_called_once()
    message, protocol = mock_send.call_args.args
    assert protocol == "fluid"
    assert "[Proposal 131](https://fluid.io/gov/proposals/131)" in message
    assert "TYPE\\_1 vaults..." in message
    assert mock_send.call_args.kwargs == {}
    mock_write.assert_called_once_with("fluid", 131)


def test_fluid_proposal_fetch_error_routes_to_errors_channel():
    with (
        patch("protocols.fluid.proposals.requests.get", side_effect=Exception("bad TYPE_1 payload")),
        patch("protocols.fluid.proposals.send_error_message") as mock_send,
    ):
        get_proposals()

    mock_send.assert_called_once_with(
        "Error processing Fluid proposals: bad TYPE_1 payload",
        "fluid",
    )

from unittest.mock import patch

from aave.proposals import fetch_queued_proposals, handle_governance_proposals, has_pending_payload


def _payload(chain_id: int = 1, payload_id: int = 1) -> dict:
    return {
        "id": f"{chain_id}_{payload_id}",
        "chainId": str(chain_id),
        "payloadsController": "0xdabad81af85554e9ae636395611c58f7ec1aaec5",
    }


def _proposal(proposal_id: int, timestamp: int, title: str = "Test Proposal", payloads: list[dict] | None = None) -> dict:
    return {
        "proposalId": str(proposal_id),
        "proposalMetadata": {"title": title},
        "transactions": {"executed": {"timestamp": str(timestamp)}},
        "payloads": payloads or [_payload(payload_id=proposal_id)],
    }


def test_aave_fetches_executed_governance_proposals_with_payloads():
    payload = {"data": {"proposals": [_proposal(12, 1), _proposal(10, 1)]}}

    with patch("aave.proposals.run_query", return_value=payload) as mock_run_query:
        proposals = fetch_queued_proposals(9)

    query, variables = mock_run_query.call_args.args
    assert "state:$executedState" in query
    assert "orderDirection: desc" in query
    assert "first: 10" in query
    assert "payloads" in query
    assert variables == {"lastId": 9, "executedState": 4}
    assert [proposal["proposalId"] for proposal in proposals] == ["10", "12"]


def test_aave_handler_alerts_governance_executed_proposals_with_pending_payloads():
    proposals = [
        _proposal(489, 1_800_000_000 - 60, "Passed Proposal"),
        _proposal(488, 1_800_000_000 - 120, "Already Executed Proposal"),
    ]

    with (
        patch("aave.proposals.get_last_queued_id_from_file", return_value=487),
        patch("aave.proposals.fetch_queued_proposals", return_value=proposals),
        patch("aave.proposals.has_pending_payload", side_effect=[True, False]),
        patch("aave.proposals.send_telegram_message") as mock_send,
        patch("aave.proposals.write_last_queued_id_to_file") as mock_write,
    ):
        handle_governance_proposals()

    mock_send.assert_called_once()
    message, protocol = mock_send.call_args.args
    assert protocol == "aave"
    assert "Passed Proposal" in message
    assert "Already Executed Proposal" not in message
    mock_write.assert_called_once_with("aave", 489)


def test_aave_has_pending_payload_is_true_when_any_payload_is_not_executed():
    proposal = _proposal(489, 1)
    payload_states = [
        {"payloadId": "442", "chainId": "1", "state": "executed"},
        {"payloadId": "4", "chainId": "196", "state": "created"},
    ]

    with patch("aave.proposals.fetch_payload_states", return_value=payload_states):
        assert has_pending_payload(proposal)


def test_aave_has_pending_payload_is_false_when_all_payloads_are_executed():
    proposal = _proposal(488, 1)
    payload_states = [{"payloadId": "443", "chainId": "1", "state": "executed"}]

    with patch("aave.proposals.fetch_payload_states", return_value=payload_states):
        assert not has_pending_payload(proposal)

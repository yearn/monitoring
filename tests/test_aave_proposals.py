from unittest.mock import patch

from protocols.aave.proposals import (
    earliest_queued_at,
    fetch_queued_proposals,
    handle_governance_proposals,
    has_pending_payload,
)
from utils.alert import AlertSeverity


def _proposal(proposal_id: int, title: str = "Test Proposal", state: str = "queued") -> dict:
    return {"proposalId": str(proposal_id), "title": title, "state": state}


def _payload_state(
    state: str = "executed",
    queued_at: str | None = "2026-05-26T10:30:35+00:00",
    payload_id: int = 1,
    chain_id: int = 1,
) -> dict:
    return {
        "proposalId": "1",
        "payloadId": str(payload_id),
        "chainId": str(chain_id),
        "state": state,
        "queuedAt": queued_at,
        "executedAt": None,
        "cancelledAt": None,
    }


def test_aave_fetches_queued_governance_proposals():
    payload = {"data": {"getProposalsByState": {"nodes": [_proposal(12), _proposal(10), _proposal(9)]}}}

    with patch("protocols.aave.proposals.run_query", return_value=payload) as mock_run_query:
        proposals = fetch_queued_proposals(9)

    query, variables = mock_run_query.call_args.args
    assert "getProposalsByState" in query
    assert "stateFilter: $state" in query
    assert variables == {"state": "queued", "limit": 10}
    # id 9 is filtered out (not > 9); remainder sorted ascending.
    assert [proposal["proposalId"] for proposal in proposals] == ["10", "12"]


def test_aave_handler_alerts_governance_executed_proposals_with_pending_payloads():
    proposals = [
        _proposal(488, "Already Executed Proposal"),
        _proposal(489, "Passed Proposal"),
    ]
    fully_executed = [_payload_state(state="executed")]
    still_pending = [
        _payload_state(state="executed", chain_id=1),
        _payload_state(state="created", chain_id=196, payload_id=4, queued_at="2026-05-27T08:00:00+00:00"),
    ]

    with (
        patch("protocols.aave.proposals.get_last_queued_id_from_file", return_value=487),
        patch("protocols.aave.proposals.fetch_queued_proposals", return_value=proposals),
        patch("protocols.aave.proposals.fetch_payload_states", side_effect=[fully_executed, still_pending]),
        patch("protocols.aave.proposals.send_alert") as mock_send,
        patch("protocols.aave.proposals.write_last_queued_id_to_file") as mock_write,
    ):
        handle_governance_proposals()

    mock_send.assert_called_once()
    alert = mock_send.call_args.args[0]
    assert alert.protocol == "aave"
    assert alert.severity == AlertSeverity.LOW
    assert "Passed Proposal" in alert.message
    assert "Already Executed Proposal" not in alert.message
    assert "2026-05-26 10:30:35 UTC" in alert.message
    mock_write.assert_called_once_with("aave", 489)


def test_aave_has_pending_payload_is_true_when_any_payload_is_not_executed():
    payload_states = [
        {"payloadId": "442", "chainId": "1", "state": "executed"},
        {"payloadId": "4", "chainId": "196", "state": "created"},
    ]
    assert has_pending_payload(payload_states)


def test_aave_has_pending_payload_is_false_when_all_payloads_are_executed():
    assert not has_pending_payload([{"payloadId": "443", "chainId": "1", "state": "executed"}])


def test_aave_has_pending_payload_is_false_when_no_payloads():
    assert not has_pending_payload([])


def test_aave_earliest_queued_at_returns_min_timestamp():
    states = [
        _payload_state(queued_at="2026-05-27T08:00:00+00:00"),
        _payload_state(queued_at="2026-05-26T10:30:35+00:00"),
        _payload_state(queued_at=None),
    ]
    queued = earliest_queued_at(states)
    assert queued is not None
    assert queued.strftime("%Y-%m-%d %H:%M:%S") == "2026-05-26 10:30:35"

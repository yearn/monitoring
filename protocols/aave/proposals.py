from datetime import datetime

import requests

from utils.cache import get_last_queued_id_from_file, write_last_queued_id_to_file
from utils.http import request_with_retry
from utils.logging import get_logger
from utils.telegram import send_error_message, send_telegram_message

PROTOCOL = "aave"
logger = get_logger(PROTOCOL)

# Aave's official governance cache API. Replaces the decentralized-network
# subgraph (gateway-arbitrum), whose indexers stopped serving the governance
# deployment and returned BadResponse(400)/Unavailable on every query.
AAVE_GOVERNANCE_CACHE_API = "https://governance-cache-api.aave.com/graphql"
# Proposal-level state in the cache API: governance execution has queued the
# payloads, but the proposal stays "queued" until every payload has executed
# (it only flips to "executed" once all are done). These are the proposals to
# alert on — distinct from the payload-level "executed" check below.
QUEUED_PROPOSAL_STATE = "queued"
# Payload-level state: an individual payload has executed on its target chain.
EXECUTED_PAYLOAD_STATE = "executed"


def run_query(query: str, variables: dict) -> dict | None:
    """Run a GraphQL query against the Aave governance cache API with retry logic.

    Args:
        query: The GraphQL query string.
        variables: Variables for the GraphQL query.

    Returns:
        Parsed JSON response dict, or None on failure.
    """
    request_body = {"query": query, "variables": variables}

    try:
        response = request_with_retry("post", AAVE_GOVERNANCE_CACHE_API, json=request_body)
    except requests.RequestException as e:
        logger.error("Aave governance cache API query failed after retries: %s", e)
        send_error_message(f"Aave governance cache API query failed after retries: {e}", PROTOCOL)
        return None

    data: dict = response.json()
    if "errors" in data:
        logger.error("GraphQL error in response: %s", data["errors"])
        send_error_message(f"GraphQL error in response: {data['errors']}", PROTOCOL)
        return None

    return data


def fetch_queued_proposals(last_reported_id: int) -> list[dict]:
    """Fetch queued governance proposals newer than ``last_reported_id``.

    A proposal is ``queued`` once governance execution has queued its payloads in
    the PayloadsController of each target chain and at least one payload is still
    awaiting execution; once every payload executes it flips to ``executed``.
    ``has_pending_payload`` re-confirms a payload is still pending before alerting.

    Args:
        last_reported_id: Highest proposal id already reported.

    Returns:
        Queued proposals with id greater than ``last_reported_id``, sorted by
        ascending proposal id. Each dict carries ``proposalId``, ``title`` and ``state``.
    """
    query = """
        query($state: String!, $limit: Int!) {
            getProposalsByState(stateFilter: $state, limitCount: $limit) {
                nodes {
                    proposalId
                    title
                    state
                }
            }
        }
    """

    variables = {"state": QUEUED_PROPOSAL_STATE, "limit": 10}
    response = run_query(query, variables)
    if response is None:
        return []

    nodes = response["data"]["getProposalsByState"]["nodes"]
    proposals = [proposal for proposal in nodes if int(proposal["proposalId"]) > last_reported_id]
    return sorted(proposals, key=lambda proposal: int(proposal["proposalId"]))


def fetch_payload_states(proposal_id: int) -> list[dict]:
    """Fetch the per-chain payload execution states for a proposal.

    Args:
        proposal_id: Governance proposal id.

    Returns:
        Payload state nodes (one per payload), each with ``state``, ``queuedAt``,
        ``executedAt`` and ``cancelledAt`` ISO-8601 timestamps.
    """
    query = """
        query GetProposalPayloads($proposalId: String!) {
            getProposalPayloads(pProposalId: $proposalId) {
                nodes {
                    proposalId
                    payloadId
                    chainId
                    state
                    queuedAt
                    executedAt
                    cancelledAt
                }
            }
        }
    """
    response = request_with_retry(
        "post",
        AAVE_GOVERNANCE_CACHE_API,
        json={"query": query, "variables": {"proposalId": str(proposal_id)}},
    )
    data = response.json()
    if "errors" in data:
        raise ValueError(f"Aave governance cache API error: {data['errors']}")
    nodes: list[dict] = data["data"]["getProposalPayloads"]["nodes"]
    return nodes


def has_pending_payload(payload_states: list[dict]) -> bool:
    """Return True when any payload of a proposal has not executed yet.

    The app shows proposals like 489 as still pending while any payload is not
    executed; fully executed proposals like 488 should not alert again.

    Args:
        payload_states: Payload state nodes from ``fetch_payload_states``.
    """
    if not payload_states:
        return False
    return any(payload.get("state") != EXECUTED_PAYLOAD_STATE for payload in payload_states)


def earliest_queued_at(payload_states: list[dict]) -> datetime | None:
    """Return the earliest ``queuedAt`` across a proposal's payloads, if any.

    Args:
        payload_states: Payload state nodes from ``fetch_payload_states``.
    """
    queued = [datetime.fromisoformat(p["queuedAt"]) for p in payload_states if p.get("queuedAt")]
    return min(queued) if queued else None


def handle_governance_proposals() -> None:
    """Alert on executed Aave proposals whose payloads are still queued."""
    last_sent_id = get_last_queued_id_from_file(PROTOCOL)
    proposals = fetch_queued_proposals(last_sent_id)
    if not proposals:
        logger.info("No proposals found")
        return

    aave_url = "https://app.aave.com/governance/v3/proposal/?proposalId="
    message = ""
    newest_reported_id = last_sent_id
    for proposal in proposals:
        proposal_id = int(proposal["proposalId"])
        if proposal_id <= last_sent_id:
            logger.info("Proposal: %s already reported", proposal["proposalId"])
            continue

        payload_states = fetch_payload_states(proposal_id)
        if not has_pending_payload(payload_states):
            logger.info("Proposal: %s has no pending payloads", proposal["proposalId"])
            continue

        newest_reported_id = max(newest_reported_id, proposal_id)
        queued_at = earliest_queued_at(payload_states)
        formatted_timestamp = queued_at.strftime("%Y-%m-%d %H:%M:%S UTC") if queued_at else "unknown"
        message += (
            f"📕 Title: {proposal['title']}\n"
            f"🆔 ID: {proposal['proposalId']}\n"
            f"🕒 Queued at: {formatted_timestamp}\n"
            f"🔗 Link to Proposal: {aave_url + proposal['proposalId']}\n\n"
        )

    if not message:
        logger.info("No proposals found in the last hour")
        return

    message = "🖋️ Queued Aave Governance Proposals 🖋️\n" + message
    send_telegram_message(message, PROTOCOL)
    write_last_queued_id_to_file(PROTOCOL, newest_reported_id)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(handle_governance_proposals, PROTOCOL)

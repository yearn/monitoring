import os
from datetime import datetime

import requests

from utils.cache import get_last_queued_id_from_file, write_last_queued_id_to_file
from utils.http import request_with_retry
from utils.logging import get_logger
from utils.telegram import send_error_message, send_telegram_message

PROTOCOL = "aave"
logger = get_logger(PROTOCOL)
AAVE_GOVERNANCE_EXECUTED_STATE = 4
AAVE_GOVERNANCE_CACHE_API = "https://governance-cache-api.aave.com/graphql"


def run_query(query: str, variables: dict) -> dict | None:
    """Run a GraphQL query against The Graph API with retry logic.

    Args:
        query: The GraphQL query string.
        variables: Variables for the GraphQL query.

    Returns:
        Parsed JSON response dict, or None on failure.
    """
    api_key = os.getenv("GRAPH_API_KEY")
    subgraph_id = "A7QMszgomC9cnnfpAcqZVLr2DffvkGNfimD8iUSMiurK"
    url = f"https://gateway-arbitrum.network.thegraph.com/api/{api_key}/subgraphs/id/{subgraph_id}"
    request_body = {"query": query, "variables": variables}

    try:
        response = request_with_retry("post", url, json=request_body)
    except requests.RequestException as e:
        logger.error("Graph API query failed after retries: %s", e)
        send_error_message(f"Graph API query failed after retries: {e}", PROTOCOL)
        return None

    data = response.json()
    if "errors" in data:
        logger.error("GraphQL error in response: %s", data["errors"])
        send_error_message(f"GraphQL error in response: {data['errors']}", PROTOCOL)
        return None

    return data


def fetch_queued_proposals(last_reported_id: int):
    # state: 3 is queued state: https://github.com/bgd-labs/aave-governance-v3/blob/0c14d60ac89d7a9f79d0a1f77de5c99c3ba1201f/src/interfaces/IGovernanceCore.sol#L75
    # state: 4 is executed. Aave Governance execution queues proposal payloads in their PayloadsController.
    query = """
        query($lastId: Int!, $executedState: Int!) {
            proposals(
                where:{state:$executedState, proposalId_gt:$lastId}
                orderBy: proposalId
                orderDirection: desc
                first: 10
            ) {
                proposalId
                proposalMetadata{
                    title
                }
                transactions{
                    executed{
                        timestamp
                    }
                }
                payloads{
                    id
                    chainId
                    payloadsController
                }
            }
        }
    """

    variables = {"lastId": last_reported_id, "executedState": AAVE_GOVERNANCE_EXECUTED_STATE}
    response = run_query(query, variables)
    if response is None:
        return []

    proposals = response["data"]["proposals"]
    return sorted(proposals, key=lambda proposal: int(proposal["proposalId"]))


def fetch_payload_states(proposal_id: int) -> list[dict]:
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
    return data["data"]["getProposalPayloads"]["nodes"]


def has_pending_payload(proposal: dict) -> bool:
    proposal_id = int(proposal["proposalId"])
    payload_states = fetch_payload_states(proposal_id)
    if not payload_states:
        logger.info("Proposal: %s has no payload cache data", proposal_id)
        return False

    # The app shows proposals like 489 as still pending while any payload is not
    # executed yet. Fully executed proposals like 488 should not alert again.
    return any(payload.get("state") != "executed" for payload in payload_states)


def handle_governance_proposals():
    last_sent_id = get_last_queued_id_from_file(PROTOCOL)
    proposals = fetch_queued_proposals(last_sent_id)
    if not proposals:
        logger.info("No proposals found")
        return

    aave_url = "https://app.aave.com/governance/v3/proposal/?proposalId="
    message = ""
    newest_reported_id = last_sent_id
    for proposal in proposals:
        timestamp = int(proposal["transactions"]["executed"]["timestamp"])
        proposal_id = int(proposal["proposalId"])
        if proposal_id <= last_sent_id:
            logger.info("Proposal: %s already reported", proposal["proposalId"])
            continue

        if not has_pending_payload(proposal):
            logger.info("Proposal: %s has no pending payloads", proposal["proposalId"])
            continue

        newest_reported_id = max(newest_reported_id, proposal_id)
        date_time = datetime.fromtimestamp(timestamp)
        formatted_timestamp = date_time.strftime("%Y-%m-%d %H:%M:%S")
        message += (
            f"📕 Title: {proposal['proposalMetadata']['title']}\n"
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

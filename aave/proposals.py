import os
from datetime import datetime

import requests

from utils.cache import get_last_queued_id_from_file, write_last_queued_id_to_file
from utils.http import request_with_retry
from utils.logging import get_logger
from utils.telegram import send_telegram_message

PROTOCOL = "aave"
logger = get_logger(PROTOCOL)


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
        send_telegram_message(f"Graph API query failed after retries: {e}", PROTOCOL, True)
        return None

    data = response.json()
    if "errors" in data:
        logger.error("GraphQL error in response: %s", data["errors"])
        send_telegram_message(f"GraphQL error in response: {data['errors']}", PROTOCOL, True)
        return None

    return data


def fetch_queued_proposals(last_reported_id: int):
    # state: 3 is queued state: https://github.com/bgd-labs/aave-governance-v3/blob/0c14d60ac89d7a9f79d0a1f77de5c99c3ba1201f/src/interfaces/IGovernanceCore.sol#L75
    # queued state is transferred to active state when it's executed, few seconds later so we need to check state 4
    query = """
        query($lastId: Int!) {
            proposals(where:{state:4, proposalId_gt:$lastId}) {
                proposalId
                proposalMetadata{
                    title
                }
                transactions{
                    executed{
                        timestamp
                    }
                }
            }
        }
    """

    # get only last 10 proposals
    variables = {"lastId": last_reported_id}
    response = run_query(query, variables)
    if response is None:
        return []

    proposals = response["data"]["proposals"]
    return proposals


def handle_governance_proposals():
    last_sent_id = get_last_queued_id_from_file(PROTOCOL)
    proposals = fetch_queued_proposals(last_sent_id)
    if not proposals:
        logger.info("No proposals found")
        return

    aave_url = "https://app.aave.com/governance/v3/proposal/?proposalId="
    message = ""
    for proposal in proposals:
        timestamp = int(proposal["transactions"]["executed"]["timestamp"])
        proposal_id = int(proposal["proposalId"])
        if proposal_id <= last_sent_id:
            logger.info("Proposal: %s already reported", proposal["proposalId"])
            continue

        date_time = datetime.fromtimestamp(timestamp)
        timestamp = date_time.strftime("%Y-%m-%d %H:%M:%S")
        message += (
            f"📕 Title: {proposal['proposalMetadata']['title']}\n"
            f"🆔 ID: {proposal['proposalId']}\n"
            f"🕒 Queued at: {timestamp}\n"
            f"🔗 Link to Proposal: {aave_url + proposal['proposalId']}\n\n"
        )

    if not message:
        logger.info("No proposals found in the last hour")
        return

    message = "🖋️ Queued Aave Governance Proposals 🖋️\n" + message
    send_telegram_message(message, PROTOCOL)
    write_last_queued_id_to_file(PROTOCOL, proposals[-1]["proposalId"])


if __name__ == "__main__":
    handle_governance_proposals()

"""Monitor new Spark (SPK) governance proposals from the Snapshot space sparkfi.eth.

Voting happens on https://app.spark.fi/spk/governance, which is backed by the
Snapshot space `sparkfi.eth` (queried via the Snapshot hub GraphQL API).
"""

from datetime import datetime, timezone

import requests

from utils.alert import Alert, AlertSeverity, send_alert
from utils.cache import get_last_queued_id_from_file, write_last_queued_id_to_file
from utils.logger import get_logger
from utils.telegram import escape_markdown, send_error_message

PROTOCOL = "spark"
logger = get_logger(PROTOCOL)

SNAPSHOT_GRAPHQL_URL = "https://hub.snapshot.org/graphql"
SNAPSHOT_SPACE = "sparkfi.eth"
GOVERNANCE_URL = "https://app.spark.fi/spk/governance"

PROPOSALS_QUERY = """
query Proposals($space: String!) {
  proposals(
    first: 30
    where: { space_in: [$space] }
    orderBy: "created"
    orderDirection: desc
  ) {
    id
    title
    state
    start
    end
    created
    link
  }
}
"""


def fetch_proposals() -> list[dict]:
    """Fetch the most recent proposals from the Snapshot hub GraphQL API."""
    response = requests.post(
        SNAPSHOT_GRAPHQL_URL,
        json={"query": PROPOSALS_QUERY, "variables": {"space": SNAPSHOT_SPACE}},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise ValueError(f"Snapshot GraphQL errors: {payload['errors']}")
    proposals: list[dict] = payload["data"]["proposals"]
    return proposals


def format_timestamp(timestamp: int) -> str:
    """Format a unix timestamp as a UTC date-time string."""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def get_proposals() -> None:
    """Fetch and alert on new Spark governance proposals."""
    try:
        proposals = fetch_proposals()
        if not proposals:
            logger.info("No proposals found for space %s", SNAPSHOT_SPACE)
            return

        # Sort by created ascending so we process oldest first.
        proposals = sorted(proposals, key=lambda p: p["created"])

        # Cache stores the created timestamp of the newest processed proposal.
        last_reported_timestamp = get_last_queued_id_from_file(PROTOCOL)
        newest_timestamp = last_reported_timestamp

        message = ""
        for proposal in proposals:
            created = int(proposal["created"])
            if created <= last_reported_timestamp:
                continue

            if created > newest_timestamp:
                newest_timestamp = created

            # Skip proposals whose voting already ended (also avoids a first-run backfill).
            if proposal["state"] == "closed":
                continue

            message += f"📕 Title: {escape_markdown(proposal['title'])}\n"
            message += f"🗳️ State: {proposal['state']}\n"
            message += f"🔗 Vote: {proposal['link']}\n"
            message += f"⏰ Voting ends: {format_timestamp(int(proposal['end']))}\n"
            message += "\n"

        if message:
            message = f"⚡ Spark Governance Proposals ⚡\n🏛️ Portal: {GOVERNANCE_URL}\n\n" + message
            send_alert(Alert(AlertSeverity.LOW, message, PROTOCOL))
        else:
            logger.info("No new open proposals to report")

        if newest_timestamp > last_reported_timestamp:
            write_last_queued_id_to_file(PROTOCOL, newest_timestamp)
            logger.info("Processed proposals up to timestamp %s", newest_timestamp)

    except requests.RequestException as e:
        error_message = f"Failed to fetch Spark proposals from Snapshot: {escape_markdown(str(e))}"
        logger.error("%s", error_message)
        send_error_message(error_message, PROTOCOL)
    except Exception as e:
        error_message = f"Error processing Spark proposals: {escape_markdown(str(e))}"
        logger.error("%s", error_message)
        send_error_message(error_message, PROTOCOL)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(get_proposals, PROTOCOL)

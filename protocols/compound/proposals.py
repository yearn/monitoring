import os

import requests
from dotenv import load_dotenv

from utils.cache import get_last_queued_id_from_file, write_last_queued_id_to_file
from utils.logger import get_logger
from utils.telegram import escape_markdown, send_telegram_message

load_dotenv()

# If more project start to use tally, extract tally code to utils
TALLY_API_KEY = os.getenv("TALLY_API_KEY")
TALLY_API_URL = "https://api.tally.xyz/query"
COMPOUND_TALLY_URL = "https://www.tally.xyz/gov/compound/proposal/"

PROTOCOL = "comp"  # must be lower case
logger = get_logger(PROTOCOL)
max_length_summary = 450


def extract_title_from_metadata(raw_title: str) -> str:
    """Extract the actual title from Tally metadata title field.

    Tally sometimes returns the full markdown description in the title field.
    This extracts just the first heading as the actual title.
    """
    if not raw_title:
        return ""
    # Tally may return literal "\n" (escaped) or actual newlines
    first_line = raw_title.strip().split("\\n")[0].split("\n")[0].strip()
    # Remove markdown heading prefix
    if first_line.startswith("#"):
        first_line = first_line.lstrip("#").strip()
    return first_line


def extract_summary_from_description(description):
    # Extract the summary part
    summary_start = description.find("# Summary")
    if summary_start == -1:
        summary_start = description.find("## Proposal summary")
    motivation_start = description.find("# Motivation")
    if motivation_start == -1:
        motivation_start = description.find("## Proposal Action")
    summary = ""
    if summary_start != -1 and motivation_start != -1:
        summary_start = description.find("\n", summary_start)
        summary = description[summary_start:motivation_start].strip()
        summary = summary.replace("\n\n", "\n")
        # Truncate the summary if it exceeds max_length
        if len(summary) > max_length_summary:
            summary = summary[:max_length_summary].rsplit(" ", 1)[0] + "..."
    return summary


def get_proposals():
    headers = {"Api-Key": TALLY_API_KEY, "Content-Type": "application/json"}

    query = """
    query GovernanceProposals($input: ProposalsInput!) {
      proposals(input: $input) {
        nodes {
          ... on Proposal {
            id
            onchainId
            status
            createdAt
            metadata {
              title
              description
            }
          }
        }
      }
    }
    """

    variables = {
        "input": {
            "filters": {"organizationId": "2206072050458560433"},
            "sort": {"sortBy": "id", "isDescending": True},
            "page": {"limit": 10},
        }
    }

    try:
        response = requests.post(
            TALLY_API_URL,
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            raise Exception(f"GraphQL errors: {data['errors']}")

        proposals = data["data"]["proposals"]["nodes"]
        queued_proposals = [p for p in proposals if p["status"] == "queued"]
        if not queued_proposals:
            logger.info("No queued proposals found")
            return

        logger.info("Found %s queued proposals", len(queued_proposals))

        # Sort queued proposals by onchainId from lowest to highest
        queued_proposals.sort(key=lambda x: int(x["onchainId"]))
        last_reported_id = get_last_queued_id_from_file(PROTOCOL)
        newest_queued_proposal_id = int(queued_proposals[-1]["onchainId"])
        logger.info("Newest queued proposal id: %s", newest_queued_proposal_id)
        if newest_queued_proposal_id <= last_reported_id:
            logger.info("No new proposals, last reported id: %s", last_reported_id)
            return

        message = "🖋️ Compound Governance Proposals 🖋️\n"
        newest_reported_proposal_id = last_reported_id
        for proposal in queued_proposals:
            proposal_id = int(proposal["onchainId"])
            if proposal_id <= last_reported_id:
                # use continue instead of break because Comp can have unordered proposal ids
                continue

            newest_reported_proposal_id = max(newest_reported_proposal_id, proposal_id)
            link = COMPOUND_TALLY_URL + str(proposal_id)
            message += f"📗 Proposal ID: {proposal_id}\n"
            message += f"🔗 Link to Proposal: {link}\n"

            metadata = proposal["metadata"]
            title = extract_title_from_metadata(metadata["title"])
            if title:
                message += f"📝 Title: {escape_markdown(title)}\n"
            # if description:
            #     summary = extract_summary_from_description(description)
            #     if summary:
            #         message += f"📝 Description: {summary}\n\n"

        send_telegram_message(message, PROTOCOL)
        # write the last reported id (highest ID since we sorted ascending)
        write_last_queued_id_to_file(PROTOCOL, newest_reported_proposal_id)

    except requests.RequestException as e:
        message = f"Failed to fetch compound proposals: {e}"
        send_telegram_message(message, PROTOCOL, disable_notification=True, plain_text=True)
    except Exception as e:
        message = f"Error processing compound proposals: {e}"
        logger.error("%s", message)
        send_telegram_message(message, PROTOCOL, disable_notification=True, plain_text=True)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(get_proposals, PROTOCOL)

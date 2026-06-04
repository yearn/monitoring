from datetime import datetime

import requests

from utils.cache import get_last_queued_id_from_file, write_last_queued_id_to_file
from utils.logging import get_logger
from utils.telegram import escape_markdown, send_telegram_message

PROTOCOL = "fluid"
logger = get_logger(PROTOCOL)
FLUID_API_URL = "https://atlas.api.instadapp.io/proposals"
FLUID_PROPOSAL_URL = "https://fluid.io/gov/proposals/"

max_length_summary = 450


def extract_summary_from_description(description):
    """Extract the summary part from proposal description"""
    if not description:
        return ""

    # Look for summary section
    summary_start = description.find("## Summary")
    if summary_start == -1:
        summary_start = description.find("# Summary")

    # Look for next section
    code_changes_start = description.find("## Code Changes")
    if code_changes_start == -1:
        code_changes_start = description.find("## Description")
    if code_changes_start == -1:
        code_changes_start = description.find("# Description")

    summary = ""
    if summary_start != -1:
        if code_changes_start != -1:
            summary_start = description.find("\n", summary_start)
            summary = description[summary_start:code_changes_start].strip()
        else:
            # Take first paragraph after summary header
            summary_start = description.find("\n", summary_start)
            lines = description[summary_start:].split("\n")
            summary_lines = []
            for line in lines:
                line = line.strip()
                if line and not line.startswith("#"):
                    summary_lines.append(line)
                elif summary_lines:  # Stop at next section or empty line after content
                    break
            summary = " ".join(summary_lines)

        summary = summary.replace("\n\n", "\n")
        # Truncate the summary if it exceeds max_length
        if len(summary) > max_length_summary:
            summary = summary[:max_length_summary].rsplit(" ", 1)[0] + "..."

    return summary


def get_proposals():
    """Fetch and process Fluid governance proposals"""
    try:
        # Fetch queued proposals
        response = requests.get(f"{FLUID_API_URL}?status=queued", timeout=30)
        response.raise_for_status()
        data = response.json()

        if "data" not in data or not data["data"]:
            logger.info("No queued proposals found")
            return

        proposals = data["data"]
        if not isinstance(proposals, list):
            raise ValueError("Fluid API returned invalid proposals payload")

        logger.info("Found %s queued proposals", len(proposals))

        # Sort proposals by id from lowest to highest
        proposals.sort(key=lambda x: int(x["id"]))
        last_reported_id = get_last_queued_id_from_file(PROTOCOL)
        newest_proposal_id = int(proposals[-1]["id"])

        logger.info("Newest proposal id: %s", newest_proposal_id)
        if newest_proposal_id <= last_reported_id:
            logger.info("No new proposals, last reported id: %s", last_reported_id)
            return

        message = "🏛️ Fluid Protocol Governance Proposals 🏛️\n"
        new_proposals_found = False
        newest_reported_proposal_id = last_reported_id

        for proposal in proposals:
            proposal_id = int(proposal["id"])
            if proposal_id <= last_reported_id:
                continue

            new_proposals_found = True
            newest_reported_proposal_id = max(newest_reported_proposal_id, proposal_id)
            link = FLUID_PROPOSAL_URL + str(proposal_id)
            message += f"📗 Proposal ID: {proposal_id}\n"
            message += f"🔗 Link: [Proposal {proposal_id}]({link})\n"

            title = proposal.get("title", "")
            if title:
                message += f"📝 Title: {escape_markdown(title)}\n"

            # Add execution info
            queued_at = proposal.get("queued_at")
            if queued_at:
                try:
                    queued_date = datetime.fromisoformat(queued_at.replace("Z", "+00:00"))
                    message += f"✅ Queued: {queued_date.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                except Exception:
                    message += f"✅ Queued: {escape_markdown(queued_at)}\n"

            # Add summary if available
            description = proposal.get("description", "")
            if description:
                summary = extract_summary_from_description(description)
                if summary:
                    message += f"📄 Summary: {escape_markdown(summary)}\n"

            message += "\n"

        if new_proposals_found:
            send_telegram_message(message, PROTOCOL)
            write_last_queued_id_to_file(PROTOCOL, newest_reported_proposal_id)
        else:
            logger.info("No new proposals to report")

    except requests.RequestException as e:
        error_message = f"Failed to fetch Fluid proposals: {e}"
        logger.error("%s", error_message)
        send_telegram_message(error_message, PROTOCOL, disable_notification=True, plain_text=True)
    except Exception as e:
        error_message = f"Error processing Fluid proposals: {e}"
        logger.error("%s", error_message)
        send_telegram_message(error_message, PROTOCOL, disable_notification=True, plain_text=True)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(get_proposals, PROTOCOL)

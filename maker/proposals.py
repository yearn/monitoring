from datetime import datetime

import requests

from utils.cache import get_last_queued_id_from_file, write_last_queued_id_to_file
from utils.logging import get_logger
from utils.telegram import send_telegram_message

PROTOCOL = "maker"
logger = get_logger(PROTOCOL)

SKY_EXECUTIVE_API = "https://vote.sky.money/api/executive"
SKY_EXECUTIVE_URL = "https://vote.sky.money/executive/"


def fetch_executive_proposals() -> list[dict]:
    """Fetch executive proposals from the Sky governance API."""
    response = requests.get(SKY_EXECUTIVE_API, timeout=30)
    response.raise_for_status()
    return response.json()


def get_proposals():
    """Fetch and alert on new Sky (Maker) executive proposals."""
    try:
        proposals = fetch_executive_proposals()
        if not proposals:
            logger.info("No executive proposals found")
            return

        # Sort by date ascending so we process oldest first
        proposals.sort(key=lambda p: p["date"])

        # Cache stores epoch timestamp of the latest reported proposal date
        last_reported_timestamp = get_last_queued_id_from_file(PROTOCOL)
        newest_timestamp = last_reported_timestamp

        message = ""
        for proposal in proposals:
            proposal_date = datetime.fromisoformat(proposal["date"].replace("Z", "+00:00"))
            proposal_timestamp = int(proposal_date.timestamp())

            if proposal_timestamp <= last_reported_timestamp:
                continue

            spell_data = proposal.get("spellData", {})

            # Only alert on scheduled (not yet executed) proposals
            if not spell_data.get("hasBeenScheduled") or spell_data.get("hasBeenCast"):
                continue

            link = SKY_EXECUTIVE_URL + proposal["key"]

            message += f"📕 Title: {proposal['title']}\n"
            if proposal.get("proposalBlurb"):
                blurb = proposal["proposalBlurb"]
                if len(blurb) > 450:
                    blurb = blurb[:450].rsplit(" ", 1)[0] + "..."
                message += f"📝 Summary: {blurb}\n"
            message += f"🔗 Link: {link}\n"
            message += f"📅 Date: {proposal_date.strftime('%Y-%m-%d')}\n"
            if spell_data.get("eta"):
                message += f"⏰ ETA: {spell_data['eta']}\n"
            message += "\n"

            if proposal_timestamp > newest_timestamp:
                newest_timestamp = proposal_timestamp

        if not message:
            logger.info("No new executive proposals to report")
            return

        message = "🏛️ Sky (Maker) Executive Proposals 🏛️\n" + message
        send_telegram_message(message, PROTOCOL)
        write_last_queued_id_to_file(PROTOCOL, newest_timestamp)
        logger.info("Reported proposals up to timestamp %s", newest_timestamp)

    except requests.RequestException as e:
        error_message = f"Failed to fetch Sky executive proposals: {e}"
        logger.error("%s", error_message)
        send_telegram_message(error_message, PROTOCOL, True, plain_text=True)
    except Exception as e:
        error_message = f"Error processing Sky executive proposals: {e}"
        logger.error("%s", error_message)
        send_telegram_message(error_message, PROTOCOL, True, plain_text=True)


if __name__ == "__main__":
    from utils.runner import run_with_alert

    run_with_alert(get_proposals, PROTOCOL)

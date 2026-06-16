"""
Tenderly monitoring script to verify that alerts haven't changed.
This script is run daily by Github actions to verify alerts match the stored snapshot.
Use --update to save a new snapshot of alerts.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from utils.logger import get_logger

load_dotenv()

logger = get_logger("tenderly")

TENDERLY_API_KEY = os.getenv("TENDERLY_API_KEY")
TENDERLY_API_URL = "https://api.tenderly.co/api/v1/account/yearn/project/sam/alerts"

# Path to stored alerts snapshot
SCRIPT_DIR = Path(__file__).parent
ALERTS_FILE = SCRIPT_DIR / "alerts.json"


def get_response_hash(data: dict) -> str:
    """Generate a hash from the entire JSON response, sorted by alert ID."""
    sorted_alerts = sorted(data.get("alerts", []), key=lambda x: x["id"])
    sorted_data = {"alerts": sorted_alerts}
    json_str = json.dumps(sorted_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(json_str.encode()).hexdigest()


def fetch_alerts() -> dict:
    """Fetch alerts from Tenderly API."""
    headers = {"Accept": "application/json", "X-Access-Key": TENDERLY_API_KEY}
    response = requests.get(TENDERLY_API_URL, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Failed to get alerts: {response.status_code} - {response.text}")
    return response.json()


def load_stored_alerts() -> dict:
    """Load stored alerts from alerts.json."""
    if not ALERTS_FILE.exists():
        raise Exception(f"alerts.json not found at {ALERTS_FILE}. Run with --update flag to create initial snapshot.")
    with open(ALERTS_FILE, "r") as f:
        return json.load(f)


def save_alerts(data: dict) -> None:
    """Save alerts to alerts.json."""
    # Sort alerts by ID for consistent storage
    sorted_alerts = sorted(data.get("alerts", []), key=lambda x: x["id"])
    sorted_data = {"alerts": sorted_alerts}
    with open(ALERTS_FILE, "w") as f:
        json.dump(sorted_data, f, indent=2)
    logger.info("Saved %d alerts to %s", len(sorted_alerts), ALERTS_FILE)


def verify_alerts() -> None:
    """Verify current alerts match stored snapshot."""
    stored_data = load_stored_alerts()
    current_data = fetch_alerts()

    stored_hash = get_response_hash(stored_data)
    current_hash = get_response_hash(current_data)

    stored_count = len(stored_data.get("alerts", []))
    current_count = len(current_data.get("alerts", []))

    if current_count != stored_count:
        raise Exception(f"❌ Alert count mismatch: stored has {stored_count}, current has {current_count}")

    if current_hash != stored_hash:
        raise Exception(
            f"❌ Alert configuration changed!\n"
            f"   Stored hash: {stored_hash}\n"
            f"   Current hash: {current_hash}\n"
            f"   Check who updated Tenderly alerts for SAM project.\n"
            f"   Run with --update to save new snapshot if changes are intentional."
        )

    logger.info("Verified %d alerts match stored snapshot (hash: %s)", current_count, current_hash)


def main():
    parser = argparse.ArgumentParser(description="Verify Tenderly alerts match stored snapshot")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Update alerts.json with current alerts from API",
    )
    args = parser.parse_args()

    if args.update:
        data = fetch_alerts()
        save_alerts(data)
        logger.info("Hash: %s", get_response_hash(data))
    else:
        verify_alerts()


if __name__ == "__main__":
    main()

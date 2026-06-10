"""Publish markdown text to Wavey Gist and return the public URL."""

import os

import requests

from utils.logging import get_logger

logger = get_logger("utils.wavey_gist")

WAVEY_GIST_API_URL = "https://api.wavey.info/api/v1/gists"
DEFAULT_GIST_TITLE = "Monitoring Details"


def upload_to_gist(content: str, title: str = "") -> str:
    """Publish markdown ``content`` to Wavey Gist and return the rendered-page URL.

    Args:
        content: The markdown text to upload.
        title: Optional title, prepended as a top-level markdown heading.

    Returns:
        The URL of the created gist, or an empty string on failure.
    """
    if not content:
        return ""

    api_key = os.getenv("WAVEY_GIST_API_KEY")
    if not api_key:
        logger.warning("Failed to upload to Wavey Gist: WAVEY_GIST_API_KEY is not set")
        return ""

    markdown = f"# {title}\n\n{content}" if title else content

    try:
        response = requests.post(
            WAVEY_GIST_API_URL,
            json={"title": title or DEFAULT_GIST_TITLE, "markdown": markdown},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Failed to upload to Wavey Gist: %s", e)
        return ""

    url = payload.get("url", "")
    if not url:
        logger.warning("Wavey Gist response did not include a URL: %s", payload)
        return ""

    logger.info("Uploaded gist to %s", url)
    return url

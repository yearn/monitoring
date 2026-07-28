"""Publish markdown text to Wavey Gist and return the public URL."""

import os

import requests

from utils.logger import get_logger

logger = get_logger("utils.wavey_gist")

WAVEY_GIST_API_URL = "https://api.wavey.info/api/v1/gists"
DEFAULT_GIST_TITLE = "Monitoring Details"
# Wavey Gist picks `README.md` as the primary file when present, so the rendered
# page opens with our content. See https://gist.wavey.info/llms.txt.
PRIMARY_FILE_NAME = "README.md"


def upload_to_gist(content: str, title: str = "") -> str:
    """Publish markdown ``content`` to Wavey Gist and return the rendered-page URL.

    Args:
        content: The markdown text to upload.
        title: Optional title, prepended as a top-level markdown heading and used
            as the gist's display title.

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
    # Wavey Gist's create endpoint now expects a `files` snapshot — the legacy
    # `title` + `markdown` fields are rejected with HTTP 400. See
    # https://gist.wavey.info/llms.txt.
    payload: dict = {
        "title": title or DEFAULT_GIST_TITLE,
        "files": {PRIMARY_FILE_NAME: {"content": markdown}},
    }

    try:
        response = requests.post(
            WAVEY_GIST_API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Failed to upload to Wavey Gist: %s", e)
        return ""

    url = body.get("url", "")
    if not url:
        logger.warning("Wavey Gist response did not include a URL: %s", body)
        return ""

    logger.info("Uploaded gist to %s", url)
    return url

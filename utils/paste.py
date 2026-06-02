"""Upload markdown text to rentry.co and return the URL.

rentry.co renders markdown and accepts anonymous pastes through a small CSRF
flow: fetch the ``csrftoken`` cookie from the site, then POST the content with
the matching ``csrfmiddlewaretoken`` field and a ``Referer`` header.
"""

import requests

from utils.logging import get_logger

logger = get_logger("utils.paste")

RENTRY_BASE_URL = "https://rentry.co"
RENTRY_API_NEW_URL = f"{RENTRY_BASE_URL}/api/new"


def upload_to_paste(content: str, title: str = "") -> str:
    """Upload markdown ``content`` to rentry.co and return the rendered-page URL.

    Args:
        content: The markdown text to upload.
        title: Optional title, prepended as a top-level markdown heading.

    Returns:
        The URL of the created paste, or an empty string on failure.
    """
    if not content:
        return ""

    text = f"# {title}\n\n{content}" if title else content

    try:
        with requests.Session() as session:
            # Priming GET sets the csrftoken cookie that the POST must echo back.
            session.get(RENTRY_BASE_URL, timeout=10).raise_for_status()
            csrftoken = session.cookies.get("csrftoken")
            if not csrftoken:
                logger.warning("Failed to upload to paste service: no csrftoken cookie from rentry")
                return ""

            response = session.post(
                RENTRY_API_NEW_URL,
                data={"csrfmiddlewaretoken": csrftoken, "text": text},
                headers={"Referer": RENTRY_BASE_URL},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Failed to upload to paste service: %s", e)
        return ""

    # rentry signals success with status "200" in the JSON body; anything else
    # carries the reason in "errors"/"content".
    if str(payload.get("status")) != "200":
        logger.warning("Paste service rejected upload: %s", payload.get("errors") or payload.get("content") or payload)
        return ""

    url = payload.get("url", "")
    logger.info("Uploaded paste to %s", url)
    return url

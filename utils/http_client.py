"""Simple HTTP helper for fetching JSON from APIs."""

import time
from typing import Any

import requests

from utils.config import Config
from utils.logger import get_logger

logger = get_logger("utils.http_client")


def request_with_retry(
    method: str,
    url: str,
    retries: int | None = None,
    backoff_factor: float | None = None,
    timeout: int | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Make an HTTP request with exponential backoff retry on transient errors.

    Retries on 5xx server errors, connection errors, and timeouts.
    Raises immediately on 4xx client errors.

    Args:
        method: HTTP method (get, post, etc.).
        url: The URL to request.
        retries: Number of retry attempts. Defaults to Config.get_retry_count().
        backoff_factor: Multiplier for exponential backoff. Defaults to Config.get_backoff_factor().
        timeout: Request timeout in seconds. Defaults to Config.get_request_timeout().
        **kwargs: Additional arguments passed to requests.request().

    Returns:
        The successful Response object (with status already verified).

    Raises:
        requests.RequestException: After all retries are exhausted or on non-retryable errors.
    """
    if retries is None:
        retries = Config.get_retry_count()
    if backoff_factor is None:
        backoff_factor = Config.get_backoff_factor()
    if timeout is None:
        timeout = Config.get_request_timeout()

    last_exception: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code is not None and status_code < 500:
                raise  # Don't retry client errors (4xx)
            last_exception = e
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exception = e

        if attempt < retries:
            wait_time = backoff_factor * (2**attempt)
            logger.warning(
                "Request to %s failed (attempt %d/%d): %s. Retrying in %.1fs...",
                url,
                attempt + 1,
                retries + 1,
                last_exception,
                wait_time,
            )
            time.sleep(wait_time)

    logger.error("Request to %s failed after %d attempts: %s", url, retries + 1, last_exception)
    raise last_exception  # type: ignore[misc]


def fetch_json(
    url: str,
    method: str = "get",
    timeout: int | None = None,
    **kwargs: Any,
) -> dict | None:
    """Fetch JSON from a URL with error handling.

    Returns the parsed JSON dict on success, or None on failure.
    """
    if timeout is None:
        timeout = Config.get_request_timeout()
    try:
        resp = requests.request(method, url, timeout=timeout, **kwargs)
        if resp.status_code != 200:
            logger.error("HTTP %s for %s: %s", resp.status_code, url, resp.text[:200])
            return None
        return resp.json()
    except Exception as e:
        logger.error("Request failed for %s: %s", url, e)
        return None

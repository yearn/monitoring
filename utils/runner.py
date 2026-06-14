"""Run a script's main function with crash-alert telemetry.

Wrap a script's entrypoint so that any unhandled exception sends a Telegram
alert and the process still exits 0 — so a CI shell loop running multiple
monitoring scripts continues to the next one instead of aborting the whole run.

Usage:
    if __name__ == "__main__":
        run_with_alert(main, PROTOCOL)
"""

from typing import Callable

from utils.logging import get_logger
from utils.telegram import get_github_run_url, send_error_message

logger = get_logger("utils.runner")


def run_with_alert(entrypoint: Callable[[], None], protocol: str, name: str | None = None) -> None:
    """Run entrypoint(); on unhandled exception, send a Telegram alert and return.

    KeyboardInterrupt and SystemExit are re-raised so explicit exits aren't
    swallowed. All other exceptions trigger a plain-text crash alert routed to
    the dedicated errors channel (labelled with `protocol`; falls back to
    `protocol`'s own channel when no errors destination is configured), then this
    function returns normally so a CI shell loop running multiple scripts
    continues to the next one.

    Args:
        entrypoint: Zero-arg callable to execute (typically the script's `main`).
        protocol: Telegram protocol key used to label the crash alert and as the
            fallback channel.
        name: Optional display name for the script. Defaults to entrypoint.__module__.
    """
    try:
        entrypoint()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 - top-level safety net by design
        script = name or entrypoint.__module__
        logger.exception("%s crashed", script)
        lines = [f"🚨 {script} crashed: {type(exc).__name__}: {exc}"]
        run_url = get_github_run_url()
        if run_url:
            lines.append(f"Run: {run_url}")
        try:
            send_error_message("\n".join(lines), protocol, source="crash")
        except Exception:  # noqa: BLE001 - alerting must not itself crash the wrapper
            logger.exception("Failed to send crash alert for %s", script)

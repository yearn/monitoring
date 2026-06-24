"""Structured logging module for monitoring scripts."""

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Third-party libraries that emit high-volume per-request / per-connection noise
# (e.g. `web3.manager.RequestManager Making request. Method: ...`). They're
# capped to their own level so our own output isn't buried. Quiet by default;
# bring the chatter back without touching our level via `DEP_LOG_LEVEL=DEBUG`
# (or any other level name) in the environment.
_DEPENDENCY_LOGGERS = (
    "web3",
    "urllib3",
    "requests",
    "aiohttp",
    "asyncio",
    "websockets",
    "hpack",
    "httpx",
    "httpcore",
    "eth",
    "ens",
)


def quiet_dependency_loggers(level: str | None = None) -> None:
    """Cap noisy third-party loggers at `level` (default `DEP_LOG_LEVEL` env or
    WARNING), leaving our own loggers untouched.

    Fully reversible — pass a lower level (e.g. ``DEP_LOG_LEVEL=DEBUG``) to
    surface dependency output again. Idempotent and safe to call repeatedly.

    Args:
        level: Level name to pin dependency loggers to. Falls back to the
            ``DEP_LOG_LEVEL`` env var, then ``WARNING``.
    """
    name = (level or os.getenv("DEP_LOG_LEVEL", "WARNING") or "WARNING").upper()
    numeric = getattr(logging, name, logging.WARNING)
    for logger_name in _DEPENDENCY_LOGGERS:
        logging.getLogger(logger_name).setLevel(numeric)


def get_logger(name: str) -> logging.Logger:
    """Return a pre-configured logger for the given protocol or module name."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
        level = os.getenv("LOG_LEVEL", "INFO").upper()
        logger.setLevel(getattr(logging, level, logging.INFO))
    return logger


# Apply once on import so every script (each protocol runs in its own
# subprocess that imports this module) inherits quiet dependency loggers,
# regardless of whether it configures the root logger via basicConfig.
quiet_dependency_loggers()

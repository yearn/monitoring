"""Structured logging module for monitoring scripts."""

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def get_logger(name: str) -> logging.Logger:
    """Return a pre-configured logger for the given protocol or module name."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        level = os.getenv("LOG_LEVEL", "INFO").upper()
        logger.setLevel(getattr(logging, level, logging.INFO))
    return logger

"""
Configuration module for global settings and environment variable handling.

This module centralizes configuration settings and provides a consistent
interface for accessing environment variables and other configuration values.
"""

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

from utils.logger import get_logger

# Load environment variables from .env file
load_dotenv()

logger = get_logger("utils.config")


@dataclass
class ProtocolConfig:
    """Configuration settings for a specific protocol."""

    name: str
    alert_threshold: float = 0.95
    critical_threshold: float = 0.98
    enable_notifications: bool = True


class Config:
    """Global configuration handler."""

    # Default values that can be overridden by environment variables
    DEFAULT_TIMEOUT = 30  # seconds
    DEFAULT_RETRY_COUNT = 3
    DEFAULT_BACKOFF_FACTOR = 1.0

    @staticmethod
    def get_env(key: str, default: Optional[str] = None) -> Optional[str]:
        """Get environment variable with fallback to default."""
        return os.getenv(key, default)

    @staticmethod
    def get_env_int(key: str, default: int) -> int:
        """Get environment variable as integer with fallback to default."""
        value = os.getenv(key)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            logger.warning("Invalid integer value for %s: %s. Using default %s", key, value, default)
            return default

    @staticmethod
    def get_env_float(key: str, default: float) -> float:
        """Get environment variable as float with fallback to default."""
        value = os.getenv(key)
        if value is None:
            return default
        try:
            return float(value)
        except ValueError:
            logger.warning("Invalid float value for %s: %s. Using default %s", key, value, default)
            return default

    @staticmethod
    def get_env_bool(key: str, default: bool) -> bool:
        """Get environment variable as boolean with fallback to default."""
        value = os.getenv(key)
        if value is None:
            return default
        return value.lower() in ("true", "yes", "1")

    @staticmethod
    def get_protocol_config(protocol: str) -> ProtocolConfig:
        """Get configuration for a specific protocol."""
        return ProtocolConfig(
            name=protocol,
            alert_threshold=Config.get_env_float(f"{protocol.upper()}_ALERT_THRESHOLD", 0.95),
            critical_threshold=Config.get_env_float(f"{protocol.upper()}_CRITICAL_THRESHOLD", 0.98),
            enable_notifications=Config.get_env_bool(f"{protocol.upper()}_ENABLE_NOTIFICATIONS", True),
        )

    @classmethod
    def get_request_timeout(cls) -> int:
        """Get HTTP request timeout in seconds."""
        return cls.get_env_int("REQUEST_TIMEOUT", cls.DEFAULT_TIMEOUT)

    @classmethod
    def get_retry_count(cls) -> int:
        """Get number of retry attempts for external calls."""
        return cls.get_env_int("RETRY_COUNT", cls.DEFAULT_RETRY_COUNT)

    @classmethod
    def get_backoff_factor(cls) -> float:
        """Get backoff factor for retries."""
        return cls.get_env_float("BACKOFF_FACTOR", cls.DEFAULT_BACKOFF_FACTOR)

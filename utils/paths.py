import os

from dotenv import load_dotenv

load_dotenv()

# CACHE_DIR is the single knob for where on-disk monitoring state lives.
CACHE_DIR: str = os.getenv("CACHE_DIR", "")


def cache_path(filename: str) -> str:
    """Resolve a cache filename against CACHE_DIR."""
    return os.path.join(os.getenv("CACHE_DIR", CACHE_DIR), filename)

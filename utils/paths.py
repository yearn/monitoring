import os

from dotenv import load_dotenv

load_dotenv()

# CACHE_DIR is the single knob for where on-disk monitoring state lives.
# "" → current working directory for local runs; systemd sets /srv/cache.
CACHE_DIR: str = os.getenv("CACHE_DIR", "")


def cache_path(filename: str) -> str:
    """Resolve a cache filename against CACHE_DIR.

    Reads the ``CACHE_DIR`` env var at call time so modules that compute paths at
    their own import time (e.g. ``protocols/ustb/main.py``) still pick it up, and
    falls back to the module global ``CACHE_DIR`` when it is unset — which tests
    override via ``monkeypatch.setattr``.
    """
    return os.path.join(os.getenv("CACHE_DIR", CACHE_DIR), filename)

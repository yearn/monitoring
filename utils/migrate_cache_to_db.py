from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from automation.config import load_jobs_config
from utils import paths, store
from utils.logger import get_logger

logger = get_logger("utils.migrate_cache_to_db")

_CACHE_ENV_KEYS = ("CACHE_FILENAME", "NONCE_FILENAME", "MORPHO_FILENAME")
_DEFAULT_CACHE_FILES = {
    "CACHE_FILENAME": "cache-id.txt",
    "NONCE_FILENAME": "nonces.txt",
    "MORPHO_FILENAME": "cache-id.txt",
}


@dataclass(frozen=True)
class MigrationResult:
    files_seen: int = 0
    files_missing: int = 0
    rows_imported: int = 0
    rows_skipped: int = 0
    rows_invalid: int = 0


def known_cache_files(jobs_path: Path | None = None) -> list[Path]:
    """Return cache files used by the current deployment configuration."""
    candidates: list[str] = []
    for key in _CACHE_ENV_KEYS:
        candidates.append(os.getenv(key, _DEFAULT_CACHE_FILES[key]))

    try:
        jobs = load_jobs_config(jobs_path)
    except Exception:
        logger.debug("Could not load jobs config for migration file discovery", exc_info=True)
    else:
        for profile in jobs.profiles.values():
            for key in _CACHE_ENV_KEYS:
                value = profile.env.get(key)
                if value:
                    candidates.append(value)

    resolved: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = Path(paths.cache_path(candidate))
        if path not in seen:
            resolved.append(path)
            seen.add(path)
    return resolved


def migrate_files(files: Iterable[Path], *, overwrite: bool = False) -> MigrationResult:
    """Import legacy key:value cache files into SQLite monitor_state."""
    store.initialize_database()
    result = MigrationResult()
    for file_path in files:
        partial = migrate_file(file_path, overwrite=overwrite)
        result = MigrationResult(
            files_seen=result.files_seen + partial.files_seen,
            files_missing=result.files_missing + partial.files_missing,
            rows_imported=result.rows_imported + partial.rows_imported,
            rows_skipped=result.rows_skipped + partial.rows_skipped,
            rows_invalid=result.rows_invalid + partial.rows_invalid,
        )
    return result


def migrate_file(file_path: Path, *, overwrite: bool = False) -> MigrationResult:
    namespace = file_path.name
    if not file_path.exists():
        logger.info("cache migration: missing %s", file_path)
        return MigrationResult(files_missing=1)

    imported = 0
    skipped = 0
    invalid = 0
    with file_path.open("r") as f:
        for line_no, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            if ":" not in line:
                invalid += 1
                logger.warning("cache migration: invalid line %s:%d", file_path, line_no)
                continue
            key, value = line.split(":", 1)
            if not key:
                invalid += 1
                logger.warning("cache migration: empty key at %s:%d", file_path, line_no)
                continue
            if not overwrite and store.state_get(namespace, key) is not None:
                skipped += 1
                continue
            store.state_set(namespace, key, value)
            imported += 1

    logger.info(
        "cache migration: %s namespace=%s imported=%d skipped=%d invalid=%d",
        file_path,
        namespace,
        imported,
        skipped,
        invalid,
    )
    return MigrationResult(files_seen=1, rows_imported=imported, rows_skipped=skipped, rows_invalid=invalid)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import legacy text cache files into monitoring.db")
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        help="Additional legacy cache file to import. Relative paths resolve under CACHE_DIR.",
    )
    parser.add_argument("--jobs-yaml", type=Path, default=None, help="jobs.yaml path for profile env discovery")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing SQLite monitor_state values")
    parser.add_argument("--checkpoint", action="store_true", help="Run a WAL checkpoint after migration")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    files = known_cache_files(args.jobs_yaml)
    files.extend(Path(paths.cache_path(path)) for path in args.file)
    result = migrate_files(files, overwrite=args.overwrite)
    if args.checkpoint:
        store.checkpoint_wal()
    logger.info(
        "cache migration complete: files=%d missing=%d imported=%d skipped=%d invalid=%d db=%s",
        result.files_seen,
        result.files_missing,
        result.rows_imported,
        result.rows_skipped,
        result.rows_invalid,
        store.db_path(),
    )


if __name__ == "__main__":
    main()

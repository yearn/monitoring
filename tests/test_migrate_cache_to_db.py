from __future__ import annotations

from pathlib import Path

from utils import migrate_cache_to_db, paths, store


def _use_cache_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(paths, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(store, "_initialized", False)
    monkeypatch.setattr(store, "_initialized_path", None)


def test_known_cache_files_include_profile_env(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    jobs_yaml = tmp_path / "jobs.yaml"
    jobs_yaml.write_text(
        """
profiles:
  hourly:
    cron: "5 * * * *"
    tasks: []
  daily:
    cron: "19 8 * * *"
    env:
      CACHE_FILENAME: cache-id-daily.txt
      MORPHO_FILENAME: cache-id-daily.txt
    tasks: []
"""
    )

    files = migrate_cache_to_db.known_cache_files(jobs_yaml)
    assert files == [
        tmp_path / "cache-id.txt",
        tmp_path / "nonces.txt",
        tmp_path / "cache-id-daily.txt",
    ]


def test_migrate_file_imports_key_values_without_overwriting(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    legacy = tmp_path / "cache-id.txt"
    legacy.write_text("aave:10\nmorpho:20\ninvalid\n")
    store.state_set("cache-id.txt", "aave", "9")

    result = migrate_cache_to_db.migrate_file(legacy)

    assert result.files_seen == 1
    assert result.rows_imported == 1
    assert result.rows_skipped == 1
    assert result.rows_invalid == 1
    assert store.state_get("cache-id.txt", "aave") == "9"
    assert store.state_get("cache-id.txt", "morpho") == "20"


def test_migrate_file_overwrite(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    legacy = tmp_path / "cache-id.txt"
    legacy.write_text("aave:10\n")
    store.state_set("cache-id.txt", "aave", "9")

    result = migrate_cache_to_db.migrate_file(legacy, overwrite=True)

    assert result.rows_imported == 1
    assert result.rows_skipped == 0
    assert store.state_get("cache-id.txt", "aave") == "10"


def test_migrate_missing_file(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    result = migrate_cache_to_db.migrate_files([Path(tmp_path / "missing.txt")])
    assert result.files_missing == 1
    assert store.db_path() == str(tmp_path / "monitoring.db")
    assert Path(store.db_path()).exists()

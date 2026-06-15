from __future__ import annotations

from utils import cache, paths, store


def _use_cache_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(paths, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(store, "_initialized", False)
    monkeypatch.setattr(store, "_initialized_path", None)
    monkeypatch.delenv("CACHE_BACKEND", raising=False)
    monkeypatch.delenv("CACHE_DUAL_WRITE_LEGACY", raising=False)


def test_missing_key_returns_zero(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    assert cache.get_last_value_for_key_from_file(str(tmp_path / "cache-id.txt"), "aave") == 0


def test_string_values_round_trip_and_update(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    filename = str(tmp_path / "cache-id.txt")
    cache.write_last_value_to_file(filename, "aave", "12.5")
    assert cache.get_last_value_for_key_from_file(filename, "aave") == "12.5"
    cache.write_last_value_to_file(filename, "aave", "13.5")
    assert cache.get_last_value_for_key_from_file(filename, "aave") == "13.5"


def test_int_wrappers_still_cast(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(cache, "cache_filename", str(tmp_path / "cache-id.txt"))
    cache.write_last_queued_id_to_file("aave", 10)
    assert cache.get_last_queued_id_from_file("aave") == 10


def test_namespace_is_basename(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    hourly = tmp_path / "hourly" / "cache-id.txt"
    daily = tmp_path / "daily" / "cache-id-daily.txt"
    hourly.parent.mkdir()
    daily.parent.mkdir()
    cache.write_last_value_to_file(str(hourly), "aave", 1)
    cache.write_last_value_to_file(str(daily), "aave", 2)

    assert store.state_get("cache-id.txt", "aave") == "1"
    assert store.state_get("cache-id-daily.txt", "aave") == "2"


def test_legacy_file_read_through_imports_to_sqlite(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    filename = tmp_path / "cache-id.txt"
    filename.write_text("aave:42\n")

    assert cache.get_last_value_for_key_from_file(str(filename), "aave") == "42"
    filename.unlink()
    assert cache.get_last_value_for_key_from_file(str(filename), "aave") == "42"


def test_file_backend_uses_legacy_file(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("CACHE_BACKEND", "file")
    filename = tmp_path / "cache-id.txt"

    cache.write_last_value_to_file(str(filename), "aave", 7)
    assert filename.read_text() == "aave:7\n"
    assert cache.get_last_value_for_key_from_file(str(filename), "aave") == "7"
    assert store.state_get("cache-id.txt", "aave") is None


def test_dual_write_legacy(monkeypatch, tmp_path):
    _use_cache_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("CACHE_DUAL_WRITE_LEGACY", "1")
    filename = tmp_path / "cache-id.txt"
    cache.write_last_value_to_file(str(filename), "aave", 7)

    assert store.state_get("cache-id.txt", "aave") == "7"
    assert filename.read_text() == "aave:7\n"

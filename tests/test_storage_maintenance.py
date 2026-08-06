from __future__ import annotations

import json
from datetime import date

from agent.storage_maintenance import (
    archive_old_daily_folders,
    build_storage_inventory,
    write_storage_inventory,
)


def test_storage_inventory_is_report_only_and_lists_largest_files(tmp_path):
    storage = tmp_path / "storage"
    (storage / "people" / "user").mkdir(parents=True)
    (storage / "people" / "user" / "photo.jpg").write_bytes(b"x" * 20)
    (storage / "state.sqlite3").write_bytes(b"x" * 10)

    inventory = build_storage_inventory(storage)

    assert inventory["total_bytes"] == 30
    assert inventory["file_count"] == 2
    assert inventory["bytes_by_top_level"] == {"people": 20, "state.sqlite3": 10}
    assert inventory["largest_files"][0]["path"] == "people/user/photo.jpg"
    assert inventory["policy"]["mode"] == "report_only"


def test_storage_inventory_writes_json_atomically(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "session.json").write_text("{}", encoding="utf-8")
    output = tmp_path / "dashboard" / "storage_inventory.json"

    inventory = write_storage_inventory(storage, output)

    assert json.loads(output.read_text(encoding="utf-8")) == inventory


def test_storage_archive_defaults_to_dry_run_without_removing_sources(tmp_path):
    storage = tmp_path / "storage"
    old_day = storage / "people" / "Alex" / "2026-06-01"
    old_day.mkdir(parents=True)
    (old_day / "session.json").write_text("{}", encoding="utf-8")

    result = archive_old_daily_folders(
        storage,
        before=date(2026, 7, 1),
        destination=tmp_path / "archives",
        key_path=tmp_path / "key",
    )

    assert result["mode"] == "dry_run"
    assert result["candidate_count"] == 1
    assert old_day.exists()
    assert not (tmp_path / "archives").exists()


def test_storage_archive_verifies_before_explicit_source_deletion(tmp_path):
    storage = tmp_path / "storage"
    old_day = storage / "people" / "Alex" / "2026-06-01"
    current_day = storage / "people" / "Alex" / "2026-07-15"
    old_day.mkdir(parents=True)
    current_day.mkdir(parents=True)
    (old_day / "session.json").write_text('{"old":true}', encoding="utf-8")
    (current_day / "session.json").write_text('{"current":true}', encoding="utf-8")

    result = archive_old_daily_folders(
        storage,
        before=date(2026, 7, 1),
        destination=tmp_path / "archives",
        key_path=tmp_path / "key",
        apply=True,
        delete_source=True,
    )

    assert result["deleted_source_count"] == 1
    assert result["archive_path"].endswith(".tar.gz.enc")
    assert not old_day.exists()
    assert current_day.exists()

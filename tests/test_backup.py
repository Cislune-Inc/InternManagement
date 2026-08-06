from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent.backup import create_backup, prune_backups, restore_backup, verify_backup


def _workspace(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "config").mkdir(parents=True)
    (workspace / "data").mkdir()
    day = workspace / "storage" / "people" / "Alex" / "2026-07-30"
    (day / "images").mkdir(parents=True)
    (workspace / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (workspace / "bootstrap.local.json").write_text(
        json.dumps(
            {
                "agent_config_path": "config/agent.config.json",
                "storage_root_path": "storage",
                "state_db_path": "data/agent_state.sqlite3",
            }
        ),
        encoding="utf-8",
    )
    (workspace / "config" / "agent.config.json").write_text("{}", encoding="utf-8")
    (day / "session.json").write_text('{"stage":"clocked_out"}', encoding="utf-8")
    (day / "transcript.md").write_text("Useful history", encoding="utf-8")
    (day / "images" / "photo.jpg").write_bytes(b"photo")
    with sqlite3.connect(workspace / "data" / "agent_state.sqlite3") as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('saved')")
    return workspace


def test_encrypted_backup_verifies_and_restores_to_staging(tmp_path):
    workspace = _workspace(tmp_path)
    backups = tmp_path / "backups"
    key = tmp_path / "secrets" / "backup.key"

    created = create_backup(
        workspace,
        destination=backups,
        key_path=key,
        now=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    backup_path = backups / created["backup_path"].split("/")[-1]

    assert backup_path.read_bytes()[:2] != b"\\x1f\\x8b"
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    assert verify_backup(backup_path, key_path=key)["valid"] is True

    restore = tmp_path / "restore"
    restored = restore_backup(backup_path, key_path=key, destination=restore)
    assert restored["live_files_modified"] is False
    assert (restore / "config" / "agent.config.json").exists()
    assert (restore / "storage" / "people" / "Alex" / "2026-07-30" / "session.json").exists()
    assert not (restore / "storage" / "people" / "Alex" / "2026-07-30" / "images" / "photo.jpg").exists()
    with sqlite3.connect(restore / "data" / "agent_state.sqlite3") as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "saved"


def test_image_backup_is_explicit_and_restore_destination_must_be_empty(tmp_path):
    workspace = _workspace(tmp_path)
    backups = tmp_path / "backups"
    key = tmp_path / "key"
    created = create_backup(
        workspace,
        destination=backups,
        key_path=key,
        include_images=True,
    )
    backup_path = backups / created["backup_path"].split("/")[-1]
    restore = tmp_path / "restore"
    restore_backup(backup_path, key_path=key, destination=restore)
    assert (restore / "storage" / "people" / "Alex" / "2026-07-30" / "images" / "photo.jpg").exists()
    with pytest.raises(ValueError, match="must be empty"):
        restore_backup(backup_path, key_path=key, destination=restore)


def test_prune_backups_only_removes_expired_encrypted_bundles(tmp_path):
    old = tmp_path / "old.tar.gz.enc"
    recent = tmp_path / "recent.tar.gz.enc"
    unrelated = tmp_path / "notes.txt"
    for path in (old, recent, unrelated):
        path.write_text("x", encoding="utf-8")
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    old_timestamp = (now - timedelta(days=31)).timestamp()
    os.utime(old, (old_timestamp, old_timestamp))

    removed = prune_backups(tmp_path, retention_days=30, now=now)

    assert removed == [old]
    assert not old.exists()
    assert recent.exists()
    assert unrelated.exists()

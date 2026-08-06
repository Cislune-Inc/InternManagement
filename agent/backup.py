from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .persistence import atomic_write_text

_BACKUP_SUFFIX = ".tar.gz.enc"
_CORE_STORAGE_SUFFIXES = {".csv", ".json", ".jsonl", ".md"}
_IMAGE_SUFFIXES = {".gif", ".heic", ".jpeg", ".jpg", ".png", ".webp"}


def create_backup(
    workspace_root: Path,
    *,
    destination: Path,
    key_path: Path,
    include_images: bool = False,
    retention_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    workspace_root = workspace_root.resolve()
    destination = destination.resolve()
    key_path = key_path.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    key = ensure_backup_key(key_path)
    reference = now or datetime.now(timezone.utc)
    run_id = reference.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_path = destination / f"intern-management-{run_id}{_BACKUP_SUFFIX}"

    with tempfile.TemporaryDirectory(prefix=".backup-", dir=destination) as temp_name:
        temp_root = Path(temp_name)
        snapshot_root = temp_root / "snapshot"
        sqlite_snapshot = snapshot_root / "data" / "agent_state.sqlite3"
        sqlite_snapshot.parent.mkdir(parents=True, exist_ok=True)
        bootstrap = _read_bootstrap_paths(workspace_root / "bootstrap.local.json")
        state_db_path = Path(bootstrap.get("state_db_path", "data/agent_state.sqlite3"))
        storage_root_path = Path(bootstrap.get("storage_root_path", "storage"))
        _snapshot_sqlite(workspace_root / state_db_path, sqlite_snapshot)

        live_sources = list(
            _iter_backup_sources(
                workspace_root,
                storage_root=workspace_root / storage_root_path,
                include_images=include_images,
            )
        )
        sources: list[tuple[Path, Path]] = []
        for source_path, archive_path in live_sources:
            snapshot_path = snapshot_root / archive_path
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, snapshot_path)
            sources.append((snapshot_path, archive_path))
        sources.append((sqlite_snapshot, Path("data/agent_state.sqlite3")))
        manifest_files = [
            {
                "path": str(archive_path),
                "bytes": source_path.stat().st_size,
                "sha256": _sha256_path(source_path),
            }
            for source_path, archive_path in sources
        ]
        manifest = {
            "format_version": 1,
            "created_at": reference.astimezone(timezone.utc).isoformat(),
            "workspace_root": str(workspace_root),
            "include_images": include_images,
            "file_count": len(manifest_files),
            "files": manifest_files,
            "restore_policy": "staging_only",
        }
        manifest_path = temp_root / "backup_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        archive_path = temp_root / "backup.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(manifest_path, arcname="backup_manifest.json")
            for source_path, member_path in sources:
                archive.add(source_path, arcname=str(member_path), recursive=False)
        encrypted_temp = temp_root / final_path.name
        _encrypt_archive(archive_path, encrypted_temp, key)
        os.chmod(encrypted_temp, 0o600)
        os.replace(encrypted_temp, final_path)

    removed = prune_backups(destination, retention_days=retention_days, now=reference)
    return {
        "backup_path": str(final_path),
        "created_at": manifest["created_at"],
        "file_count": manifest["file_count"],
        "bytes": final_path.stat().st_size,
        "include_images": include_images,
        "removed_expired_backups": [str(path) for path in removed],
        "key_path": str(key_path),
    }


def verify_backup(backup_path: Path, *, key_path: Path) -> dict[str, Any]:
    key = _read_backup_key(key_path)
    with tempfile.TemporaryDirectory(prefix=".backup-verify-") as temp_name:
        archive_path = Path(temp_name) / "backup.tar.gz"
        _decrypt_archive(backup_path.resolve(), archive_path, key)
        with tarfile.open(archive_path, "r:gz") as archive:
            manifest_member = archive.getmember("backup_manifest.json")
            manifest_handle = archive.extractfile(manifest_member)
            if manifest_handle is None:
                raise RuntimeError("Backup manifest could not be read.")
            manifest = json.loads(manifest_handle.read().decode("utf-8"))
            expected_files = manifest.get("files")
            if not isinstance(expected_files, list):
                raise RuntimeError("Backup manifest has no file inventory.")
            for item in expected_files:
                if not isinstance(item, dict):
                    raise RuntimeError("Backup manifest contains an invalid file entry.")
                member_name = str(item.get("path") or "")
                member = archive.getmember(member_name)
                handle = archive.extractfile(member)
                if handle is None:
                    raise RuntimeError(f"Backup member could not be read: {member_name}")
                digest = hashlib.sha256(handle.read()).hexdigest()
                if digest != str(item.get("sha256") or ""):
                    raise RuntimeError(f"Backup checksum mismatch: {member_name}")
    return {
        "valid": True,
        "backup_path": str(backup_path.resolve()),
        "created_at": manifest.get("created_at"),
        "file_count": len(expected_files),
        "include_images": bool(manifest.get("include_images")),
    }


def restore_backup(
    backup_path: Path,
    *,
    key_path: Path,
    destination: Path,
) -> dict[str, Any]:
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Restore destination must be empty.")
    verification = verify_backup(backup_path, key_path=key_path)
    destination.mkdir(parents=True, exist_ok=True)
    key = _read_backup_key(key_path)
    with tempfile.TemporaryDirectory(prefix=".backup-restore-") as temp_name:
        archive_path = Path(temp_name) / "backup.tar.gz"
        _decrypt_archive(backup_path.resolve(), archive_path, key)
        with tarfile.open(archive_path, "r:gz") as archive:
            _safe_extract(archive, destination)
    return {
        **verification,
        "restore_destination": str(destination),
        "live_files_modified": False,
    }


def ensure_backup_key(key_path: Path) -> str:
    if key_path.exists():
        return _read_backup_key(key_path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    atomic_write_text(key_path, key + "\n")
    os.chmod(key_path, 0o600)
    return key


def prune_backups(
    destination: Path,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> list[Path]:
    if retention_days <= 0:
        return []
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - timedelta(
        days=retention_days
    ).total_seconds()
    removed: list[Path] = []
    for path in destination.glob(f"*{_BACKUP_SUFFIX}"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed


def _iter_backup_sources(
    workspace_root: Path,
    *,
    storage_root: Path,
    include_images: bool,
) -> Iterable[tuple[Path, Path]]:
    fixed_paths = [
        workspace_root / ".env",
        workspace_root / "bootstrap.local.json",
    ]
    for path in fixed_paths:
        if path.is_file():
            yield path, path.relative_to(workspace_root)
    config_root = workspace_root / "config"
    if config_root.exists():
        for path in sorted(config_root.rglob("*")):
            if path.is_file():
                yield path, path.relative_to(workspace_root)
    if not storage_root.exists():
        return
    dashboard_root = storage_root / "dashboard"
    for path in sorted(storage_root.rglob("*")):
        if not path.is_file() or path.is_relative_to(dashboard_root):
            continue
        suffix = path.suffix.lower()
        if suffix in _CORE_STORAGE_SUFFIXES or (include_images and suffix in _IMAGE_SUFFIXES):
            yield path, path.relative_to(workspace_root)


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"State database does not exist: {source}")
    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)


def _read_bootstrap_paths(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Bootstrap configuration could not be read: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Bootstrap configuration must be a JSON object.")
    return payload


def _encrypt_archive(source: Path, destination: Path, key: str) -> None:
    _run_openssl(
        [
            "enc",
            "-aes-256-cbc",
            "-salt",
            "-pbkdf2",
            "-iter",
            "200000",
            "-in",
            str(source),
            "-out",
            str(destination),
        ],
        key,
    )


def _decrypt_archive(source: Path, destination: Path, key: str) -> None:
    _run_openssl(
        [
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "200000",
            "-in",
            str(source),
            "-out",
            str(destination),
        ],
        key,
    )


def _run_openssl(arguments: list[str], key: str) -> None:
    executable = shutil.which("openssl")
    if executable is None:
        raise RuntimeError("OpenSSL is required for encrypted backups.")
    environment = os.environ.copy()
    environment["BACKUP_OPENSSL_PASSWORD"] = key
    result = subprocess.run(
        [executable, *arguments, "-pass", "env:BACKUP_OPENSSL_PASSWORD"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "OpenSSL backup operation failed: "
            + (result.stderr.strip() or "unknown error")
        )


def _read_backup_key(key_path: Path) -> str:
    try:
        key = key_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Backup key could not be read: {key_path}") from exc
    if len(key) < 32:
        raise RuntimeError("Backup key is missing or too short.")
    return key


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if not target.is_relative_to(destination):
            raise RuntimeError(f"Unsafe backup member path: {member.name}")
    archive.extractall(destination, filter="data")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create and verify encrypted backups.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--verify", type=Path)
    action.add_argument("--restore", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--destination", type=Path, default=Path("backups"))
    parser.add_argument("--key", type=Path, default=Path("secrets/backup.key"))
    parser.add_argument("--include-images", action="store_true")
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--restore-to", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.verify:
        result = verify_backup(args.verify, key_path=args.key)
    elif args.restore:
        if args.restore_to is None:
            raise SystemExit("--restore-to is required with --restore.")
        result = restore_backup(
            args.restore,
            key_path=args.key,
            destination=args.restore_to,
        )
    else:
        result = create_backup(
            args.workspace,
            destination=args.destination,
            key_path=args.key,
            include_images=args.include_images,
            retention_days=args.retention_days,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

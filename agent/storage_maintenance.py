from __future__ import annotations

import argparse
import json
import os
import shutil
import tarfile
import tempfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from agent.backup import (
    _decrypt_archive,
    _encrypt_archive,
    _read_backup_key,
    ensure_backup_key,
)
from agent.persistence import atomic_write_json


def build_storage_inventory(storage_root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    bytes_by_suffix: Counter[str] = Counter()
    bytes_by_top_level: Counter[str] = Counter()

    if storage_root.exists():
        for path in storage_root.rglob("*"):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            relative = path.relative_to(storage_root)
            suffix = path.suffix.lower() or "[no extension]"
            top_level = relative.parts[0] if relative.parts else "[root]"
            bytes_by_suffix[suffix] += size
            bytes_by_top_level[top_level] += size
            files.append(
                {
                    "path": str(relative),
                    "bytes": size,
                    "human": _human_bytes(size),
                }
            )

    files.sort(key=lambda item: item["bytes"], reverse=True)
    total_bytes = sum(item["bytes"] for item in files)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "storage_root": str(storage_root.resolve()),
        "total_bytes": total_bytes,
        "total_human": _human_bytes(total_bytes),
        "file_count": len(files),
        "bytes_by_top_level": dict(bytes_by_top_level.most_common()),
        "bytes_by_suffix": dict(bytes_by_suffix.most_common()),
        "largest_files": files[:100],
        "policy": {
            "mode": "report_only",
            "note": (
                "This inventory never deletes source sessions, transcripts, or images. "
                "Archive or deletion requires a separate explicit operator decision."
            ),
        },
    }


def write_storage_inventory(storage_root: Path, output_path: Path) -> dict[str, Any]:
    inventory = build_storage_inventory(storage_root)
    atomic_write_json(output_path, inventory)
    return inventory


def archive_old_daily_folders(
    storage_root: Path,
    *,
    before: date,
    destination: Path,
    key_path: Path,
    apply: bool = False,
    delete_source: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    candidates = _daily_folder_candidates(storage_root, before)
    total_bytes = sum(_directory_bytes(path) for path in candidates)
    result: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "before": before.isoformat(),
        "candidate_count": len(candidates),
        "candidate_bytes": total_bytes,
        "candidate_human": _human_bytes(total_bytes),
        "delete_source": bool(delete_source),
        "archive_path": None,
        "deleted_source_count": 0,
    }
    if not apply or not candidates:
        return result

    destination.mkdir(parents=True, exist_ok=True)
    key = ensure_backup_key(key_path)
    reference = now or datetime.now(timezone.utc)
    run_id = reference.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = destination / (
        f"daily-source-before-{before.isoformat()}-{run_id}.tar.gz.enc"
    )
    storage_root = storage_root.resolve()
    with tempfile.TemporaryDirectory(prefix=".storage-archive-", dir=destination) as temp:
        temp_root = Path(temp)
        manifest = {
            "format_version": 1,
            "created_at": reference.isoformat(),
            "storage_root": str(storage_root),
            "before": before.isoformat(),
            "folders": [
                str(path.resolve().relative_to(storage_root))
                for path in candidates
            ],
        }
        manifest_path = temp_root / "storage_archive_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        plain_archive = temp_root / "source.tar.gz"
        with tarfile.open(plain_archive, "w:gz") as archive:
            archive.add(
                manifest_path,
                arcname="storage_archive_manifest.json",
            )
            for folder in candidates:
                archive.add(
                    folder,
                    arcname=str(folder.resolve().relative_to(storage_root)),
                )
        encrypted = temp_root / archive_path.name
        _encrypt_archive(plain_archive, encrypted, key)
        os.chmod(encrypted, 0o600)
        _verify_storage_archive(
            encrypted,
            key_path=key_path,
            expected_folders=len(candidates),
        )
        os.replace(encrypted, archive_path)

    result["archive_path"] = str(archive_path.resolve())
    if delete_source:
        for folder in candidates:
            shutil.rmtree(folder)
        result["deleted_source_count"] = len(candidates)
    return result


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def _daily_folder_candidates(storage_root: Path, before: date) -> list[Path]:
    people = storage_root / "people"
    candidates: list[Path] = []
    if not people.exists():
        return candidates
    for person in people.iterdir():
        if not person.is_dir():
            continue
        for daily in person.iterdir():
            if not daily.is_dir():
                continue
            try:
                daily_date = date.fromisoformat(daily.name)
            except ValueError:
                continue
            if daily_date < before:
                candidates.append(daily)
    return sorted(candidates)


def _directory_bytes(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def _verify_storage_archive(
    archive_path: Path,
    *,
    key_path: Path,
    expected_folders: int,
) -> None:
    key = _read_backup_key(key_path)
    with tempfile.TemporaryDirectory(prefix=".storage-verify-") as temp:
        plain = Path(temp) / "source.tar.gz"
        _decrypt_archive(archive_path, plain, key)
        with tarfile.open(plain, "r:gz") as archive:
            manifest_handle = archive.extractfile("storage_archive_manifest.json")
            if manifest_handle is None:
                raise RuntimeError("Storage archive manifest is missing.")
            manifest = json.loads(manifest_handle.read().decode("utf-8"))
            folders = manifest.get("folders")
            if not isinstance(folders, list) or len(folders) != expected_folders:
                raise RuntimeError("Storage archive folder inventory is incomplete.")
            names = set(archive.getnames())
            for folder in folders:
                if not any(name == folder or name.startswith(f"{folder}/") for name in names):
                    raise RuntimeError(f"Storage archive is missing folder: {folder}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a non-destructive inventory of local storage usage."
    )
    parser.add_argument("--storage-root", type=Path, default=Path("storage"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("storage/dashboard/system/storage_inventory.json"),
    )
    parser.add_argument("--archive-before", type=date.fromisoformat)
    parser.add_argument(
        "--archive-destination",
        type=Path,
        default=Path("backups/storage-archives"),
    )
    parser.add_argument("--key", type=Path, default=Path("secrets/backup.key"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delete-source", action="store_true")
    args = parser.parse_args()

    if args.delete_source and not args.apply:
        parser.error("--delete-source requires --apply.")
    if args.archive_before:
        result = archive_old_daily_folders(
            args.storage_root,
            before=args.archive_before,
            destination=args.archive_destination,
            key_path=args.key,
            apply=args.apply,
            delete_source=args.delete_source,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    inventory = write_storage_inventory(args.storage_root, args.output)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "total": inventory["total_human"],
                "files": inventory["file_count"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

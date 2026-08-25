from __future__ import annotations

import argparse
import asyncio
import csv
import json
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .models import SessionState, UserProfile
from .runtime import InternManagementRuntime
from .time_utils import resolve_timezone

_RUN_ROOT = Path("dashboard") / "time_tracking" / "lunch_deduction_cleanup"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recompute archived time summaries using only recorded lunch intervals."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Write only audit output. This is the default.")
    mode.add_argument("--apply", action="store_true", help="Rewrite changed archived sessions and rebuild reports.")
    parser.add_argument("--user", help="Optional user_key filter.")
    parser.add_argument("--from", dest="date_from", help="Optional start session_date, YYYY-MM-DD.")
    parser.add_argument("--to", dest="date_to", help="Optional end session_date, YYYY-MM-DD.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    load_dotenv()
    runtime = InternManagementRuntime()
    asyncio.run(runtime.refresh_configuration(force=True))
    now = datetime.now(tz=resolve_timezone(runtime.runtime_timezone_name()))
    summary = asyncio.run(_run_cleanup(runtime, args=args, now=now))
    print(json.dumps(summary, indent=2, sort_keys=True))


async def _run_cleanup(
    runtime: InternManagementRuntime,
    *,
    args: argparse.Namespace,
    now: datetime,
) -> dict[str, Any]:
    storage_root = runtime._storage_root_path()
    if storage_root is None:
        raise SystemExit("Storage root is unavailable.")
    run_id = now.strftime("%Y%m%d_%H%M%S")
    run_root = storage_root / _RUN_ROOT / run_id
    backup_root = run_root / "original_sessions"
    run_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    changed_count = 0
    rewritten_count = 0
    adjusted_lunch_count = 0
    total_recorded_lunch_seconds = 0
    for user, session_path in _iter_archived_session_paths(storage_root):
        if args.user and user.user_key != args.user:
            continue
        session_date = session_path.parent.name
        if args.date_from and session_date < args.date_from:
            continue
        if args.date_to and session_date > args.date_to:
            continue
        try:
            session = SessionState(**json.loads(session_path.read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            rows.append(
                {
                    "user_key": user.user_key,
                    "display_name": user.display_name,
                    "session_date": session_date,
                    "changed": False,
                    "error": f"could not read session: {exc}",
                    "session_path": str(session_path.resolve()),
                    "backup_path": "",
                }
            )
            continue
        runtime._normalize_session_state(session, user=user)
        before = dict(session.time_summary)
        summary_now = runtime._session_time_summary_reference_now(session, user, now)
        runtime._refresh_session_time_summary(session, summary_now)
        after = dict(session.time_summary)
        before_deducted_seconds = int(
            before.get("unpaid_lunch_deducted_seconds") or 0
        )
        after_deducted_seconds = int(
            after.get("unpaid_lunch_deducted_seconds") or 0
        )
        lunch_adjustment_seconds = after_deducted_seconds - before_deducted_seconds
        if lunch_adjustment_seconds:
            adjusted_lunch_count += 1
        total_recorded_lunch_seconds += after_deducted_seconds
        changed = before != after
        if changed:
            changed_count += 1
        backup_path = ""
        if args.apply and changed:
            backup_path_obj = backup_root / user.storage_folder_name / session_date / "session.json"
            backup_path_obj.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(session_path, backup_path_obj)
            backup_path = str(backup_path_obj.resolve())
            session_path.write_text(json.dumps(asdict(session), indent=2, sort_keys=True), encoding="utf-8")
            runtime.state_store.save_session(session)
            rewritten_count += 1
        rows.append(
            {
                "user_key": user.user_key,
                "display_name": user.display_name,
                "session_date": session.session_date,
                "changed": changed,
                "clocked_in_before_seconds": int(before.get("clocked_in_total_seconds") or 0),
                "clocked_in_after_seconds": int(after.get("clocked_in_total_seconds") or 0),
                "gross_clocked_in_after_seconds": int(after.get("gross_clocked_in_total_seconds") or 0),
                "unpaid_lunch_deducted_before_seconds": before_deducted_seconds,
                "unpaid_lunch_deducted_after_seconds": after_deducted_seconds,
                "lunch_adjustment_seconds": lunch_adjustment_seconds,
                "task_tracked_after_seconds": int(after.get("task_tracked_total_seconds") or 0),
                "session_path": str(session_path.resolve()),
                "backup_path": backup_path,
                "error": "",
            }
        )
    audit_path = run_root / "audit.csv"
    _write_audit_csv(audit_path, rows)
    if args.apply:
        await runtime._write_time_tracking_csv(now=now)
    summary = {
        "run_id": run_id,
        "apply": bool(args.apply),
        "processed_days": len(rows),
        "changed_days": changed_count,
        "adjusted_lunch_days": adjusted_lunch_count,
        "total_recorded_lunch_seconds": total_recorded_lunch_seconds,
        "rewritten_days": rewritten_count,
        "audit_csv_path": str(audit_path.resolve()),
        "run_root": str(run_root.resolve()),
    }
    (run_root / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def _iter_archived_session_paths(storage_root: Path) -> list[tuple[UserProfile, Path]]:
    people_dir = storage_root / "people"
    if not people_dir.exists():
        return []
    sessions: list[tuple[UserProfile, Path]] = []
    for profile_path in sorted(people_dir.glob("*/profile.json"), key=lambda path: str(path).lower()):
        try:
            user = UserProfile(**json.loads(profile_path.read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        for session_path in sorted(profile_path.parent.glob("*/session.json")):
            sessions.append((user, session_path))
    return sessions


def _write_audit_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "user_key",
        "display_name",
        "session_date",
        "changed",
        "clocked_in_before_seconds",
        "clocked_in_after_seconds",
        "gross_clocked_in_after_seconds",
        "unpaid_lunch_deducted_before_seconds",
        "unpaid_lunch_deducted_after_seconds",
        "lunch_adjustment_seconds",
        "task_tracked_after_seconds",
        "session_path",
        "backup_path",
        "error",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


if __name__ == "__main__":
    main()

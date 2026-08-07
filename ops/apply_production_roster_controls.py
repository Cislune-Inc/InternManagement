from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.config import parse_roster_bytes


CONTROL_FIELDS = (
    "worker_type",
    "work_location",
    "labor_jurisdiction",
    "compensation_plan",
    "time_tracking_required",
    "meal_tracking_required",
    "overtime_approval_required",
)
AJ_SLACK_USER_ID = "U095NMY2U4R"
AJ_FOCUS_AREAS = ("Lockheed Bagworm", "LM_Nightjar", "shop organization")
AJ_ROSTER_DEFAULTS = {
    "user_key": "AJ",
    "display_name": "AJ Torres",
    "discord_user_id": "",
    "discord_username": "",
    "storage_folder_name": "AJ Torres",
    "timezone": "America/Los_Angeles",
    "clickup_user_id": "",
    "clickup_user_email": "ajtorres@caltech.edu",
    "slack_user_id": AJ_SLACK_USER_ID,
    "active": "true",
    "preferred_transport": "slack",
    "worker_type": "intern",
    "work_location": "Rosemead, CA",
    "labor_jurisdiction": "California",
    "compensation_plan": "nasa_stipend",
    "time_tracking_required": "true",
    "meal_tracking_required": "true",
    "overtime_approval_required": "true",
    "expected_daily_hours": "8",
    "weekly_target_hours": "40",
    "regular_workdays": "monday;tuesday;wednesday;thursday;friday",
    "typical_start_time": "09:00",
    "typical_end_time": "17:00",
    "planned_time_off": "",
    "interests": ";".join(AJ_FOCUS_AREAS),
    "skills": "",
}
OVERTIME_EXEMPT_WORKER_TYPES = {"salaried", "exempt", "external", "contractor"}


def _is_active(row: dict[str, Any]) -> bool:
    return str(row.get("active", "true")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _default_compensation_plan(row: dict[str, Any]) -> str:
    worker_type = str(row.get("worker_type") or "intern").strip().lower()
    if worker_type in {"salaried", "exempt"}:
        return "salary"
    if worker_type in {"external", "contractor"}:
        return "external"
    if str(row.get("gusto_entity_uuid") or "").strip():
        return "cislune_hourly"
    return "needs_review"


def _matches_aj(row: dict[str, Any]) -> bool:
    identifiers = {
        str(row.get("display_name") or "").strip().casefold(),
        str(row.get("user_key") or "").strip().casefold(),
        str(row.get("discord_username") or "").strip().casefold(),
    }
    return bool({"aj", "aj torres", "ajtorres"}.intersection(identifiers))


def _append_semicolon_values(current: Any, additions: tuple[str, ...]) -> str:
    values = [item.strip() for item in str(current or "").split(";") if item.strip()]
    normalized = {item.casefold() for item in values}
    values.extend(item for item in additions if item.casefold() not in normalized)
    return ";".join(values)


def apply_roster_controls(
    rows: list[dict[str, Any]],
    fieldnames: list[str],
    *,
    ensure_aj: bool = False,
) -> tuple[list[str], list[str]]:
    output_fields = list(fieldnames)
    for field_name in (*CONTROL_FIELDS, "slack_user_id", "preferred_transport", "interests"):
        if field_name not in output_fields:
            output_fields.append(field_name)

    changed: list[str] = []
    if ensure_aj and not any(_matches_aj(row) for row in rows):
        for field_name in AJ_ROSTER_DEFAULTS:
            if field_name not in output_fields:
                output_fields.append(field_name)
        aj_row = {field_name: "" for field_name in output_fields}
        aj_row.update(AJ_ROSTER_DEFAULTS)
        rows.append(aj_row)
        changed.append("added AJ Torres")
    for index, row in enumerate(rows, start=2):
        if not _is_active(row):
            continue
        desired_values = {
            "worker_type": str(row.get("worker_type") or "intern").strip(),
            "work_location": str(row.get("work_location") or "Rosemead, CA").strip(),
            "labor_jurisdiction": str(
                row.get("labor_jurisdiction") or "California"
            ).strip(),
            "compensation_plan": str(row.get("compensation_plan") or "").strip()
            or _default_compensation_plan(row),
            "time_tracking_required": "true",
            "meal_tracking_required": "true",
            "overtime_approval_required": (
                "false"
                if str(row.get("worker_type") or "intern").strip().lower()
                in OVERTIME_EXEMPT_WORKER_TYPES
                else "true"
            ),
        }
        for field_name, desired in desired_values.items():
            current = str(row.get(field_name) or "").strip().lower()
            if current == desired.lower():
                continue
            row[field_name] = desired
            changed.append(f"row {index}: {field_name}")
        if _matches_aj(row):
            aj_values = {
                "slack_user_id": AJ_SLACK_USER_ID,
                "preferred_transport": "slack",
                "interests": _append_semicolon_values(row.get("interests"), AJ_FOCUS_AREAS),
            }
            for field_name, desired in aj_values.items():
                if str(row.get(field_name) or "").strip() == desired:
                    continue
                row[field_name] = desired
                changed.append(f"row {index}: {field_name}")
    return output_fields, changed


def _render_roster(rows: list[dict[str, Any]], fieldnames: list[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply reviewed Don Pollo time-tracking and compensation controls."
    )
    parser.add_argument("--roster", type=Path, default=Path("config/roster.csv"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    roster_path = args.roster.resolve()
    with roster_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    if not fieldnames:
        raise ValueError("Roster CSV is missing a header row.")

    output_fields, changed = apply_roster_controls(rows, fieldnames, ensure_aj=True)
    rendered = _render_roster(rows, output_fields)
    parse_roster_bytes(roster_path.name, rendered)

    active_count = sum(_is_active(row) for row in rows)
    plan_counts: dict[str, int] = {}
    for row in rows:
        if not _is_active(row):
            continue
        plan = str(row.get("compensation_plan") or "needs_review")
        plan_counts[plan] = plan_counts.get(plan, 0) + 1
    print(f"Validated {active_count} active roster users.")
    print("Compensation plans: " + ", ".join(f"{key}={value}" for key, value in sorted(plan_counts.items())))
    if not changed:
        print("Production roster already matches the reviewed controls.")
        return 0
    print(f"Roster values to update: {len(changed)}")
    if not args.apply:
        print("Dry run only; pass --apply to write the roster.")
        return 0

    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    backup_path = roster_path.with_name(
        f"{roster_path.stem}.before-ops-controls.{timestamp}{roster_path.suffix}"
    )
    shutil.copy2(roster_path, backup_path)
    original_mode = stat.S_IMODE(roster_path.stat().st_mode)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{roster_path.name}.",
        suffix=".tmp",
        dir=roster_path.parent,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, original_mode)
        os.replace(temporary_name, roster_path)
    finally:
        temporary_path = Path(temporary_name)
        if temporary_path.exists():
            temporary_path.unlink()
    print(f"Applied production roster controls. Previous roster: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

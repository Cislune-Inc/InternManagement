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
    "compensation_plan",
    "time_tracking_required",
    "meal_tracking_required",
    "overtime_approval_required",
)
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


def apply_roster_controls(
    rows: list[dict[str, Any]],
    fieldnames: list[str],
) -> tuple[list[str], list[str]]:
    output_fields = list(fieldnames)
    for field_name in CONTROL_FIELDS:
        if field_name not in output_fields:
            output_fields.append(field_name)

    changed: list[str] = []
    for index, row in enumerate(rows, start=2):
        if not _is_active(row):
            continue
        desired_values = {
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

    output_fields, changed = apply_roster_controls(rows, fieldnames)
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

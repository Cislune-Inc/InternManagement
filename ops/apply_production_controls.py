from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


CONTROL_VALUES: dict[str, dict[str, Any]] = {
    "schedule": {
        "auto_clock_out_after_hours": 1,
        "auto_clock_out_warning_minutes": 15,
    },
    "labor": {
        "short_rest_break_minutes": 10,
        "meal_minimum_minutes": 30,
        "meal_warning_after_hours": 4.5,
        "meal_auto_pause_after_hours": 5.0,
        "overtime_limit_hours": 8.0,
        "overtime_warning_minutes": 30,
        "auto_clock_out_at_overtime_limit": True,
    },
    "clickup": {
        "new_task_approval_required": True,
        "new_task_approver_names": ["Erik", "George"],
    },
    "slack": {
        "operational_digest_interval_minutes": 1440,
        "operational_digest_hour": 8,
        "operational_digest_timezone": "America/Los_Angeles",
        "manager_queue_url": "http://192.168.4.87:8765/exceptions",
        "practice_channel_id": None,
        "quarantine_uncertain_routes": True,
        "thread_daily_updates": True,
    },
}

ADMIN_SLACK_USER_IDS = {
    "erik": "U01SWQKDTBM",
    "george": "U0AEC5J2SJD",
}


def _apply_admin_slack_user_ids(payload: dict[str, Any], changed: list[str]) -> None:
    admins = payload.get("admins")
    if isinstance(admins, list) and admins:
        for index, admin in enumerate(admins):
            if not isinstance(admin, dict):
                continue
            normalized_name = str(admin.get("name") or "").strip().lower()
            slack_user_id = ADMIN_SLACK_USER_IDS.get(normalized_name)
            if not slack_user_id or admin.get("slack_user_id") == slack_user_id:
                continue
            admin["slack_user_id"] = slack_user_id
            changed.append(f"admins[{index}].slack_user_id")
        return

    normalized_name = str(payload.get("admin_display_name") or "").strip().lower()
    slack_user_id = ADMIN_SLACK_USER_IDS.get(normalized_name)
    if slack_user_id and payload.get("admin_slack_user_id") != slack_user_id:
        payload["admin_slack_user_id"] = slack_user_id
        changed.append("admin_slack_user_id")


def apply_controls(payload: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for section_name, values in CONTROL_VALUES.items():
        section = payload.setdefault(section_name, {})
        if not isinstance(section, dict):
            raise ValueError(f"{section_name} must be a JSON object.")
        for key, value in values.items():
            if section.get(key) == value:
                continue
            section[key] = value
            changed.append(f"{section_name}.{key}")
    _apply_admin_slack_user_ids(payload, changed)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply the reviewed Don Pollo production control values."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/agent.config.json"),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    config_path = args.config.resolve()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Agent config must be a JSON object.")
    changed = apply_controls(payload)
    if not changed:
        print("Production controls already match the reviewed values.")
        return 0
    print("Control values to update:")
    for key in changed:
        print(f"- {key}")
    if not args.apply:
        print("Dry run only; pass --apply to write the config.")
        return 0

    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    backup_path = config_path.with_name(
        f"{config_path.stem}.before-ops-controls.{timestamp}{config_path.suffix}"
    )
    shutil.copy2(config_path, backup_path)
    original_mode = stat.S_IMODE(config_path.stat().st_mode)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.",
        suffix=".tmp",
        dir=config_path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, original_mode)
        os.replace(temporary_name, config_path)
    finally:
        temporary_path = Path(temporary_name)
        if temporary_path.exists():
            temporary_path.unlink()
    print(f"Applied production controls. Previous config: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

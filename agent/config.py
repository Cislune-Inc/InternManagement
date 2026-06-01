from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .models import (
    AdminConsoleConfig,
    AdminProfile,
    AgentConfig,
    BootstrapConfig,
    ClickUpConfig,
    PromptConfig,
    ScheduleConfig,
    UserProfile,
)
from .time_utils import parse_local_clock_time, resolve_timezone

_UNSUPPORTED_ROSTER_COLUMNS = {"clickup_task_id", "clickup_list_id"}


def load_bootstrap() -> BootstrapConfig:
    load_dotenv()
    bootstrap_path = Path(os.environ.get("BOOTSTRAP_PATH", "bootstrap.local.json"))
    payload = json.loads(bootstrap_path.read_text(encoding="utf-8"))
    return BootstrapConfig(
        agent_config_path=Path(payload.get("agent_config_path", "config/agent.config.json")),
        storage_root_path=Path(payload.get("storage_root_path", "storage")),
        state_db_path=Path(payload.get("state_db_path", "data/agent_state.sqlite3")),
        default_timezone=payload.get("default_timezone", "America/Los_Angeles"),
    )


def parse_agent_config(payload: dict[str, Any], fallback_timezone: str) -> AgentConfig:
    schedule = payload.get("schedule", {})
    prompts = payload.get("prompts", {})
    clickup = payload.get("clickup", {})
    admin_console = payload.get("admin_console", {})
    follow_up_interval_minutes = schedule.get("follow_up_interval_minutes")
    if follow_up_interval_minutes is None:
        follow_up_interval_minutes = int(schedule.get("follow_up_interval_hours", 2)) * 60
    admins_payload = payload.get("admins", [])
    admins = _parse_admin_profiles(admins_payload)
    primary_admin_id = int(payload["admin_discord_user_id"])
    if not admins:
        admins = [
            AdminProfile(
                name=str(payload.get("admin_display_name") or "Admin"),
                discord_user_id=primary_admin_id,
            )
        ]
    timezone_name = _validate_timezone_name(payload.get("timezone", fallback_timezone), "agent config timezone")
    workday_rollover_time = _normalize_local_clock_time(schedule.get("workday_rollover_time", "03:30"))
    return AgentConfig(
        timezone=timezone_name,
        admin_discord_user_id=primary_admin_id,
        roster_file_name=payload.get("roster_file_name", "roster.csv"),
        dashboard_file_name=payload.get("dashboard_file_name", "dashboard.md"),
        schedule=ScheduleConfig(
            workdays=list(schedule.get("workdays", [0, 1, 2, 3, 4])),
            clock_in_hour=int(schedule.get("clock_in_hour", 9)),
            clock_in_cutoff_hour=int(schedule.get("clock_in_cutoff_hour", 13)),
            workday_rollover_time=workday_rollover_time,
            follow_up_interval_minutes=int(follow_up_interval_minutes),
            inactivity_minutes=int(schedule.get("inactivity_minutes", 10)),
            stuck_alert_after_hours=int(schedule.get("stuck_alert_after_hours", 4)),
            task_onboarding_interval_minutes=int(schedule.get("task_onboarding_interval_minutes", 5)),
            auto_clock_out_after_hours=int(schedule.get("auto_clock_out_after_hours", 6)),
        ),
        clickup=ClickUpConfig(
            workspace_id=str(clickup["workspace_id"]),
            default_list_id=clickup.get("default_list_id"),
            mission_board_list_id=clickup.get("mission_board_list_id"),
            comment_updates=bool(clickup.get("comment_updates", True)),
            attach_images_to_tasks=bool(clickup.get("attach_images_to_tasks", False)),
            auto_status_updates=bool(clickup.get("auto_status_updates", True)),
            create_time_entries=bool(clickup.get("create_time_entries", True)),
            custom_fields=dict(clickup.get("custom_fields", {})),
        ),
        prompts=PromptConfig(
            clock_in=prompts.get("clock_in", "Have you clocked in yet?"),
            clock_in_reminder=prompts.get(
                "clock_in_reminder", "Checking back in. Have you clocked in yet?"
            ),
            plan_question=prompts.get(
                "plan_question", "Tell me what you are planning on accomplishing today."
            ),
            start_photo_question=prompts.get(
                "start_photo_question", "Send me a picture of your project before you start."
            ),
            risk_question=prompts.get("risk_question", "What might slow you down today, if anything?"),
            follow_up_questions=list(
                prompts.get(
                    "follow_up_questions",
                    [
                        "How is the project going? Are you stuck?",
                        "What progress have you made since the last check-in?",
                    ],
                )
            ),
            clock_out_prompt=prompts.get(
                "clock_out_prompt",
                "Before you clock out, send me a picture of what you finished and a short wrap-up.",
            ),
            lunch_break_check_in=prompts.get(
                "lunch_break_check_in",
                "Are you done with lunch break yet?",
            ),
        ),
        admin_console=AdminConsoleConfig(
            enable_ai_fallback=bool(admin_console.get("enable_ai_fallback", False)),
            menu_timeout_minutes=int(admin_console.get("menu_timeout_minutes", 10)),
        ),
        admins=admins,
    )


def parse_roster_bytes(filename: str, raw: bytes) -> list[UserProfile]:
    lower_name = filename.lower()
    if lower_name.endswith(".json"):
        payload = json.loads(raw.decode("utf-8"))
        return [parse_user_profile(row, source=f"{filename}[{index}]") for index, row in enumerate(payload, start=1)]
    if lower_name.endswith(".csv"):
        rows = csv.DictReader(raw.decode("utf-8").splitlines())
        _validate_roster_headers(filename, rows.fieldnames)
        users: list[UserProfile] = []
        for row_number, row in enumerate(rows, start=2):
            users.append(parse_user_profile(row, source=f"{filename}:{row_number}"))
        return users
    raise ValueError(f"Unsupported roster format for {filename!r}. Use .csv or .json.")


def parse_user_profile(row: dict[str, Any], source: str = "roster") -> UserProfile:
    return UserProfile(
        user_key=str(row["user_key"]).strip(),
        display_name=str(row.get("display_name") or row["user_key"]).strip(),
        discord_user_id=_parse_discord_user_id(row.get("discord_user_id"), source),
        discord_username=str(row.get("discord_username", "")).strip(),
        storage_folder_name=str(
            row.get("storage_folder_name")
            or row.get("drive_folder_name")
            or row.get("display_name")
            or row["user_key"]
        ).strip(),
        timezone=_parse_optional_timezone(row.get("timezone"), source),
        clickup_user_id=_clean_optional(row.get("clickup_user_id")),
        clickup_user_email=_clean_optional(row.get("clickup_user_email")),
        active=_parse_bool(row.get("active", True)),
    )


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _validate_roster_headers(filename: str, fieldnames: list[str] | None) -> None:
    if not fieldnames:
        return
    normalized = {str(fieldname).strip() for fieldname in fieldnames if fieldname is not None}
    unsupported = sorted(_UNSUPPORTED_ROSTER_COLUMNS.intersection(normalized))
    if not unsupported:
        return
    columns = ", ".join(unsupported)
    raise ValueError(
        f"Unsupported roster columns in {filename}: {columns}. "
        "Remove them from the roster file. ClickUp task selection is now runtime-driven from the "
        "user's ClickUp identity and session state."
    )


def _validate_timezone_name(value: Any, source: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Missing timezone in {source}. Use a valid IANA timezone name such as 'America/Los_Angeles'.")
    resolve_timezone(text)
    return text


def _parse_optional_timezone(value: Any, source: str) -> str | None:
    text = _clean_optional(value)
    if not text:
        return None
    return _validate_timezone_name(text, f"{source} timezone")


def _normalize_local_clock_time(value: Any) -> str:
    parsed = parse_local_clock_time(str(value or "03:30"))
    return f"{parsed.hour:02d}:{parsed.minute:02d}"


def _parse_admin_profiles(raw_admins: Any) -> list[AdminProfile]:
    if not isinstance(raw_admins, list):
        return []
    admins: list[AdminProfile] = []
    for index, item in enumerate(raw_admins, start=1):
        if not isinstance(item, dict):
            continue
        discord_user_id = _parse_discord_user_id(item.get("discord_user_id"), f"admins[{index}]")
        name = str(item.get("name") or f"Admin {index}").strip() or f"Admin {index}"
        admins.append(
            AdminProfile(
                name=name,
                discord_user_id=discord_user_id,
                clickup_user_id=_clean_optional(item.get("clickup_user_id")),
                clickup_user_email=_clean_optional(item.get("clickup_user_email")),
            )
        )
    return admins


def _parse_discord_user_id(value: Any, source: str) -> int:
    text = str(value or "").strip()
    match = re.fullmatch(r"<@!?(\d+)>", text)
    if match:
        text = match.group(1)
    if text.isdigit():
        return int(text)
    raise ValueError(
        f"Invalid discord_user_id {text!r} in {source}. "
        "Use the numeric Discord user ID, not the username."
    )

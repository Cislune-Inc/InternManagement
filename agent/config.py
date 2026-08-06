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
    LaborConfig,
    PromptConfig,
    ScheduleConfig,
    SlackConfig,
    SlackProjectRoute,
    UserProfile,
)
from .time_utils import parse_local_clock_time, resolve_timezone

_UNSUPPORTED_ROSTER_COLUMNS = {"clickup_task_id", "clickup_list_id"}
_DEFAULT_FOLLOW_UP_QUESTIONS = [
    "How is the project going? Are you stuck?",
    "What progress have you made since the last check-in?",
]


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
    slack = payload.get("slack", {})
    labor = payload.get("labor", {})
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
                slack_user_id=_clean_optional(payload.get("admin_slack_user_id")),
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
            auto_clock_out_warning_minutes=max(
                1,
                int(schedule.get("auto_clock_out_warning_minutes", 15)),
            ),
        ),
        clickup=ClickUpConfig(
            workspace_id=str(clickup["workspace_id"]),
            default_list_id=clickup.get("default_list_id"),
            mission_board_list_id=clickup.get("mission_board_list_id"),
            comment_updates=bool(clickup.get("comment_updates", True)),
            attach_images_to_tasks=bool(clickup.get("attach_images_to_tasks", False)),
            auto_status_updates=bool(clickup.get("auto_status_updates", True)),
            create_time_entries=bool(clickup.get("create_time_entries", True)),
            new_task_approval_required=bool(
                clickup.get("new_task_approval_required", True)
            ),
            new_task_approver_names=_clean_string_list(
                clickup.get("new_task_approver_names")
            )
            or ["Erik", "George"],
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
            follow_up_questions=_parse_follow_up_questions(prompts),
            clock_out_prompt=prompts.get(
                "clock_out_prompt",
                "Before you clock out, send me a picture of what you finished and a short wrap-up.",
            ),
            lunch_break_check_in=prompts.get(
                "lunch_break_check_in",
                "Are you done with lunch break yet?",
            ),
        ),
        slack=_parse_slack_config(slack),
        labor=_parse_labor_config(labor),
        admin_console=AdminConsoleConfig(
            enable_ai_fallback=bool(admin_console.get("enable_ai_fallback", True)),
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
        rows = csv.DictReader(raw.decode("utf-8-sig").splitlines())
        _validate_roster_headers(filename, rows.fieldnames)
        users: list[UserProfile] = []
        for row_number, row in enumerate(rows, start=2):
            users.append(parse_user_profile(row, source=f"{filename}:{row_number}"))
        return users
    raise ValueError(f"Unsupported roster format for {filename!r}. Use .csv or .json.")


def parse_user_profile(row: dict[str, Any], source: str = "roster") -> UserProfile:
    discord_user_id = _parse_optional_discord_user_id(row.get("discord_user_id"), source)
    slack_user_id = _clean_optional(row.get("slack_user_id"))
    if discord_user_id is None and slack_user_id is None:
        raise ValueError(
            f"{source} must include either a numeric discord_user_id or a Slack user ID."
        )
    preferred_transport = str(row.get("preferred_transport") or "auto").strip().lower()
    if preferred_transport not in {"auto", "discord", "slack"}:
        raise ValueError(
            f"Invalid preferred_transport {preferred_transport!r} in {source}. "
            "Use auto, discord, or slack."
        )
    worker_type = str(row.get("worker_type") or "intern").strip().lower()
    gusto_entity_uuid = _clean_optional(row.get("gusto_entity_uuid"))
    return UserProfile(
        user_key=str(row["user_key"]).strip(),
        display_name=str(row.get("display_name") or row["user_key"]).strip(),
        discord_user_id=discord_user_id,
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
        slack_user_id=slack_user_id,
        active=_parse_bool(row.get("active", True)),
        preferred_transport=preferred_transport,
        worker_type=worker_type,
        time_tracking_required=_parse_bool_default(row.get("time_tracking_required"), True),
        meal_tracking_required=_parse_bool_default(row.get("meal_tracking_required"), True),
        overtime_approval_required=_parse_bool_default(
            row.get("overtime_approval_required"),
            worker_type not in {"salaried", "exempt", "external"},
        ),
        expected_daily_hours=_parse_float_default(row.get("expected_daily_hours"), 8.0),
        check_in_interval_minutes=_parse_optional_positive_int(
            row.get("check_in_interval_minutes"),
            source,
        ),
        gusto_entity_uuid=gusto_entity_uuid,
        labor_cost_rate=_parse_optional_nonnegative_float(
            row.get("labor_cost_rate"),
            source,
        ),
    )


def _parse_follow_up_questions(prompts: dict[str, Any]) -> list[str]:
    if "follow_up_questions" not in prompts:
        return list(_DEFAULT_FOLLOW_UP_QUESTIONS)
    raw_questions = prompts.get("follow_up_questions")
    if not isinstance(raw_questions, (list, tuple)):
        raise ValueError("prompts.follow_up_questions must be a list of strings.")
    questions: list[str] = []
    for item in raw_questions:
        if not isinstance(item, str):
            raise ValueError("prompts.follow_up_questions must contain only strings.")
        text = item.strip()
        if text:
            questions.append(text)
    if not questions:
        raise ValueError("prompts.follow_up_questions must contain at least one non-empty question.")
    return questions


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _parse_bool_default(value: Any, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return _parse_bool(value)


def _parse_float_default(value: Any, default: float) -> float:
    if value is None or str(value).strip() == "":
        return default
    return float(value)


def _parse_optional_positive_int(value: Any, source: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = int(text)
    if parsed <= 0:
        raise ValueError(f"check_in_interval_minutes must be positive in {source}.")
    return parsed


def _parse_optional_nonnegative_float(value: Any, source: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = float(text)
    if parsed < 0:
        raise ValueError(f"labor_cost_rate cannot be negative in {source}.")
    return parsed


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
                slack_user_id=_clean_optional(item.get("slack_user_id")),
            )
        )
    return admins


def _parse_slack_config(raw_slack: Any) -> SlackConfig:
    if not isinstance(raw_slack, dict):
        return SlackConfig()
    return SlackConfig(
        enabled=bool(raw_slack.get("enabled", False)),
        daily_updates_enabled=bool(raw_slack.get("daily_updates_enabled", True)),
        weekly_recaps_enabled=bool(raw_slack.get("weekly_recaps_enabled", True)),
        practice_channel_id=_clean_optional(raw_slack.get("practice_channel_id")),
        default_channel_id=_clean_optional(raw_slack.get("default_channel_id")),
        unmapped_channel_id=_clean_optional(raw_slack.get("unmapped_channel_id")),
        post_start_hour=int(raw_slack.get("post_start_hour", 10)),
        post_end_hour=int(raw_slack.get("post_end_hour", 17)),
        min_post_interval_minutes=int(raw_slack.get("min_post_interval_minutes", 25)),
        max_images_per_update=max(0, int(raw_slack.get("max_images_per_update", 3))),
        weekly_recap_day=int(raw_slack.get("weekly_recap_day", 4)),
        weekly_recap_hour=int(raw_slack.get("weekly_recap_hour", 16)),
        operational_alerts_enabled=bool(
            raw_slack.get("operational_alerts_enabled", True)
        ),
        operational_alert_cooldown_minutes=max(
            1,
            int(raw_slack.get("operational_alert_cooldown_minutes", 360)),
        ),
        operational_digest_interval_minutes=max(
            15,
            int(raw_slack.get("operational_digest_interval_minutes", 360)),
        ),
        manager_queue_url=str(
            raw_slack.get("manager_queue_url")
            or "http://127.0.0.1:8765/exceptions"
        ).strip(),
        quarantine_uncertain_routes=bool(
            raw_slack.get("quarantine_uncertain_routes", True)
        ),
        thread_daily_updates=bool(raw_slack.get("thread_daily_updates", True)),
        feedback_poll_interval_minutes=max(
            15,
            int(raw_slack.get("feedback_poll_interval_minutes", 360)),
        ),
        feedback_reactions={
            str(key).strip().strip(":"): str(value).strip()
            for key, value in raw_slack.get(
                "feedback_reactions",
                SlackConfig().feedback_reactions,
            ).items()
            if str(key).strip().strip(":") and str(value).strip()
        }
        if isinstance(
            raw_slack.get("feedback_reactions", SlackConfig().feedback_reactions),
            dict,
        )
        else SlackConfig().feedback_reactions,
        positive_reactions=[
            str(item).strip().strip(":")
            for item in raw_slack.get("positive_reactions", SlackConfig().positive_reactions)
            if str(item).strip().strip(":")
        ],
        project_routes=_parse_slack_project_routes(raw_slack.get("project_routes", [])),
    )


def _parse_labor_config(raw_labor: Any) -> LaborConfig:
    if not isinstance(raw_labor, dict):
        return LaborConfig()
    return LaborConfig(
        enabled=bool(raw_labor.get("enabled", True)),
        meal_warning_after_hours=float(raw_labor.get("meal_warning_after_hours", 4.5)),
        meal_auto_pause_after_hours=float(raw_labor.get("meal_auto_pause_after_hours", 5.0)),
        overtime_limit_hours=float(raw_labor.get("overtime_limit_hours", 8.0)),
        overtime_warning_minutes=max(
            1,
            int(raw_labor.get("overtime_warning_minutes", 30)),
        ),
        auto_clock_out_at_overtime_limit=bool(
            raw_labor.get("auto_clock_out_at_overtime_limit", True)
        ),
        monday_export_hour=int(raw_labor.get("monday_export_hour", 7)),
        overhead_task_ids=_clean_string_list(raw_labor.get("overhead_task_ids")),
        overhead_name_patterns=_clean_string_list(
            raw_labor.get("overhead_name_patterns")
        )
        or LaborConfig().overhead_name_patterns,
    )


def _parse_slack_project_routes(raw_routes: Any) -> list[SlackProjectRoute]:
    if not isinstance(raw_routes, list):
        return []
    routes: list[SlackProjectRoute] = []
    for item in raw_routes:
        if not isinstance(item, dict):
            continue
        channel_id = _clean_optional(item.get("channel_id"))
        if not channel_id:
            continue
        routes.append(
            SlackProjectRoute(
                channel_id=channel_id,
                label=str(item.get("label") or "").strip(),
                clickup_task_ids=_clean_string_list(item.get("clickup_task_ids")),
                clickup_list_ids=_clean_string_list(item.get("clickup_list_ids")),
                clickup_folder_ids=_clean_string_list(item.get("clickup_folder_ids")),
                content_patterns=_clean_string_list(item.get("content_patterns")),
                task_name_patterns=_clean_string_list(item.get("task_name_patterns")),
                list_name_patterns=_clean_string_list(item.get("list_name_patterns")),
                folder_name_patterns=_clean_string_list(item.get("folder_name_patterns")),
                labor_code=str(item.get("labor_code") or "").strip(),
                budget_hours=_parse_optional_nonnegative_float(
                    item.get("budget_hours"),
                    f"Slack route {item.get('label') or channel_id}",
                ),
            )
        )
    return routes


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


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


def _parse_optional_discord_user_id(value: Any, source: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return _parse_discord_user_id(value, source)

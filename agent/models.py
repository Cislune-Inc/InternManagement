from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class BootstrapConfig:
    agent_config_path: Path
    storage_root_path: Path = Path("storage")
    state_db_path: Path = Path("data/agent_state.sqlite3")
    default_timezone: str = "America/Los_Angeles"


@dataclass(slots=True)
class ScheduleConfig:
    workdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    clock_in_hour: int = 9
    clock_in_cutoff_hour: int = 13
    workday_rollover_time: str = "03:30"
    follow_up_interval_minutes: int = 120
    inactivity_minutes: int = 10
    stuck_alert_after_hours: int = 4
    task_onboarding_interval_minutes: int = 5
    auto_clock_out_after_hours: int = 6


@dataclass(slots=True)
class PromptConfig:
    clock_in: str
    clock_in_reminder: str
    plan_question: str
    start_photo_question: str
    risk_question: str
    follow_up_questions: list[str]
    clock_out_prompt: str
    lunch_break_check_in: str = "Are you done with lunch break yet?"


@dataclass(slots=True)
class ClickUpConfig:
    workspace_id: str
    default_list_id: str | None = None
    mission_board_list_id: str | None = None
    comment_updates: bool = True
    attach_images_to_tasks: bool = False
    auto_status_updates: bool = True
    create_time_entries: bool = True
    custom_fields: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AdminConsoleConfig:
    enable_ai_fallback: bool = False
    menu_timeout_minutes: int = 10


@dataclass(slots=True)
class AdminProfile:
    name: str
    discord_user_id: int
    clickup_user_id: str | None = None
    clickup_user_email: str | None = None


@dataclass(slots=True)
class AgentConfig:
    timezone: str
    admin_discord_user_id: int
    roster_file_name: str
    dashboard_file_name: str
    schedule: ScheduleConfig
    clickup: ClickUpConfig
    prompts: PromptConfig
    admin_console: AdminConsoleConfig = field(default_factory=AdminConsoleConfig)
    admins: list[AdminProfile] = field(default_factory=list)


@dataclass(slots=True)
class UserProfile:
    user_key: str
    display_name: str
    discord_user_id: int
    discord_username: str
    storage_folder_name: str
    timezone: str | None = None
    clickup_user_id: str | None = None
    clickup_user_email: str | None = None
    active: bool = True


@dataclass(slots=True)
class AttachmentRecord:
    filename: str
    url: str
    content_type: str | None
    size: int | None
    local_path: str | None = None
    original_filename: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    analysis_model: str | None = None


@dataclass(slots=True)
class MessageRecord:
    message_id: str
    direction: str
    author_id: int
    created_at: datetime
    content: str
    attachments: list[AttachmentRecord] = field(default_factory=list)


@dataclass(slots=True)
class LocalWorkspace:
    root_dir: Path
    user_dir: Path
    daily_dir: Path
    images_dir: Path


@dataclass(slots=True)
class SessionState:
    user_key: str
    session_date: str
    work_segments: list[dict[str, str | None]] = field(default_factory=list)
    stage: str = "awaiting_clock_in"
    first_sign_of_life_at: str | None = None
    clocked_in_at: str | None = None
    intake_completed_at: str | None = None
    last_contact_at: str | None = None
    last_user_message_at: str | None = None
    last_outbound_at: str | None = None
    last_clock_in_prompt_at: str | None = None
    last_follow_up_at: str | None = None
    last_clickup_sync_at: str | None = None
    stuck_since: str | None = None
    stuck_alerted_at: str | None = None
    clocked_out_at: str | None = None
    awaiting_start_photo: bool = False
    awaiting_clock_out_photo: bool = False
    awaiting_clock_out_summary: bool = False
    pending_clickup_sync: bool = False
    latest_plan: str | None = None
    latest_status: str | None = None
    latest_blocker: str | None = None
    latest_feedback: str | None = None
    time_summary: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ClickUpContextBundle:
    context: str = ""
    active_task_id: str | None = None
    active_task_name: str | None = None
    selection_reason: str | None = None
    candidate_task_ids: list[str] = field(default_factory=list)

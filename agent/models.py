from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class SessionStage(StrEnum):
    AWAITING_CLOCK_IN = "awaiting_clock_in"
    AWAITING_TASK_SELECTION = "awaiting_task_selection"
    AWAITING_PLAN = "awaiting_plan"
    AWAITING_START_PHOTO = "awaiting_start_photo"
    AWAITING_RISK = "awaiting_risk"
    ACTIVE = "active"
    ON_LUNCH_BREAK = "on_lunch_break"
    AWAITING_ADMIN_REVIEW = "awaiting_admin_review"
    AWAITING_CLOCK_OUT_ARTIFACTS = "awaiting_clock_out_artifacts"
    CLOCKED_OUT = "clocked_out"
    DEMO_LIVE = "demo_live"


class IssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


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
    auto_clock_out_warning_minutes: int = 15


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
    new_task_approval_required: bool = True
    new_task_approver_names: list[str] = field(
        default_factory=lambda: ["Erik", "George"]
    )
    custom_fields: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class LaborConfig:
    enabled: bool = True
    meal_warning_after_hours: float = 4.5
    meal_auto_pause_after_hours: float = 5.0
    overtime_limit_hours: float = 8.0
    overtime_warning_minutes: int = 30
    auto_clock_out_at_overtime_limit: bool = True
    monday_export_hour: int = 7
    overhead_task_ids: list[str] = field(default_factory=list)
    overhead_name_patterns: list[str] = field(
        default_factory=lambda: [
            r"\boverhead\b",
            r"\badministration\b",
            r"\btraining\b",
            r"\bshop cleanup\b",
            r"\bshop\b.*\b(?:cleaning|cleanup|improvement|maintenance|organization)\b",
            r"\b(?:cleaning|cleanup|improving|maintaining|organizing)\b.*\bshop\b",
            r"\bfacilit(?:y|ies)\b",
        ]
    )


@dataclass(slots=True)
class SlackProjectRoute:
    channel_id: str
    label: str = ""
    clickup_task_ids: list[str] = field(default_factory=list)
    clickup_list_ids: list[str] = field(default_factory=list)
    clickup_folder_ids: list[str] = field(default_factory=list)
    content_patterns: list[str] = field(default_factory=list)
    task_name_patterns: list[str] = field(default_factory=list)
    list_name_patterns: list[str] = field(default_factory=list)
    folder_name_patterns: list[str] = field(default_factory=list)
    labor_code: str = ""
    budget_hours: float | None = None


@dataclass(slots=True)
class SlackConfig:
    enabled: bool = False
    daily_updates_enabled: bool = True
    weekly_recaps_enabled: bool = True
    practice_channel_id: str | None = None
    default_channel_id: str | None = None
    unmapped_channel_id: str | None = None
    post_start_hour: int = 10
    post_end_hour: int = 17
    min_post_interval_minutes: int = 25
    max_images_per_update: int = 3
    weekly_recap_day: int = 4
    weekly_recap_hour: int = 16
    operational_alerts_enabled: bool = True
    operational_alert_cooldown_minutes: int = 360
    operational_digest_interval_minutes: int = 360
    manager_queue_url: str = "http://127.0.0.1:8765/exceptions"
    quarantine_uncertain_routes: bool = True
    thread_daily_updates: bool = True
    feedback_poll_interval_minutes: int = 360
    feedback_reactions: dict[str, str] = field(
        default_factory=lambda: {
            "white_check_mark": "useful",
            "x": "wrong_task_or_channel",
            "twisted_rightwards_arrows": "wrong_task_or_channel",
            "repeat": "duplicate",
            "memo": "too_detailed",
        }
    )
    positive_reactions: list[str] = field(
        default_factory=lambda: [
            "heart",
            "heart_eyes",
            "fire",
            "raised_hands",
            "clap",
            "tada",
            "star-struck",
            "rocket",
            "white_check_mark",
            "thumbsup",
        ]
    )
    project_routes: list[SlackProjectRoute] = field(default_factory=list)


@dataclass(slots=True)
class AdminConsoleConfig:
    enable_ai_fallback: bool = True
    menu_timeout_minutes: int = 10


@dataclass(slots=True)
class AdminProfile:
    name: str
    discord_user_id: int
    clickup_user_id: str | None = None
    clickup_user_email: str | None = None
    slack_user_id: str | None = None


@dataclass(slots=True)
class AgentConfig:
    timezone: str
    admin_discord_user_id: int
    roster_file_name: str
    dashboard_file_name: str
    schedule: ScheduleConfig
    clickup: ClickUpConfig
    prompts: PromptConfig
    slack: SlackConfig = field(default_factory=SlackConfig)
    labor: LaborConfig = field(default_factory=LaborConfig)
    admin_console: AdminConsoleConfig = field(default_factory=AdminConsoleConfig)
    admins: list[AdminProfile] = field(default_factory=list)


@dataclass(slots=True)
class UserProfile:
    user_key: str
    display_name: str
    discord_user_id: int | None = None
    discord_username: str = ""
    storage_folder_name: str = ""
    timezone: str | None = None
    clickup_user_id: str | None = None
    clickup_user_email: str | None = None
    slack_user_id: str | None = None
    active: bool = True
    preferred_transport: str = "auto"
    worker_type: str = "intern"
    time_tracking_required: bool = True
    meal_tracking_required: bool = True
    overtime_approval_required: bool = False
    expected_daily_hours: float = 8.0
    check_in_interval_minutes: int | None = None
    gusto_entity_uuid: str | None = None
    labor_cost_rate: float | None = None


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
    stage: str | SessionStage = SessionStage.AWAITING_CLOCK_IN
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

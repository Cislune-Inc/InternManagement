from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
import requests

from .admin_commands import AdminCommandRouter
from .advisor import Advisor, build_advisor
from .clickup_client import ClickUpClient
from .config import load_bootstrap
from .formatting import build_clickup_update, build_transcript_markdown
from .image_intelligence import ImageIntelligence, build_image_manifest
from .interface_intelligence import InterfaceIntelligence
from .local_store import LocalStore
from .models import AdminProfile, AgentConfig, AttachmentRecord, ClickUpContextBundle, MessageRecord, SessionState, UserProfile
from .operations import OperationalIssueReporter
from .persistence import atomic_write_json
from .scheduler import run_scheduler_tick
from .session_metadata import SessionMetadata
from .signals import detect_signals, is_clock_out_cancellation
from .slack_client import SlackClient
from .slack_feedback import collect_slack_update_feedback
from .slack_update_policy import SlackUpdatePolicy
from .state_store import StateStore
from .time_tracking_dashboard import write_time_tracking_dashboard
from .time_utils import ADMIN_DISPLAY_TIMEZONE, effective_workday_date, format_admin_datetime, format_user_datetime, localize_datetime, resolve_timezone
from .task_corrections import apply_operator_task_correction


_COMPLETE_HINTS = (
    "finished",
    "completed",
    "done",
    "wrapped up",
    "all set",
    "shipped",
)

_INCOMPLETE_HINTS = (
    "still need",
    "not done",
    "not finished",
    "almost done",
    "almost finished",
    "still working",
    "not yet",
    "waiting for approval",
    "waiting on approval",
    "in the meantime",
    "blocked",
    "stuck",
    "need help",
    "tomorrow",
    "next day",
)

_TASK_REVIEW_CANCEL_HINTS = (
    "not done",
    "not finished",
    "still working",
    "almost done",
    "almost finished",
    "not yet",
)

_BLOCKER_STATE_KEY = "blocker_state"
_BLOCKER_HELP_DECISION_AT_KEY = "blocker_help_decision_at"
_PENDING_FOLLOW_UP_KEY = "pending_follow_up"
_FOLLOW_UP_RESPONSE_AGGREGATION_KEY = "follow_up_response_aggregation"
_PROGRESS_PROBE_HISTORY_KEY = "progress_probe_history"
_FOLLOW_UP_PROBE_GRACE_WINDOW = timedelta(minutes=1)
_PROGRESS_PROBE_TIMEOUT = timedelta(minutes=30)
_AUTO_CLOCK_OUT_WARNING_KEY = "auto_clock_out_warning"
_SHORT_REST_ACTIVE_KEY = "short_rest_break_active"
_SHORT_REST_HISTORY_KEY = "short_rest_breaks"
_SLACK_USER_MIN_POST_INTERVAL = timedelta(minutes=90)
_TRANSCRIPT_PACIFIC_BACKFILL_MARKER = "transcript_pacific_backfill_v1.done"
_SESSION_DATE_DIRECTORY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PENDING_ADMIN_REVIEWS_KEY = "pending_admin_reviews"
_PENDING_INTERN_TASK_SWITCH_KEY = "pending_intern_task_switch"
_SELF_LOOKUP_PROMPT_KEY = "self_lookup_prompt"
_DAY_SUPPRESSION_PROMPT_KEY = "day_suppression_prompt"
_DAY_SUPPRESSION_STATE_KEY = "day_suppression"
_LUNCH_WINDOWS_KEY = "lunch_windows"
_TASK_CORRECTION_WITH_HINT_PATTERNS = (
    re.compile(r"^(?:actually\s+)?switch\s+task\s+to\s+(.+)$", re.IGNORECASE),
    re.compile(r"^(?:actually\s+)?use\s+(.+?)\s+instead\.?$", re.IGNORECASE),
    re.compile(r"^(?:actually\s+)?i\s+meant\s+(.+?)\s+instead\.?$", re.IGNORECASE),
)
_TASK_CORRECTION_NO_HINT_PATTERNS = (
    re.compile(r"^(?:actually\s+)?wrong\s+task\.?$", re.IGNORECASE),
    re.compile(r"^(?:actually\s+)?not\s+this\s+task\.?$", re.IGNORECASE),
    re.compile(r"^(?:actually\s+)?pick\s+(?:a\s+)?different\s+task\.?$", re.IGNORECASE),
    re.compile(r"^(?:actually\s+)?different\s+task\.?$", re.IGNORECASE),
)
_INTERN_TASK_SWITCH_WITH_HINT_PATTERNS = (
    re.compile(r"^(?:i\s+(?:want|need|would\s+like)\s+to\s+)?switch\s+(?:my\s+)?tasks?\s+to\s+(.+)$", re.IGNORECASE),
    re.compile(r"^(?:i\s+(?:want|need|would\s+like)\s+to\s+)?change\s+(?:my\s+)?tasks?\s+to\s+(.+)$", re.IGNORECASE),
    re.compile(r"^(?:i\s+(?:want|need|would\s+like)\s+to\s+)?work\s+on\s+(.+?)\s+instead\.?$", re.IGNORECASE),
)
_INTERN_TASK_SWITCH_BARE_PATTERNS = (
    re.compile(r"^(?:i\s+(?:want|need|would\s+like)\s+to\s+)?switch\s+(?:my\s+)?tasks?\.?$", re.IGNORECASE),
    re.compile(r"^(?:i\s+(?:want|need|would\s+like)\s+to\s+)?change\s+(?:my\s+)?tasks?\.?$", re.IGNORECASE),
    re.compile(r"^(?:i\s+(?:want|need|would\s+like)\s+to\s+)?work\s+on\s+something\s+else\.?$", re.IGNORECASE),
    re.compile(r"^(?:i\s+(?:want|need|would\s+like)\s+to\s+)?(?:pick|find)\s+another\s+objective\.?$", re.IGNORECASE),
    re.compile(r"^(?:show|list)\s+my\s+tasks?\s+so\s+i\s+can\s+switch\.?$", re.IGNORECASE),
)
_TASK_CREATION_REQUEST_PATTERNS = (
    re.compile(r"^create(?:\s+(?:a|new))?\s+task\.?$", re.IGNORECASE),
    re.compile(r"^new\s+task\.?$", re.IGNORECASE),
    re.compile(r"^none\s+of\s+these\s+fit\.?$", re.IGNORECASE),
    re.compile(r"^none\s+fit\.?$", re.IGNORECASE),
)
_TASK_CREATION_BACK_PATTERNS = (
    re.compile(r"^back\.?$", re.IGNORECASE),
    re.compile(r"^go\s+back\.?$", re.IGNORECASE),
)
_SELF_LOOKUP_HOURS_REQUEST_PATTERNS = {
    "my hours",
    "show my hours",
    "show me my hours",
    "can i see my hours",
    "let me see my hours",
    "check my hours",
    "hours today",
    "hours this week",
    "hours last week",
    "hours whole summer",
    "hours for today",
    "hours for this week",
    "hours for last week",
    "hours for the whole summer",
    "what are my hours",
    "what are my hours today",
    "what are my hours this week",
    "what are my hours last week",
    "what are my hours for today",
    "what are my hours for this week",
    "what are my hours for last week",
    "how many hours do i have",
    "how many hours do i have clocked in",
    "how many hours do i have today",
    "how many hours do i have this week",
    "how many hours do i have last week",
    "how many hours have i worked",
    "how many hours have i clocked in",
    "how many hours did i log today",
    "how many hours did i log this week",
    "how many hours did i log last week",
    "how many hours have i clocked in today",
    "how many hours have i clocked in this week",
    "how many hours have i clocked in last week",
}
_SELF_LOOKUP_HOURS_REQUEST_PREFIXES = (
    "show ",
    "show me ",
    "tell me ",
    "check ",
    "let me see ",
    "can you show ",
    "could you show ",
    "can you tell ",
    "could you tell ",
    "can i see ",
    "could i see ",
    "what ",
    "whats ",
    "how many ",
    "how much ",
    "how long ",
)
_SELF_LOOKUP_TIME_TRACKING_PHRASES = (
    "time tracking",
    "tracked time",
    "time tracked",
    "logged time",
    "time logged",
)
_SELF_LOOKUP_HOURS_REPORT_TOKENS = {
    "my",
    "have",
    "did",
    "logged",
    "clocked",
    "tracked",
    "tracking",
    "worked",
}
_SELF_LOOKUP_STATUS_REQUEST_PATTERNS = {
    "my status",
    "show my status",
    "show me my status",
    "what is my status",
    "whats my status",
    "what is my current status",
    "whats my current status",
    "show my current status",
    "show me my current status",
}
_AUTO_CLOCK_OUT_NOTIFICATION_MESSAGE = (
    "You were automatically clocked out for inactivity. "
    "If you are still working, message me so I can clock you back in."
)
_LUNCH_RETURN_TIME_PATTERN = re.compile(
    r"\b(?:at|since)\s+(\d{1,2})(?:\s*[:.]\s*|\s+)(\d{2})\s*([ap]\.?m\.?)?\b",
    re.IGNORECASE,
)
_SELF_LOOKUP_RANGE_LABELS = {
    "today": "Today",
    "this_week": "This Week",
    "last_week": "Last Week",
    "whole_summer": "Whole Summer",
}
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_SESSION_STATE_TIMESTAMP_FIELDS = (
    "first_sign_of_life_at",
    "clocked_in_at",
    "intake_completed_at",
    "last_contact_at",
    "last_user_message_at",
    "last_outbound_at",
    "last_clock_in_prompt_at",
    "last_follow_up_at",
    "last_clickup_sync_at",
    "stuck_since",
    "stuck_alerted_at",
    "clocked_out_at",
)
_TIME_TRACKING_REPORT_RELATIVE_PATH = Path("dashboard") / "time_tracking" / "time_tracking.csv"
_SLACK_UPDATE_STATE_RELATIVE_PATH = Path("dashboard") / "slack_updates" / "state.json"
_RETRO_HOURS_BACKFILL_METADATA_KEY = "retro_hours_backfill"
_TIME_TRACKING_REPORT_FIELDS = (
    "user_key",
    "display_name",
    "timezone",
    "session_date",
    "gross_clocked_in_total_seconds",
    "gross_clocked_in_total_human",
    "unpaid_lunch_deducted_seconds",
    "unpaid_lunch_deducted_human",
    "clocked_in_total_seconds",
    "clocked_in_total_human",
    "task_tracked_total_seconds",
    "task_tracked_total_human",
    "work_segment_count",
    "has_open_work_segment",
    "active_task_timer_running",
    "manual_edit_count",
    "latest_manual_edit_json",
    "retro_backfill_confidence",
    "retro_backfill_warning_count",
    "review_status",
    "review_summary",
    "review_reasons_json",
    "review_gap_seconds",
    "review_gap_human",
    "time_by_task_json",
    "session_path",
)
logger = logging.getLogger(__name__)


def _safe_storage_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().rstrip(".")
    return cleaned or "user"


@dataclass(slots=True)
class TaskActivationResult:
    task_id: str | None
    task_name: str | None
    clickup_status_name: str | None
    tracking_state: dict[str, Any]


class _InternBlockerActionButton(discord.ui.Button["_InternBlockerResolutionView"]):
    def __init__(self, label: str, action: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, style=style, custom_id=f"intern-blocker:{action}")
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return
        await view.runtime.handle_blocker_resolution_interaction(interaction, view.user_key, self.action)


class _InternBlockerResolutionView(discord.ui.View):
    def __init__(self, runtime: "InternManagementRuntime", user: UserProfile, step: str) -> None:
        super().__init__(timeout=600)
        self.runtime = runtime
        self.user_key = user.user_key
        self.user_id = user.discord_user_id
        if step == "offer_help":
            self.add_item(_InternBlockerActionButton("Ask Admin", "ask_admin", discord.ButtonStyle.primary))
            self.add_item(_InternBlockerActionButton("Draft Unblocker Task", "draft_task", discord.ButtonStyle.primary))
            self.add_item(_InternBlockerActionButton("No Help Needed", "no_help_needed", discord.ButtonStyle.secondary))
            self.add_item(_InternBlockerActionButton("Not Actually Blocked", "not_blocked", discord.ButtonStyle.secondary))
        elif step == "choose_admin":
            admins = runtime.admin_profiles()[:5]
            for admin in admins:
                self.add_item(_InternBlockerActionButton(admin.name, f"admin:{admin.name}", discord.ButtonStyle.primary))
        elif step == "declined_help_followup":
            self.add_item(_InternBlockerActionButton("Keep Blocker Logged", "keep_logged", discord.ButtonStyle.secondary))
            self.add_item(_InternBlockerActionButton("Clear It", "clear_it", discord.ButtonStyle.danger))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        if interaction.response.is_done():
            await interaction.followup.send("This blocker prompt is not for you.")
        else:
            await interaction.response.send_message("This blocker prompt is not for you.")
        return False


class _InternTaskConfirmButton(discord.ui.Button["_InternTaskConfirmationView"]):
    def __init__(self, label: str, action: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, style=style, custom_id=f"intern-task-confirm:{action}")
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return
        await view.runtime.handle_task_onboarding_confirmation_interaction(interaction, view.user_key, self.action)


class _InternTaskConfirmationView(discord.ui.View):
    def __init__(self, runtime: "InternManagementRuntime", user: UserProfile) -> None:
        super().__init__(timeout=600)
        self.runtime = runtime
        self.user_key = user.user_key
        self.user_id = user.discord_user_id
        self.add_item(_InternTaskConfirmButton("Yes, that's my task", "confirm", discord.ButtonStyle.primary))
        self.add_item(_InternTaskConfirmButton("Pick Different Task", "pick_different", discord.ButtonStyle.secondary))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        if interaction.response.is_done():
            await interaction.followup.send("This task confirmation prompt is not for you.")
        else:
            await interaction.response.send_message("This task confirmation prompt is not for you.")
        return False


class _InternQueuedReviewChoiceButton(discord.ui.Button["_InternQueuedReviewChoiceView"]):
    def __init__(self, label: str, action: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, style=style, custom_id=f"intern-review-choice:{action}")
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return
        await view.runtime.handle_queued_review_choice_interaction(interaction, view.user_key, self.action)


class _InternQueuedReviewChoiceView(discord.ui.View):
    def __init__(self, runtime: "InternManagementRuntime", user: UserProfile) -> None:
        super().__init__(timeout=600)
        self.runtime = runtime
        self.user_key = user.user_key
        self.user_id = user.discord_user_id
        self.add_item(_InternQueuedReviewChoiceButton("Switch Now", "switch_now", discord.ButtonStyle.primary))
        self.add_item(_InternQueuedReviewChoiceButton("Stay On Current Task", "stay_current", discord.ButtonStyle.secondary))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        if interaction.response.is_done():
            await interaction.followup.send("This rework decision prompt is not for you.")
        else:
            await interaction.response.send_message("This rework decision prompt is not for you.")
        return False


class _InternSelfLookupButton(discord.ui.Button["_InternSelfLookupView"]):
    def __init__(self, label: str, action: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, style=style, custom_id=f"intern-self-lookup:{action}")
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return
        await view.runtime.handle_self_lookup_interaction(
            interaction,
            view.user_key,
            view.session_date,
            self.action,
        )


class _InternSelfLookupView(discord.ui.View):
    def __init__(
        self,
        runtime: "InternManagementRuntime",
        user: UserProfile,
        *,
        session_date: str,
        step: str,
    ) -> None:
        super().__init__(timeout=600)
        self.runtime = runtime
        self.user_key = user.user_key
        self.user_id = user.discord_user_id
        self.session_date = session_date
        if step == "chooser":
            self.add_item(_InternSelfLookupButton("No", "dismiss", discord.ButtonStyle.secondary))
            self.add_item(_InternSelfLookupButton("Hours", "hours", discord.ButtonStyle.primary))
            self.add_item(_InternSelfLookupButton("Status", "status", discord.ButtonStyle.primary))
        elif step == "range":
            self.add_item(_InternSelfLookupButton("Today", "today", discord.ButtonStyle.primary))
            self.add_item(_InternSelfLookupButton("This Week", "this_week", discord.ButtonStyle.primary))
            self.add_item(_InternSelfLookupButton("Last Week", "last_week", discord.ButtonStyle.secondary))
            self.add_item(_InternSelfLookupButton("Whole Summer", "whole_summer", discord.ButtonStyle.secondary))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        if interaction.response.is_done():
            await interaction.followup.send("This info prompt is not for you.")
        else:
            await interaction.response.send_message("This info prompt is not for you.")
        return False


class _InternDaySuppressionButton(discord.ui.Button["_InternDaySuppressionView"]):
    def __init__(self, label: str, action: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, style=style, custom_id=f"intern-day-suppression:{action}")
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return
        await view.runtime.handle_day_suppression_interaction(
            interaction,
            view.user_key,
            view.session_date,
            self.action,
        )


class _InternDaySuppressionView(discord.ui.View):
    def __init__(self, runtime: "InternManagementRuntime", user: UserProfile, *, session_date: str) -> None:
        super().__init__(timeout=600)
        self.runtime = runtime
        self.user_key = user.user_key
        self.user_id = user.discord_user_id
        self.session_date = session_date
        self.add_item(
            _InternDaySuppressionButton(
                "Yes, pause today",
                "confirm",
                discord.ButtonStyle.primary,
            )
        )
        self.add_item(
            _InternDaySuppressionButton(
                "No, keep messages on",
                "cancel",
                discord.ButtonStyle.secondary,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        if interaction.response.is_done():
            await interaction.followup.send("This schedule pause prompt is not for you.")
        else:
            await interaction.response.send_message("This schedule pause prompt is not for you.")
        return False


class _AdminProgressProbeButton(discord.ui.Button["_AdminProgressProbeView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Yes, show probe replies",
            style=discord.ButtonStyle.primary,
            custom_id="admin-progress-probe:show",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return
        await view.runtime.handle_progress_probe_admin_interaction(
            interaction,
            view.user_key,
            view.session_date,
            view.probe_id,
        )


class _AdminProgressProbeView(discord.ui.View):
    def __init__(
        self,
        runtime: "InternManagementRuntime",
        admin: AdminProfile,
        *,
        user_key: str,
        session_date: str,
        probe_id: str,
    ) -> None:
        super().__init__(timeout=3600)
        self.runtime = runtime
        self.admin_user_id = admin.discord_user_id
        self.user_key = user_key
        self.session_date = session_date
        self.probe_id = probe_id
        self.add_item(_AdminProgressProbeButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.admin_user_id:
            return True
        if interaction.response.is_done():
            await interaction.followup.send("This probe notice is not for you.")
        else:
            await interaction.response.send_message("This probe notice is not for you.")
        return False

_CLICKUP_PROMPT_KEY = "clickup_prompt"
_CLOCK_OUT_RETURN_STATE_KEY = "clock_out_return_state"
_STATE_MACHINE_CHANGES_FILENAME = "state_machine_changes.jsonl"
_MANUAL_TIME_EDITS_FILENAME = "manual_time_edits.jsonl"
_LUNCH_CONFIRMATION_REQUESTED_AT_KEY = "lunch_confirmation_requested_at"
_TASK_ONBOARDING_METADATA_FIELDS = {
    "plan": "task_onboarding_plan",
    "tangible_result": "task_onboarding_tangible_result",
    "necessity": "task_onboarding_necessity",
    "effectiveness": "task_onboarding_effectiveness",
    "estimated_duration": "task_onboarding_estimated_duration",
    "reconsider_threshold": "task_onboarding_reconsider_threshold",
    "fallback_plan": "task_onboarding_fallback_plan",
}


class InternManagementRuntime:
    def __init__(self) -> None:
        self.bootstrap = load_bootstrap()
        self.state_store = StateStore(self.bootstrap.state_db_path)
        self.store = LocalStore(self.bootstrap)
        self.config: AgentConfig | None = None
        self.roster_by_discord_id: dict[int, UserProfile] = {}
        self.roster_by_slack_id: dict[str, UserProfile] = {}
        self.roster_by_key: dict[str, UserProfile] = {}
        self.clickup: ClickUpClient | None = None
        self.slack: SlackClient | None = None
        self.advisor: Advisor = build_advisor(
            os.environ.get("OPENAI_API_KEY"),
            os.environ.get("OPENAI_MODEL"),
            os.environ.get("BACKUP_OPENAI_MODEL"),
        )
        self.image_intelligence = ImageIntelligence()
        self.interface_intelligence = InterfaceIntelligence()
        self.admin_router = AdminCommandRouter(self)
        self._config_loaded_at: datetime | None = None
        self._user_session_locks: dict[str, asyncio.Lock] = {}
        self._slack_policy_instance = SlackUpdatePolicy(
            self._self_lookup_request_kind
        )
        self.operations = OperationalIssueReporter(
            state_store=self.state_store,
            config_provider=lambda: self.config,
            slack_provider=lambda: self.slack,
            admins_provider=self.admin_profiles,
            timezone_provider=self.runtime_timezone_name,
        )

    async def handle_direct_message(self, client: discord.Client, message: discord.Message) -> None:
        await self.refresh_configuration()
        if self.config and self.is_admin_user(message.author.id):
            handled = await self.admin_router.handle_message(client, message)
            if handled:
                return
        await self.handle_incoming_message(client, message)

    async def refresh_configuration(self, force: bool = False) -> None:
        now = datetime.now(tz=resolve_timezone(self.bootstrap.default_timezone))
        if not force and self._config_loaded_at and now - self._config_loaded_at < timedelta(minutes=5):
            return
        self.config = await self.store.load_agent_config()
        roster = await self.store.load_roster(self.config)
        self.roster_by_discord_id = {
            user.discord_user_id: user
            for user in roster
            if user.active and user.discord_user_id is not None
        }
        self.roster_by_slack_id = {
            user.slack_user_id: user
            for user in roster
            if user.active and user.slack_user_id
        }
        self.roster_by_key = {user.user_key: user for user in roster if user.active}
        clickup_token = os.environ.get("CLICKUP_API_TOKEN")
        self.clickup = ClickUpClient(clickup_token, self.config) if clickup_token else None
        slack_token = os.environ.get("SLACK_BOT_TOKEN")
        self.slack = SlackClient(slack_token) if slack_token and self.config.slack.enabled else None
        self._config_loaded_at = now

    async def backfill_transcripts_to_pacific_once(self) -> int:
        marker_path = self.bootstrap.state_db_path.parent / _TRANSCRIPT_PACIFIC_BACKFILL_MARKER
        if marker_path.exists():
            return 0
        return await asyncio.to_thread(self._backfill_transcripts_to_pacific_sync, marker_path)

    def _backfill_transcripts_to_pacific_sync(self, marker_path: Path) -> int:
        people_dir = self.bootstrap.storage_root_path / "people"
        rewritten = 0
        if people_dir.exists():
            for user_dir in sorted(people_dir.iterdir(), key=lambda path: path.name.lower()):
                if not user_dir.is_dir():
                    continue
                profile_path = user_dir / "profile.json"
                if not profile_path.exists():
                    continue
                try:
                    user = UserProfile(**json.loads(profile_path.read_text(encoding="utf-8")))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                for daily_dir in sorted(user_dir.iterdir(), key=lambda path: path.name):
                    if not daily_dir.is_dir() or not _SESSION_DATE_DIRECTORY_PATTERN.fullmatch(daily_dir.name):
                        continue
                    session_path = daily_dir / "session.json"
                    if not session_path.exists():
                        continue
                    try:
                        session = SessionState(**json.loads(session_path.read_text(encoding="utf-8")))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    messages = self.state_store.list_messages(user.user_key, session.session_date)
                    if not messages:
                        continue
                    transcript = build_transcript_markdown(user, session, messages)
                    (daily_dir / "transcript.md").write_text(transcript, encoding="utf-8")
                    rewritten += 1
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {
                    "completed_at": datetime.now(
                        tz=resolve_timezone(self.bootstrap.default_timezone)
                    ).isoformat(),
                    "rewritten_transcripts": rewritten,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return rewritten

    def is_admin_user(self, discord_user_id: int) -> bool:
        if not self.config:
            return False
        return discord_user_id in {admin.discord_user_id for admin in self.config.admins}

    def admin_profiles(self) -> list[AdminProfile]:
        if not self.config:
            return []
        return list(self.config.admins)

    def admin_profile_by_slack_user_id(self, slack_user_id: str) -> AdminProfile | None:
        normalized = str(slack_user_id or "").strip()
        if not normalized:
            return None
        return next(
            (
                admin
                for admin in self.admin_profiles()
                if str(admin.slack_user_id or "").strip() == normalized
            ),
            None,
        )

    def primary_admin_profile(self) -> AdminProfile | None:
        admins = self.admin_profiles()
        return admins[0] if admins else None

    def admin_display_names(self) -> list[str]:
        return [admin.name for admin in self.admin_profiles()]

    def admin_name_list_text(self) -> str:
        names = self.admin_display_names()
        if not names:
            return "the admin team"
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + f", and {names[-1]}"

    def runtime_timezone_name(self) -> str:
        return self.config.timezone if self.config else self.bootstrap.default_timezone

    def resolve_user_timezone_name(self, user: UserProfile) -> str:
        return user.timezone or self.runtime_timezone_name()

    def resolve_user_profile(self, user_key: str) -> UserProfile | None:
        direct = self.roster_by_key.get(user_key)
        if direct is not None:
            return direct
        storage_root = self._storage_root_path()
        people_dir = storage_root / "people" if storage_root is not None else None
        if people_dir is None or not people_dir.exists():
            return None
        for profile_path in sorted(people_dir.glob("*/profile.json"), key=lambda path: str(path).lower()):
            try:
                loaded = json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(loaded.get("user_key") or "").strip() != user_key:
                continue
            try:
                return UserProfile(**loaded)
            except TypeError:
                return None
        return None

    def resolve_user_local_now(
        self,
        user: UserProfile,
        moment: datetime | None = None,
    ) -> datetime:
        base_moment = moment or datetime.now(tz=resolve_timezone(self.runtime_timezone_name()))
        return localize_datetime(base_moment, self.resolve_user_timezone_name(user))

    def resolve_user_workday_date(
        self,
        user: UserProfile,
        moment: datetime | None = None,
    ) -> str:
        base_moment = moment or datetime.now(tz=resolve_timezone(self.runtime_timezone_name()))
        rollover_time = self.config.schedule.workday_rollover_time if self.config else "03:30"
        return effective_workday_date(base_moment, self.resolve_user_timezone_name(user), rollover_time)

    def get_user_session_for_moment(
        self,
        user: UserProfile,
        moment: datetime | None = None,
    ) -> tuple[SessionState, datetime]:
        local_now = self.resolve_user_local_now(user, moment)
        session_date = self.resolve_user_workday_date(user, moment)
        session = self.state_store.get_session(user.user_key, session_date)
        self._normalize_session_state(session, user=user)
        return session, local_now

    def _user_session_lock(self, user_key: str) -> asyncio.Lock:
        locks = getattr(self, "_user_session_locks", None)
        if not isinstance(locks, dict):
            locks = {}
            self._user_session_locks = locks
        lock = locks.get(user_key)
        if lock is None:
            lock = asyncio.Lock()
            locks[user_key] = lock
        return lock

    def find_admin_profiles(self, text: str) -> list[AdminProfile]:
        normalized = self._normalize_identifier_value(text)
        matches: list[AdminProfile] = []
        for admin in self.admin_profiles():
            candidates = {
                self._normalize_identifier_value(admin.name),
                self._normalize_identifier_value(str(admin.discord_user_id)),
            }
            if any(candidate and candidate in normalized for candidate in candidates):
                matches.append(admin)
        return matches

    async def describe_user_task_state(self, user: UserProfile, session: SessionState) -> str:
        if not self.clickup:
            return "ClickUp is not configured."
        tasks = await self.clickup.list_assigned_tasks(user, limit=12)
        tracking = await self._get_task_tracking_state(user, session)
        now = self.resolve_user_local_now(user)
        admin_now = localize_datetime(now, ADMIN_DISPLAY_TIMEZONE)
        tracked_total_seconds, tracked_tasks = self._tracked_time_totals(session, now)
        lines = [f"{user.display_name} task state:"]
        if tracking["active_task_name"]:
            lines.append(
                f"- Current active task: {tracking['active_task_name']} ({tracking['active_task_id']})"
            )
        else:
            lines.append("- Current active task: none confirmed")
        if session.stage == "on_lunch_break":
            lunch_started_at = str(session.metadata.get("lunch_started_at") or "").strip()
            lines.append(
                f"- Lunch break: on break since {format_admin_datetime(lunch_started_at, reference=admin_now)}"
            )
        if self._has_pending_admin_review(session):
            reviews = self._pending_admin_reviews(session)
            if len(reviews) == 1:
                review = reviews[0]
                lines.append(
                    f"- Admin review: pending for {review.get('task_name') or tracking['active_task_name'] or 'current task'}"
                )
            else:
                lines.append(f"- Admin reviews waiting: {len(reviews)}")
                for review in reviews[:4]:
                    lines.append(
                        f"  - {review.get('task_name') or 'unnamed task'}"
                        + (f" | id={review.get('task_id')}" if review.get("task_id") else "")
                    )
        if session.latest_blocker:
            blocker_state = self._blocker_state(session)
            blocker_label = ""
            if blocker_state == "blocked_no_help":
                blocker_label = " (logged, no help requested)"
            elif blocker_state == "blocked_help_requested":
                blocker_label = " (help requested)"
            lines.append(f"- Blocker: {session.latest_blocker}{blocker_label}")
        timer_line = "- Timer state: "
        if tracking["timer_running"]:
            timer_label = "ClickUp" if tracking["timer_source"] == "clickup" else "bot-managed"
            timer_line += (
                f"running via the {timer_label} timer on "
                f"{tracking['timer_task_name'] or tracking['timer_task_id'] or 'unknown task'}"
            )
            if tracking["timer_elapsed_seconds"] > 0:
                timer_line += f" for {self._format_duration(tracking['timer_elapsed_seconds'])}"
        else:
            timer_line += "no running task timer confirmed"
        if tracking["timer_note"]:
            timer_line += f" ({tracking['timer_note']})"
        lines.append(timer_line)
        if tracked_total_seconds > 0:
            lines.append(f"- Time tracked today: {self._format_duration(tracked_total_seconds)} total")
        if tracked_tasks:
            lines.append(
                "- Time by task: "
                + "; ".join(
                    f"{task_name or task_id or 'unknown task'} {self._format_duration(seconds)}"
                    for task_id, task_name, seconds in tracked_tasks[:6]
                )
            )
        if not tasks:
            lines.append("- Assigned tasks: none found")
        else:
            lines.append("- Assigned tasks:")
            for task in tasks:
                task_id = str(task.get("id") or "")
                marker = " [active]" if task_id and task_id == tracking["active_task_id"] else ""
                lines.append(
                    f"  - {task.get('name')} | id={task_id or 'unknown'} | "
                    f"status={(task.get('status') or {}).get('status') or 'unknown'} | "
                    f"priority={(task.get('priority') or {}).get('priority') or 'none'}{marker}"
                )
        return "\n".join(lines)

    def list_session_messages(
        self,
        user_key: str,
        session: SessionState,
        *,
        include_pre_reset: bool = False,
    ) -> list[MessageRecord]:
        messages = self.state_store.list_messages(user_key, session.session_date)
        if include_pre_reset:
            return messages
        reset_at = self._metadata_datetime(session, "debug_reset_at")
        if not reset_at:
            return messages
        return [message for message in messages if message.created_at >= reset_at]

    async def initiate_admin_task_switch(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        task_hint: str,
        now: datetime | None = None,
    ) -> str:
        await self.refresh_configuration()
        if not self.config or not self.clickup:
            return "ClickUp is not configured."
        previous_session = self._clone_session_state(session)
        now = now or self.resolve_user_local_now(user)
        target_task = await self.clickup.resolve_task_for_user(
            user,
            task_hint,
            include_mission_board=True,
            include_workspace=True,
        )
        if not target_task:
            return (
                f"I could not match `{task_hint}` to a ClickUp task closely enough for {user.display_name}. "
                "Ask me for their assigned tasks if you want the exact current names."
            )
        target_task_id = str(target_task.get("id") or "")
        target_task_name = str(target_task.get("name") or "Unnamed task")
        assigned = await self.clickup.ensure_task_assigned_to_user(target_task, user)
        if not assigned:
            return (
                f"I found `{target_task_name}`, but I could not confirm {user.display_name}'s ClickUp assignee ID, "
                "so I did not switch them yet."
            )
        current_task_id = self._active_task_id(session)
        if current_task_id and current_task_id == target_task_id:
            return f"{user.display_name} is already pointed at `{target_task_name}`, and I confirmed the task assignment."
        if current_task_id:
            await self._pause_current_task_tracking(user, session, now, set_hold=True, end_reason="admin_switch")
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_onboarding",
            "source": "admin_switch",
            "step": "plan",
            "task_id": target_task_id,
            "task_name": target_task_name,
            "reason": "Admin requested a task switch.",
            "draft": {},
        }
        session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
        session.latest_plan = None
        session.latest_feedback = None
        await self._send_dm(
            client,
            user,
            session,
            (
                f"Admin wants you to switch to `{target_task_name}`.\n\n"
                + self._task_onboarding_question(session.metadata[_CLICKUP_PROMPT_KEY], "plan")
            ),
            now,
        )
        await self._persist_session_state(
            user,
            session,
            now=now,
            previous_session=previous_session,
            trigger="admin_task_switch",
            details={
                "task_hint": task_hint,
                "target_task_id": target_task_id,
                "target_task_name": target_task_name,
            },
        )
        await self.write_dashboard()
        return f"Started a task-switch onboarding flow for {user.display_name} on `{target_task_name}`."

    async def apply_operator_task_correction(
        self,
        *,
        user_key: str,
        session_date: str,
        task_id: str,
        corrected_by: str,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return await apply_operator_task_correction(
            self,
            user_key=user_key,
            session_date=session_date,
            task_id=task_id,
            corrected_by=corrected_by,
            reason=reason,
            now=now,
        )

    async def debug_reset_workday(
        self,
        client: discord.Client | None,
        user: UserProfile,
        session: SessionState,
        *,
        now: datetime | None = None,
        notify_user: bool = True,
    ) -> str:
        await self.refresh_configuration()
        if not self.config:
            return "Configuration is unavailable."
        now = now or self.resolve_user_local_now(user)
        if client and self._progress_probe_prompt(session):
            await self._close_progress_probe(
                client,
                user,
                session,
                now,
                reason="session_reset",
            )
        previous_session = self._clone_session_state(session)
        session.stage = "awaiting_clock_in"
        session.work_segments = []
        session.first_sign_of_life_at = None
        session.clocked_in_at = None
        session.intake_completed_at = None
        session.last_contact_at = None
        session.last_user_message_at = None
        session.last_outbound_at = None
        session.last_clock_in_prompt_at = None
        session.last_follow_up_at = None
        session.last_clickup_sync_at = None
        session.stuck_since = None
        session.stuck_alerted_at = None
        session.clocked_out_at = None
        session.awaiting_start_photo = False
        session.awaiting_clock_out_photo = False
        session.awaiting_clock_out_summary = False
        session.pending_clickup_sync = False
        session.latest_plan = None
        session.latest_status = None
        session.latest_blocker = None
        session.latest_feedback = None
        session.metadata = {
            "debug_reset_at": now.isoformat(),
            "debug_reset_reason": "admin_debug_resetworkday",
        }
        if client and notify_user:
            await self._send_dm(
                client,
                user,
                session,
                (
                    "Admin reset today's workday flow for testing. Reply once you have clocked in, "
                    "and I will walk you through task selection from the top."
                ),
                now,
            )
        await self._persist_session_state(
            user,
            session,
            now=now,
            previous_session=previous_session,
            trigger="debug_reset_workday",
            details={"notified_user": bool(client and notify_user)},
        )
        await self.write_dashboard()
        return (
            f"Reset {user.display_name}'s local workday state for {session.session_date}. "
            "Stored messages and files are preserved, but new workflow logic will ignore anything from before the reset."
        )

    async def resolve_admin_review(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        *,
        approve_close: bool,
        admin_message: str,
        task_hint: str | None = None,
        task_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        await self.refresh_configuration()
        if not self.config or not self.clickup:
            return "ClickUp is not configured."
        review, error = self._match_pending_admin_review(session, task_hint=task_hint, task_id=task_id)
        if not review:
            return error or f"{user.display_name} does not have a task waiting on admin review."
        previous_session = self._clone_session_state(session)
        now = now or self.resolve_user_local_now(user)
        selected_task_id = str(review.get("task_id") or self._active_task_id(session) or "")
        task_name = str(review.get("task_name") or session.metadata.get("active_clickup_task_name") or "the task")
        remaining_reviews = [
            existing_review
            for existing_review in self._pending_admin_reviews(session)
            if existing_review is not review
        ]
        self._set_pending_admin_reviews(session, remaining_reviews)
        current_active_task_id = self._active_task_id(session)
        current_active_task_name = str(session.metadata.get("active_clickup_task_name") or "")
        if session.stage == "awaiting_admin_review" and current_active_task_id == selected_task_id:
            current_active_task_id = None
            current_active_task_name = ""
        if approve_close:
            if selected_task_id:
                await self._safe_set_task_state(session, selected_task_id, "complete")
                if self.clickup:
                    await self.clickup.comment_on_task(
                        selected_task_id,
                        f"Admin review approved closure. Admin note: {admin_message}",
                    )
                self._remember_recently_closed_task(session, selected_task_id, task_name, now)
            session.metadata["last_admin_review_resolution"] = {
                "decision": "close",
                "at": now.isoformat(),
                "message": admin_message,
            }
            if current_active_task_id and current_active_task_id != selected_task_id:
                session.stage = "active"
                await self._send_dm(
                    client,
                    user,
                    session,
                    (
                        f"Admin approved `{task_name}` and I closed it in ClickUp.\n\n"
                        f"I left `{current_active_task_name or current_active_task_id}` active, so you can keep working there."
                    ),
                    now,
                )
            else:
                session.stage = "active"
                session.metadata.pop("active_clickup_task_id", None)
                session.metadata.pop("active_clickup_task_name", None)
                session.metadata.pop("clickup_selection_reason", None)
                session.metadata[_CLICKUP_PROMPT_KEY] = {
                    "type": "task_onboarding",
                    "source": "post_review_close",
                    "step": "select_task",
                    "reason": "Previous task closed after admin review.",
                    "draft": {},
                }
                session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
                selection_prompt = await self._task_selection_prompt(user, session)
                await self._send_dm(
                    client,
                    user,
                    session,
                    (
                        f"Admin approved `{task_name}` and I closed it in ClickUp.\n\n"
                        "Tell me what task you are picking up next so I can onboard it.\n\n"
                        f"{selection_prompt}"
                    ),
                    now,
                )
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="admin_review_resolution",
                details={
                    "decision": "close",
                    "task_id": selected_task_id,
                    "task_name": task_name,
                    "admin_message_excerpt": self._excerpt_text(admin_message),
                },
            )
            await self.write_dashboard()
            if current_active_task_id and current_active_task_id != selected_task_id:
                return (
                    f"Closed `{task_name}` for {user.display_name} and left "
                    f"`{current_active_task_name or current_active_task_id}` active."
                )
            return f"Closed `{task_name}` for {user.display_name} and started next-task onboarding."
        if selected_task_id:
            await self._safe_set_task_state(session, selected_task_id, "in_progress")
            if self.clickup:
                await self.clickup.comment_on_task(
                    selected_task_id,
                    f"Admin requested more work before closure: {admin_message}",
                )
        session.metadata["last_admin_review_resolution"] = {
            "decision": "rework",
            "at": now.isoformat(),
            "message": admin_message,
        }
        if current_active_task_id and current_active_task_id != selected_task_id:
            session.stage = "active"
            session.metadata[_CLICKUP_PROMPT_KEY] = {
                "type": "queued_review_rework_decision",
                "task_id": selected_task_id,
                "task_name": task_name,
                "admin_feedback": admin_message,
                "requested_at": now.isoformat(),
            }
            await self._send_dm(
                client,
                user,
                session,
                (
                    f"Admin sent rework comments back on `{task_name}`, but you are currently on "
                    f"`{current_active_task_name or current_active_task_id}`.\n\n"
                    "Do you want to switch back now, or stay on your current task?"
                ),
                now,
                view=self._queued_review_choice_view(user),
            )
        else:
            session.stage = "active"
            session.metadata[_CLICKUP_PROMPT_KEY] = {
                "type": "task_onboarding",
                "source": "review_rework",
                "step": "plan",
                "task_id": selected_task_id,
                "task_name": task_name,
                "reason": "Admin requested more work before closure.",
                "draft": {
                    "admin_feedback": admin_message,
                },
            }
            session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
            session.latest_plan = None
            session.latest_feedback = None
            await self._send_dm(
                client,
                user,
                session,
                (
                    f"Admin reviewed `{task_name}` and wants more work before it can be closed.\n\n"
                    + self._task_onboarding_question(session.metadata[_CLICKUP_PROMPT_KEY], "plan")
                ),
                now,
            )
        await self._persist_session_state(
            user,
            session,
            now=now,
            previous_session=previous_session,
            trigger="admin_review_resolution",
            details={
                "decision": "rework",
                "task_id": selected_task_id,
                "task_name": task_name,
                "admin_message_excerpt": self._excerpt_text(admin_message),
            },
        )
        await self.write_dashboard()
        if current_active_task_id and current_active_task_id != selected_task_id:
            return (
                f"Queued a switch decision for {user.display_name}: `{task_name}` needs rework, "
                f"and `{current_active_task_name or current_active_task_id}` stayed active."
            )
        return f"Sent review feedback back to {user.display_name} and reactivated `{task_name}`."

    async def handle_incoming_message(self, client: discord.Client, message: discord.Message) -> None:
        await self.refresh_configuration()
        if not self.config:
            return
        user = self.roster_by_discord_id.get(message.author.id)
        if not user:
            return
        async with self._user_session_lock(user.user_key):
            session, now = self.get_user_session_for_moment(user, message.created_at)
            inbound = await self._build_inbound_record(message, session, user)
            await self.process_inbound_event(client, user, session, inbound, now)

    async def handle_slack_direct_message(
        self,
        client: discord.Client,
        event: dict[str, Any],
    ) -> None:
        await self.refresh_configuration()
        if not self.config:
            return
        slack_user_id = str(event.get("user") or "").strip()
        admin = self.admin_profile_by_slack_user_id(slack_user_id)
        if admin:
            if not self.slack:
                logger.error("Cannot answer Slack admin DM because Slack is not configured.")
                return
            response = await self.admin_router.handle_plain_text(
                client,
                admin.discord_user_id,
                str(event.get("text") or ""),
            )
            await self.slack.post_message(slack_user_id, response)
            return
        user = self.roster_by_slack_id.get(slack_user_id)
        if not user:
            logger.warning("Ignoring Slack DM from unmapped user %s.", slack_user_id or "unknown")
            return
        event_ts = str(event.get("event_ts") or event.get("ts") or "").strip()
        try:
            moment = datetime.fromtimestamp(float(event_ts), tz=timezone.utc)
        except (TypeError, ValueError):
            moment = datetime.now(tz=timezone.utc)
        async with self._user_session_lock(user.user_key):
            session, now = self.get_user_session_for_moment(user, moment)
            inbound = await self._build_slack_inbound_record(event, session, user, moment)
            await self.process_inbound_event(client, user, session, inbound, now)

    async def process_inbound_event(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
    ) -> None:
        previous_session = self._clone_session_state(session)
        normalized_changed = self._normalize_session_state(session, user=user)
        inserted = self.state_store.append_message(user.user_key, session.session_date, inbound)
        if not inserted:
            if normalized_changed:
                await self._persist_session_state(
                    user,
                    session,
                    now=now,
                    previous_session=previous_session,
                    trigger="inbound_message",
                    details={
                        "message_id": inbound.message_id,
                        "normalized_before_processing": True,
                        "duplicate_message_ignored": True,
                    },
                )
                await self.write_dashboard()
            return
        self._touch_inbound_session(session, now)
        if not session.first_sign_of_life_at:
            session.first_sign_of_life_at = now.isoformat()
        self_lookup_request = self._self_lookup_request_kind(inbound.content)
        if self_lookup_request:
            if self._progress_probe_prompt(session):
                await self._close_progress_probe(
                    client,
                    user,
                    session,
                    now,
                    reason="interrupted_by_self_lookup",
                )
            else:
                self._clear_follow_up_probe_tracking(session)
            session.pending_clickup_sync = previous_session.pending_clickup_sync
            await self._start_self_lookup_prompt(client, user, session, now)
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="inbound_message",
                details={
                    "message_id": inbound.message_id,
                    "content_excerpt": self._excerpt_text(inbound.content),
                    "handled_by_self_lookup_query": True,
                    "self_lookup_request_kind": self_lookup_request,
                },
            )
            await self.write_dashboard()
            return
        if session.stage == "awaiting_clock_out_artifacts":
            await self._handle_clock_out_artifacts(client, user, session, inbound, now)
            session.pending_clickup_sync = True
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="inbound_message",
                details={
                    "message_id": inbound.message_id,
                    "content_excerpt": self._excerpt_text(inbound.content),
                    "handled_by_clock_out_artifacts": True,
                },
            )
            await self.write_dashboard()
            return
        if await self._maybe_start_day_suppression_prompt(client, user, session, inbound, now):
            session.pending_clickup_sync = previous_session.pending_clickup_sync
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="inbound_message",
                details={
                    "message_id": inbound.message_id,
                    "content_excerpt": self._excerpt_text(inbound.content),
                    "handled_by_day_suppression_prompt": True,
                },
            )
            await self.write_dashboard()
            return
        signals = detect_signals(inbound.content)
        active_prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        active_prompt = active_prompt if isinstance(active_prompt, dict) else None
        if active_prompt is None:
            signals = await self.interface_intelligence.enrich_intern_signals(
                inbound.content,
                session.stage,
                signals,
            )
        if self._active_short_rest_break(session):
            await self._handle_short_rest_break_message(
                client,
                user,
                session,
                inbound,
                signals,
                now,
            )
            session.pending_clickup_sync = True
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="inbound_message",
                details={
                    "message_id": inbound.message_id,
                    "content_excerpt": self._excerpt_text(inbound.content),
                    "signals": self._signal_details(signals),
                    "handled_by_short_rest_break": True,
                },
            )
            await self.write_dashboard()
            return
        if self._day_suppression_active_for_session(session) and signals.clocked_in:
            self._clear_day_suppression_state(session)
        if await self._maybe_recover_missing_clock_in(client, user, session, inbound, signals, now):
            session.pending_clickup_sync = True
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="inbound_message",
                details={
                    "message_id": inbound.message_id,
                    "content_excerpt": self._excerpt_text(inbound.content),
                    "signals": self._signal_details(signals),
                    "handled_by_clock_in_recovery": True,
                },
            )
            await self.write_dashboard()
            return
        if session.stage == "on_lunch_break":
            await self._route_message(client, user, session, inbound, signals, now)
            session.pending_clickup_sync = True
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="inbound_message",
                details={
                    "message_id": inbound.message_id,
                    "content_excerpt": self._excerpt_text(inbound.content),
                    "signals": self._signal_details(signals),
                    "handled_by_lunch_break": True,
                },
            )
            await self.write_dashboard()
            return
        if getattr(signals, "starting_lunch", False):
            if await self._maybe_start_lunch_break(client, user, session, now):
                session.pending_clickup_sync = True
                await self._persist_session_state(
                    user,
                    session,
                    now=now,
                    previous_session=previous_session,
                    trigger="inbound_message",
                    details={
                        "message_id": inbound.message_id,
                        "content_excerpt": self._excerpt_text(inbound.content),
                        "signals": self._signal_details(signals),
                        "handled_by_lunch_intent": True,
                    },
                )
                await self.write_dashboard()
                return
        if await self._maybe_handle_lunch_confirmation(client, user, session, inbound, signals, now):
            session.pending_clickup_sync = True
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="inbound_message",
                details={
                    "message_id": inbound.message_id,
                    "content_excerpt": self._excerpt_text(inbound.content),
                    "signals": self._signal_details(signals),
                    "handled_by_lunch_intent": True,
                },
            )
            await self.write_dashboard()
            return
        if getattr(signals, "starting_short_rest", False):
            if await self._maybe_start_short_rest_break(client, user, session, now):
                session.pending_clickup_sync = True
                await self._persist_session_state(
                    user,
                    session,
                    now=now,
                    previous_session=previous_session,
                    trigger="inbound_message",
                    details={
                        "message_id": inbound.message_id,
                        "content_excerpt": self._excerpt_text(inbound.content),
                        "signals": self._signal_details(signals),
                        "handled_by_short_rest_intent": True,
                    },
                )
                await self.write_dashboard()
                return
        if (
            session.stage != "awaiting_clock_out_artifacts"
            and not signals.clocking_out
            and await self._handle_clickup_prompt(client, user, session, inbound, signals, now)
        ):
            session.pending_clickup_sync = True
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="inbound_message",
                details={
                    "message_id": inbound.message_id,
                    "content_excerpt": self._excerpt_text(inbound.content),
                    "signals": self._signal_details(signals),
                    "handled_by_prompt": True,
                },
            )
            await self.write_dashboard()
            return
        self._update_blocker_tracking_from_inbound(session, inbound, signals, now)
        await self._route_message(client, user, session, inbound, signals, now)
        await self._apply_post_route_clickup_automation(client, user, session, inbound, signals, now)
        self._maybe_capture_follow_up_reply_candidate(
            session,
            inbound,
            signals,
            now,
            previous_status=previous_session.latest_status,
        )
        session.pending_clickup_sync = True
        await self._persist_session_state(
            user,
            session,
            now=now,
            previous_session=previous_session,
            trigger="inbound_message",
            details={
                "message_id": inbound.message_id,
                "content_excerpt": self._excerpt_text(inbound.content),
                "signals": self._signal_details(signals),
                "handled_by_prompt": False,
            },
        )
        await self.write_dashboard()

    async def _maybe_recover_missing_clock_in(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        signals,
        now: datetime,
    ) -> bool:
        if session.clocked_in_at or not getattr(signals, "clocked_in", False):
            return False
        session.clocked_in_at = now.isoformat()
        session.clocked_out_at = None
        self._start_new_work_segment(session, now)
        session.awaiting_clock_out_photo = False
        session.awaiting_clock_out_summary = False
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") == "task_onboarding":
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
        if session.intake_completed_at:
            if session.stage == "clocked_out":
                session.stage = "active"
            await self._send_dm(
                client,
                user,
                session,
                "Got it. I marked you clocked in.",
                now,
            )
            return True
        await self._begin_daily_clock_in_intake(client, user, session, now, source="clock_in_recovery")
        return True

    async def _maybe_resume_same_day_work(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if not session.clocked_out_at:
            return False
        self._clear_pending_lunch_confirmation(session)
        self._clear_clock_out_state(session)
        if not session.clocked_in_at:
            session.clocked_in_at = now.isoformat()
        self._start_new_work_segment(session, now)
        self._clear_auto_clock_out_metadata(session)
        if not session.intake_completed_at:
            await self._begin_daily_clock_in_intake(client, user, session, now, source="same_day_reclockin")
            return True
        active_task_id = self._active_task_id(session)
        active_task_name = str(session.metadata.get("active_clickup_task_name") or "")
        if active_task_id:
            session.stage = "active"
            session.last_follow_up_at = now.isoformat()
            await self._activate_clickup_task(user, session, now, active_task_id, active_task_name)
            await self._send_dm(
                client,
                user,
                session,
                f"Got it. I marked you clocked back in and resumed `{active_task_name or active_task_id}`.",
                now,
            )
            return True
        if self._has_pending_admin_review(session):
            await self._start_task_selection_resume(client, user, session, now, reason="Your previous task is waiting on admin review, so I need you to confirm what you are picking up next.")
            return True
        await self._start_task_selection_resume(client, user, session, now, reason="I marked you clocked back in, but I still need to confirm which task you are resuming.")
        return True

    async def _maybe_resume_after_auto_clock_out_activity(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        signals,
        now: datetime,
    ) -> bool:
        if session.stage != "clocked_out" or not session.metadata.get("auto_clock_out_at"):
            return False
        if getattr(signals, "clocked_in", False) or getattr(signals, "clocking_out", False):
            return False
        if not inbound.content.strip() and not inbound.attachments:
            return False
        return await self._maybe_resume_same_day_work(client, user, session, now)

    async def _begin_daily_clock_in_intake(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        *,
        source: str,
    ) -> None:
        session.stage = "awaiting_task_selection"
        session.awaiting_start_photo = False
        session.clocked_out_at = None
        session.awaiting_clock_out_photo = False
        session.awaiting_clock_out_summary = False
        session.intake_completed_at = None
        session.last_follow_up_at = None
        tasks = await self.clickup.list_assigned_tasks(user, limit=8) if self.clickup else []
        recommended_task = self.clickup.pick_highest_priority_task(tasks) if self.clickup else None
        if (
            not recommended_task
            and self.clickup
            and hasattr(self.clickup, "suggest_next_tasks")
        ):
            workspace_options = await self.clickup.suggest_next_tasks(
                user,
                session,
                self.list_session_messages(user.user_key, session),
                limit=3,
            )
            recommended_task = workspace_options[0] if workspace_options else None
        prompt: dict[str, Any] = {
            "type": "task_onboarding",
            "source": source,
            "step": "select_task",
            "reason": "Daily clock-in needs a task confirmed before planning starts.",
            "draft": {},
        }
        if recommended_task:
            prompt["recommended_task_id"] = str(recommended_task.get("id") or "")
            prompt["recommended_task_name"] = str(recommended_task.get("name") or "")
            if recommended_task.get("_don_pollo_workspace_option"):
                prompt["recommended_workspace_option"] = True
        session.metadata[_CLICKUP_PROMPT_KEY] = prompt
        session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
        await self._send_dm(
            client,
            user,
            session,
            await self._daily_clock_in_task_prompt(user, session, tasks, recommended_task),
            now,
        )

    async def _daily_clock_in_task_prompt(
        self,
        user: UserProfile,
        session: SessionState,
        tasks: list[dict[str, Any]] | None = None,
        recommended_task: dict[str, Any] | None = None,
    ) -> str:
        if not self.clickup:
            return (
                "Good morning. Before we start today's intake, tell me the exact ClickUp task name or task ID you are starting with."
            )
        visible_tasks = list(tasks) if tasks is not None else await self.clickup.list_assigned_tasks(user, limit=8)
        recommendation = recommended_task or self.clickup.pick_highest_priority_task(visible_tasks)
        lines = [
            "Good morning. I marked you clocked in.",
            "",
            "Before we start today's intake, I need to tie your plan to a specific ClickUp task.",
        ]
        if recommendation:
            recommendation_id = str(recommendation.get("id") or "unknown")
            recommendation_name = str(recommendation.get("name") or "Unnamed task")
            recommendation_priority = (recommendation.get("priority") or {}).get("priority") or "none"
            is_workspace_option = bool(recommendation.get("_don_pollo_workspace_option"))
            recommendation_basis = (
                "It is my best open option across the configured ClickUp workspace, ranked by project-context overlap, status, and priority."
                if is_workspace_option
                else "It is the highest-priority assigned task I can see right now, using earliest-created as the tie-breaker."
            )
            lines.extend(["", f"My recommendation is `{recommendation_name}` ({recommendation_id}).", recommendation_basis])
            if is_workspace_option:
                lines.append(f"Location: {self._task_option_location_label(recommendation)}")
            lines.extend(
                [
                    f"Priority: {recommendation_priority}",
                    "If that is what you are starting with, reply `yes` or `recommended`. Otherwise choose another option by task name or ID.",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "I could not find a strong existing ClickUp task to recommend yet.",
                    "Reply with an exact task name or ID, or reply `create task` to propose a new one for approval.",
                ]
            )
        lines.extend(["", await self._task_selection_prompt(user, session, tasks=visible_tasks, recommended_task=recommendation)])
        return "\n".join(lines)

    async def _start_task_selection_resume(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        *,
        reason: str,
    ) -> None:
        session.stage = "awaiting_task_selection"
        session.awaiting_start_photo = False
        session.awaiting_clock_out_photo = False
        session.awaiting_clock_out_summary = False
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_onboarding",
            "source": "same_day_reclockin",
            "step": "select_task",
            "reason": "Same-workday clock-in needs an active task confirmed before work resumes.",
            "draft": {},
        }
        session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
        await self._send_dm(
            client,
            user,
            session,
            f"{reason}\n\n{await self._task_selection_prompt(user, session)}",
            now,
        )

    async def scheduler_tick(self, client: discord.Client) -> None:
        await run_scheduler_tick(self, client)

    async def _run_scheduler_for_user(
        self,
        client: discord.Client,
        user: UserProfile,
        base_now: datetime,
    ) -> None:
        async with self._user_session_lock(user.user_key):
            session, now = self.get_user_session_for_moment(user, base_now)
            is_workday = now.weekday() in self.config.schedule.workdays
            previous_session = self._clone_session_state(session)
            normalized_changed = self._normalize_session_state(session, user=user)
            changed = False
            reasons: list[str] = []
            if normalized_changed:
                changed = True
                reasons.append("normalized_session_state")
            if self._active_short_rest_break(session):
                if await self._maybe_check_short_rest_break(client, user, session, now):
                    changed = True
                    reasons.append("short_rest_break_auto_clock_out")
                if changed:
                    await self._persist_session_state(
                        user,
                        session,
                        now=now,
                        previous_session=previous_session,
                        trigger="scheduler_tick",
                        details={
                            "workday": is_workday,
                            "reasons": reasons + ["short_rest_break_active"],
                        },
                    )
                return
            if self._day_suppression_active_for_session(session):
                if await self._maybe_send_auto_clock_out_warning(client, user, session, now):
                    changed = True
                    reasons.append("sent_auto_clock_out_warning")
                if await self._maybe_auto_clock_out_inactive(client, user, session, now):
                    changed = True
                    reasons.append("auto_clock_out_inactive")
                if changed:
                    await self._persist_session_state(
                        user,
                        session,
                        now=now,
                        previous_session=previous_session,
                        trigger="scheduler_tick",
                        details={
                            "workday": is_workday,
                            "reasons": reasons + ["day_suppression_active"],
                        },
                    )
                return
            if await self._maybe_send_auto_clock_out_warning(client, user, session, now):
                changed = True
                reasons.append("sent_auto_clock_out_warning")
            if is_workday and await self._maybe_send_clock_in(client, user, session, now):
                changed = True
                reasons.append("sent_clock_in_prompt")
            if session.stage == "awaiting_clock_out_artifacts":
                if changed:
                    await self._persist_session_state(
                        user,
                        session,
                        now=now,
                        previous_session=previous_session,
                        trigger="scheduler_tick",
                        details={
                            "workday": is_workday,
                            "reasons": reasons,
                        },
                    )
                return
            if await self._maybe_auto_clock_out_inactive(client, user, session, now):
                changed = True
                reasons.append("auto_clock_out_inactive")
            if is_workday and await self._maybe_prompt_task_onboarding(client, user, session, now):
                changed = True
                reasons.append("prompted_task_onboarding")
            if is_workday and await self._maybe_send_lunch_break_check_in(client, user, session, now):
                changed = True
                reasons.append("sent_lunch_check_in")
            if is_workday and await self._maybe_check_meal_compliance(client, user, session, now):
                changed = True
                reasons.append("meal_compliance_event")
            if is_workday and await self._maybe_check_overtime_compliance(client, user, session, now):
                changed = True
                reasons.append("overtime_compliance_event")
            if await self._maybe_assess_pending_follow_up_probe(client, user, session, now):
                changed = True
                reasons.append("handled_follow_up_probe")
            if await self._maybe_timeout_progress_probe(client, user, session, now):
                changed = True
                reasons.append("timed_out_progress_probe")
            if is_workday and await self._maybe_send_follow_up(client, user, session, now):
                changed = True
                reasons.append("sent_follow_up")
            if await self._maybe_alert_admin(client, user, session, now):
                changed = True
                reasons.append("alerted_admin")
            if await self._maybe_flush_clickup(user, session, now):
                changed = True
                reasons.append("flushed_clickup")
            if await self._maybe_post_slack_daily_update(user, session, now):
                changed = True
                reasons.append("posted_slack_daily_update")
            if changed:
                await self._persist_session_state(
                    user,
                    session,
                    now=now,
                    previous_session=previous_session,
                    trigger="scheduler_tick",
                    details={
                        "workday": is_workday,
                        "reasons": reasons,
                    },
                )

    async def write_dashboard(self) -> None:
        if not self.config:
            return
        base_now = datetime.now(tz=resolve_timezone(self.runtime_timezone_name()))
        lines = [
            "# Intern Dashboard",
            "",
        ]
        for user in self.roster_by_key.values():
            session, user_now = self.get_user_session_for_moment(user, base_now)
            lines.append(f"## {user.display_name}")
            lines.append(f"- Local timezone: `{self.resolve_user_timezone_name(user)}`")
            lines.append(f"- Workday folder: `{session.session_date}`")
            lines.append(f"- Stage: `{session.stage}`")
            if session.clocked_in_at:
                lines.append(f"- Clocked in: {format_admin_datetime(session.clocked_in_at, reference=base_now)}")
            if session.latest_status:
                lines.append(f"- Latest status: {session.latest_status}")
            if session.latest_blocker:
                lines.append(f"- Blocker: {session.latest_blocker}")
            tracked_total_seconds, tracked_tasks = self._tracked_time_totals(session, user_now)
            if tracked_total_seconds > 0:
                lines.append(f"- Time tracked today: {self._format_duration(tracked_total_seconds)}")
            if tracked_tasks:
                lines.append(
                    "- Task time: "
                    + "; ".join(
                        f"{task_name or task_id or 'unknown'} {self._format_duration(seconds)}"
                        for task_id, task_name, seconds in tracked_tasks[:4]
                    )
                )
            if session.stage == "on_lunch_break":
                lunch_started_at = str(session.metadata.get("lunch_started_at") or "").strip()
                lines.append(f"- On lunch break since: {format_admin_datetime(lunch_started_at, reference=base_now)}")
            auto_clock_out_at = session.metadata.get("auto_clock_out_at")
            if isinstance(auto_clock_out_at, str) and auto_clock_out_at:
                lines.append(f"- Auto clocked out after inactivity: {format_admin_datetime(auto_clock_out_at, reference=base_now)}")
            if session.clocked_out_at:
                lines.append(f"- Clocked out: {format_admin_datetime(session.clocked_out_at, reference=base_now)}")
            lines.append("")
        await self.store.write_dashboard(self.config.dashboard_file_name, "\n".join(lines).strip() + "\n")

    async def _route_message(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        signals,
        now: datetime,
    ) -> None:
        if signals.clocking_out and not self._should_treat_as_task_completion(session, inbound.content):
            await self._start_clock_out(client, user, session, inbound, now)
            return
        if getattr(signals, "clocked_in", False):
            if await self._maybe_resume_same_day_work(client, user, session, now):
                return
        if await self._maybe_resume_after_auto_clock_out_activity(client, user, session, inbound, signals, now):
            if session.stage != "active":
                return
        if getattr(signals, "starting_lunch", False):
            if await self._maybe_start_lunch_break(client, user, session, now):
                return
        if await self._maybe_handle_lunch_confirmation(client, user, session, inbound, signals, now):
            return
        if await self._maybe_handle_intern_task_switch_request(client, user, session, inbound, now):
            return
        if session.stage == "awaiting_admin_review" and self._is_task_creation_request(inbound.content):
            await self._start_intern_task_switch(
                client,
                user,
                session,
                now,
                source="intern_switch",
                reason="Okay, that earlier task is already waiting on admin review. Let's create what you are switching to next.",
                open_task_creation=True,
            )
            return
        if session.stage == "awaiting_admin_review":
            await self._send_dm(
                client,
                user,
                session,
                "Your task is currently waiting on admin review. I will message you as soon as they respond.",
                now,
            )
            return
        if session.stage == "on_lunch_break":
            await self._handle_lunch_break_message(client, user, session, inbound, signals, now)
            return
        if session.stage == "awaiting_clock_in":
            if signals.clocked_in:
                session.clocked_in_at = now.isoformat()
                self._start_new_work_segment(session, now)
                await self._begin_daily_clock_in_intake(client, user, session, now, source="daily_clock_in")
            else:
                await self._send_dm(
                    client,
                    user,
                    session,
                    "I still need your clock-in confirmation. Reply once you have clocked in and I will start today's check-in.",
                    now,
                )
            return
        if session.stage == "awaiting_task_selection":
            await self._send_dm(
                client,
                user,
                session,
                "I still need you to confirm which assigned ClickUp task you are starting with today.",
                now,
            )
            return
        if session.stage == "awaiting_plan":
            if not inbound.content.strip():
                await self._send_dm(client, user, session, "Send me your plan in text so I can compare it against ClickUp.", now)
                return
            session.latest_plan = inbound.content
            clickup_bundle = await self._get_clickup_context(user, session, [inbound])
            self._remember_clickup_context(session, clickup_bundle)
            feedback = await self.advisor.plan_feedback(user, inbound.content, clickup_bundle.context)
            session.latest_feedback = feedback
            session.stage = "awaiting_start_photo"
            session.awaiting_start_photo = True
            await self._send_dm(
                client,
                user,
                session,
                f"{feedback}\n\n{self.config.prompts.start_photo_question}",
                now,
            )
            return
        if session.stage == "awaiting_start_photo":
            if not inbound.attachments:
                await self._send_dm(client, user, session, "I still need the before-start picture.", now)
                return
            self._record_attachment_paths(session, "before_start_photo_paths", inbound)
            session.awaiting_start_photo = False
            session.stage = "awaiting_risk"
            await self._send_dm(client, user, session, self.config.prompts.risk_question, now)
            return
        if session.stage == "awaiting_risk":
            session.latest_blocker = inbound.content.strip() or None
            session.intake_completed_at = now.isoformat()
            session.stage = "active"
            await self._maybe_begin_clickup_work(user, session, now)
            await self._send_dm(client, user, session, "Got it. I will check back in a couple of hours.", now)
            return
        if session.stage == "awaiting_clock_out_artifacts":
            await self._handle_clock_out_artifacts(client, user, session, inbound, now)
            return
        if session.stage == "active":
            if inbound.attachments:
                self._record_attachment_paths(session, "progress_photo_paths", inbound)
            if inbound.content.strip():
                session.latest_status = inbound.content.strip()
                if self._signals_blocked_status(signals):
                    session.latest_blocker = inbound.content.strip()
            elif inbound.attachments:
                session.latest_status = "Sent a progress image update."

    async def _maybe_send_clock_in(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if session.clocked_in_at:
            return False
        if now.hour < self.config.schedule.clock_in_hour or now.hour > self.config.schedule.clock_in_cutoff_hour:
            return False
        timezone_name = self.resolve_user_timezone_name(user)
        should_send = False
        if not session.last_clock_in_prompt_at:
            should_send = True
            prompt = self.config.prompts.clock_in
        else:
            last_prompt = self._coerce_datetime_for_reference(
                session.last_clock_in_prompt_at,
                reference=now,
                timezone_name=timezone_name,
            )
            if not last_prompt or now - last_prompt >= timedelta(hours=1):
                should_send = True
                prompt = self.config.prompts.clock_in_reminder
            else:
                prompt = ""
        if not should_send:
            return False
        await self._send_dm(client, user, session, prompt, now)
        session.last_clock_in_prompt_at = now.isoformat()
        return True

    async def _maybe_send_follow_up(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if session.stage != "active" or session.clocked_out_at:
            return False
        if self._self_lookup_prompt(session):
            return False
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") in {"task_onboarding", "progress_probe"}:
            return False
        if self._pending_follow_up_aggregation(session):
            return False
        if not session.intake_completed_at:
            return False
        timezone_name = self.resolve_user_timezone_name(user)
        intake_completed = self._coerce_datetime_for_reference(
            session.intake_completed_at,
            reference=now,
            timezone_name=timezone_name,
        )
        if not intake_completed:
            return False
        follow_up_interval = self._adaptive_follow_up_interval_minutes(user, session)
        if now - intake_completed < timedelta(minutes=follow_up_interval):
            return False
        if session.last_follow_up_at:
            last_follow_up = self._coerce_datetime_for_reference(
                session.last_follow_up_at,
                reference=now,
                timezone_name=timezone_name,
            )
            if last_follow_up and now - last_follow_up < timedelta(minutes=follow_up_interval):
                return False
        questions = self.config.prompts.follow_up_questions
        if not questions:
            logger.warning("Skipping follow-up for user %s because prompts.follow_up_questions is empty.", user.user_key)
            return False
        index = int(session.metadata.get("follow_up_index", 0)) % len(questions)
        sent = await self._send_dm(client, user, session, questions[index], now)
        session.metadata["follow_up_index"] = index + 1
        session.last_follow_up_at = now.isoformat()
        session.metadata[_PENDING_FOLLOW_UP_KEY] = {
            "message_id": sent.message_id if isinstance(sent, MessageRecord) else f"follow-up:{int(now.timestamp())}",
            "question_text": questions[index],
            "sent_at": now.isoformat(),
            "awaiting_reply": True,
        }
        return True

    def _adaptive_follow_up_interval_minutes(
        self,
        user: UserProfile,
        session: SessionState,
    ) -> int:
        if user.check_in_interval_minutes:
            return user.check_in_interval_minutes
        base_interval = max(45, int(self.config.schedule.follow_up_interval_minutes))
        estimated_minutes = self._estimated_task_duration_minutes(
            str(session.metadata.get("task_onboarding_estimated_duration") or "")
        )
        if estimated_minutes is None:
            return base_interval
        if estimated_minutes <= 60:
            return 45
        if estimated_minutes <= 3 * 60:
            return 75
        if estimated_minutes <= 8 * 60:
            return 90
        return min(120, max(90, base_interval))

    def _estimated_task_duration_minutes(self, value: str) -> int | None:
        normalized = value.strip().lower()
        if not normalized:
            return None
        match = re.search(
            r"(\d+(?:\.\d+)?)\s*(minutes?|mins?|hours?|hrs?|days?)\b",
            normalized,
        )
        if not match:
            return None
        amount = float(match.group(1))
        unit = match.group(2)
        if unit.startswith(("minute", "min")):
            return max(1, round(amount))
        if unit.startswith(("hour", "hr")):
            return max(1, round(amount * 60))
        return max(1, round(amount * 8 * 60))

    def _active_short_rest_break(
        self,
        session: SessionState,
    ) -> dict[str, Any] | None:
        raw = session.metadata.get(_SHORT_REST_ACTIVE_KEY)
        if not isinstance(raw, dict):
            return None
        if not str(raw.get("started_at") or "").strip():
            return None
        if not str(raw.get("deadline_at") or "").strip():
            return None
        return raw

    async def _maybe_start_short_rest_break(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if self._active_short_rest_break(session):
            await self._send_dm(
                client,
                user,
                session,
                "Your short rest break is already active. Reply `back from break` when you return.",
                now,
            )
            return True
        if (
            not self.config.labor.enabled
            or not user.time_tracking_required
            or not session.clocked_in_at
            or session.clocked_out_at
            or session.stage != "active"
            or not self._active_task_id(session)
        ):
            await self._send_dm(
                client,
                user,
                session,
                "I can start a paid short rest only while you are clocked in on an active ClickUp task.",
                now,
            )
            return True
        if self._progress_probe_prompt(session):
            await self._close_progress_probe(
                client,
                user,
                session,
                now,
                reason="converted_to_short_rest",
            )
        else:
            self._clear_follow_up_probe_tracking(session)
        limit_minutes = self.config.labor.short_rest_break_minutes
        deadline = now + timedelta(minutes=limit_minutes)
        record = {
            "started_at": now.isoformat(),
            "deadline_at": deadline.isoformat(),
            "limit_minutes": limit_minutes,
            "task_id": self._active_task_id(session),
            "task_name": str(session.metadata.get("active_clickup_task_name") or ""),
            "source": "inbound_short_rest_signal",
        }
        session.metadata[_SHORT_REST_ACTIVE_KEY] = dict(record)
        history = session.metadata.get(_SHORT_REST_HISTORY_KEY)
        if not isinstance(history, list):
            history = []
            session.metadata[_SHORT_REST_HISTORY_KEY] = history
        history.append(dict(record))
        deadline_label = deadline.strftime("%I:%M %p").lstrip("0")
        await self._send_dm(
            client,
            user,
            session,
            (
                f"Paid short rest started. Your work and active-project time remain on the clock "
                f"for up to {limit_minutes} minutes. Reply `back from break` by {deadline_label}. "
                f"If you do not check back in by then, I will clock you out effective {deadline_label}."
            ),
            now,
        )
        return True

    async def _handle_short_rest_break_message(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        signals: Any,
        now: datetime,
    ) -> None:
        active = self._active_short_rest_break(session)
        if not active:
            return
        deadline = self._coerce_datetime_for_reference(
            str(active.get("deadline_at") or ""),
            reference=now,
            timezone_name=self.resolve_user_timezone_name(user),
        )
        if deadline and now > deadline:
            await self._maybe_check_short_rest_break(client, user, session, now)
            return
        if getattr(signals, "clocking_out", False):
            self._finish_short_rest_break(session, now, outcome="manual_clock_out")
            await self._start_clock_out(client, user, session, inbound, now)
            return
        if (
            getattr(signals, "ending_short_rest", False)
            or getattr(signals, "recovered", False)
        ):
            started_at = self._coerce_datetime_for_reference(
                str(active.get("started_at") or ""),
                reference=now,
                timezone_name=self.resolve_user_timezone_name(user),
            )
            self._finish_short_rest_break(session, now, outcome="returned")
            elapsed_seconds = max(
                0,
                int((now - started_at).total_seconds()) if started_at else 0,
            )
            await self._send_dm(
                client,
                user,
                session,
                (
                    f"Welcome back. I recorded a paid short rest of "
                    f"{self._format_duration(elapsed_seconds)}. Continue on your active ClickUp task."
                ),
                now,
            )
            return
        deadline_label = deadline.strftime("%I:%M %p").lstrip("0") if deadline else "the deadline"
        await self._send_dm(
            client,
            user,
            session,
            (
                "Your paid short rest is still active. Reply `back from break` when you return. "
                f"If you do not check back in by {deadline_label}, I will clock you out."
            ),
            now,
        )

    def _finish_short_rest_break(
        self,
        session: SessionState,
        ended_at: datetime,
        *,
        outcome: str,
        detected_at: datetime | None = None,
    ) -> None:
        active = self._active_short_rest_break(session)
        if not active:
            return
        started_at = str(active.get("started_at") or "")
        active["ended_at"] = ended_at.isoformat()
        active["outcome"] = outcome
        if detected_at is not None:
            active["detected_at"] = detected_at.isoformat()
        history = session.metadata.get(_SHORT_REST_HISTORY_KEY)
        if isinstance(history, list):
            for record in reversed(history):
                if not isinstance(record, dict):
                    continue
                if str(record.get("started_at") or "") != started_at:
                    continue
                record.update(active)
                break
        session.metadata.pop(_SHORT_REST_ACTIVE_KEY, None)

    async def _maybe_check_short_rest_break(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        active = self._active_short_rest_break(session)
        if not active:
            return False
        deadline = self._coerce_datetime_for_reference(
            str(active.get("deadline_at") or ""),
            reference=now,
            timezone_name=self.resolve_user_timezone_name(user),
        )
        if deadline is None or now <= deadline:
            return False
        if session.clocked_out_at or session.stage == "clocked_out":
            self._finish_short_rest_break(
                session,
                deadline,
                outcome="already_clocked_out",
                detected_at=now,
            )
            return True
        note = await self._finalize_clickup_day(
            user,
            session,
            deadline,
            allow_status_completion=False,
            include_next_task_suggestion=False,
            pause_reason="auto_clock_out_short_rest_limit",
        )
        session.clocked_out_at = deadline.isoformat()
        self._close_current_work_segment(session, deadline)
        session.stage = "clocked_out"
        session.pending_clickup_sync = True
        session.metadata["auto_clock_out_at"] = deadline.isoformat()
        session.metadata["auto_clock_out_reference_at"] = str(active.get("started_at") or "")
        session.metadata["auto_clock_out_reason"] = (
            f"No return check-in within the {self.config.labor.short_rest_break_minutes}-minute short-rest limit."
        )
        if note:
            session.metadata["auto_clock_out_note"] = note
        self._finish_short_rest_break(
            session,
            deadline,
            outcome="auto_clocked_out",
            detected_at=now,
        )
        self._refresh_session_time_summary(session, deadline)
        worked_seconds = int(session.time_summary.get("clocked_in_total_seconds") or 0)
        self._append_compliance_event(
            session,
            event_type="short_rest_auto_clocked_out",
            now=now,
            worked_seconds=worked_seconds,
        )
        deadline_label = deadline.strftime("%I:%M %p").lstrip("0")
        await self._send_dm(
            client,
            user,
            session,
            (
                f"Your paid short rest reached {self.config.labor.short_rest_break_minutes} minutes "
                f"without a return check-in, so I clocked you out effective {deadline_label}. "
                "Reply `clock me back in` before doing more work."
            ),
            now,
        )
        return True

    async def _maybe_check_meal_compliance(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if (
            not self.config.labor.enabled
            or not user.time_tracking_required
            or not user.meal_tracking_required
            or not session.clocked_in_at
            or session.clocked_out_at
            or session.metadata.get("lunch_started_at")
        ):
            return False
        gross_seconds = self._work_segment_total_seconds(session, now)
        warning_seconds = round(self.config.labor.meal_warning_after_hours * 60 * 60)
        auto_pause_seconds = round(self.config.labor.meal_auto_pause_after_hours * 60 * 60)
        changed = False
        if (
            gross_seconds >= warning_seconds
            and gross_seconds < auto_pause_seconds
            and not session.metadata.get("meal_guidance_delivered_at")
            and not session.metadata.get("meal_compliance_warning_at")
        ):
            remaining_seconds = max(0, auto_pause_seconds - gross_seconds)
            remaining_minutes = max(1, round(remaining_seconds / 60))
            await self._send_dm(
                client,
                user,
                session,
                (
                    f"Lunch warning: you have {remaining_minutes} minute"
                    f"{'s' if remaining_minutes != 1 else ''} to begin your meal break. "
                    "If you do not start lunch, I will pause your work time automatically "
                    "at the meal deadline."
                ),
                now,
            )
            session.metadata["meal_compliance_warning_at"] = now.isoformat()
            self._append_compliance_event(
                session,
                event_type="meal_warning_sent",
                now=now,
                worked_seconds=gross_seconds,
            )
            changed = True
        if gross_seconds >= auto_pause_seconds and not session.metadata.get("meal_auto_pause_at"):
            started = await self._maybe_start_lunch_break(
                client,
                user,
                session,
                now,
                automatic=True,
            )
            if not started or session.stage != "on_lunch_break":
                return changed
            session.metadata["meal_auto_pause_at"] = now.isoformat()
            self._append_compliance_event(
                session,
                event_type="meal_auto_paused",
                now=now,
                worked_seconds=gross_seconds,
            )
            changed = True
        return changed

    async def _maybe_check_overtime_compliance(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if (
            not self.config.labor.enabled
            or not user.time_tracking_required
            or not user.overtime_approval_required
            or not session.clocked_in_at
            or session.clocked_out_at
            or session.metadata.get("overtime_approved_at")
            or not self._overtime_coaching_applies(user)
        ):
            return False
        self._refresh_session_time_summary(session, now)
        worked_seconds = int(session.time_summary.get("clocked_in_total_seconds") or 0)
        limit_seconds = round(self.config.labor.overtime_limit_hours * 60 * 60)
        warning_seconds = max(
            0,
            limit_seconds - self.config.labor.overtime_warning_minutes * 60,
        )
        changed = False
        if worked_seconds >= warning_seconds and worked_seconds < limit_seconds and not session.metadata.get(
            "overtime_warning_at"
        ):
            await self._send_dm(
                client,
                user,
                session,
                (
                    f"Overtime warning: you have {self.config.labor.overtime_warning_minutes} minutes "
                    "left before the configured daily limit. Please wrap up and clock out, or get "
                    "admin approval before continuing."
                ),
                now,
            )
            session.metadata["overtime_warning_at"] = now.isoformat()
            self._append_compliance_event(
                session,
                event_type="overtime_warning_sent",
                now=now,
                worked_seconds=worked_seconds,
            )
            changed = True
        if worked_seconds >= limit_seconds and not session.metadata.get("overtime_admin_alert_at"):
            worked_label = self._format_duration(worked_seconds)
            automatically_clocked_out = self.config.labor.auto_clock_out_at_overtime_limit
            if automatically_clocked_out:
                note = await self._finalize_clickup_day(
                    user,
                    session,
                    now,
                    allow_status_completion=False,
                    include_next_task_suggestion=False,
                    pause_reason="auto_clock_out_overtime_limit",
                )
                session.clocked_out_at = now.isoformat()
                self._close_current_work_segment(session, now)
                session.stage = "clocked_out"
                session.pending_clickup_sync = True
                session.metadata["auto_clock_out_at"] = now.isoformat()
                session.metadata["auto_clock_out_reason"] = "Configured overtime limit reached."
                if note:
                    session.metadata["auto_clock_out_note"] = note
                message = (
                    f"You reached {worked_label} of recorded work today, so I automatically "
                    "clocked you out to prevent unapproved overtime. "
                    "Do not resume work until Erik or George approves more time."
                )
            else:
                message = (
                    f"You reached {worked_label} of recorded work today. Stop work and clock out "
                    "unless Erik or George has approved more time."
                )
            await self._send_dm(
                client,
                user,
                session,
                message,
                now,
            )
            if not automatically_clocked_out:
                await self._safe_send_compliance_admin_notice(
                    client,
                    (
                        f"Unresolved overtime risk: {user.display_name} reached "
                        f"{self._format_duration(worked_seconds)} of recorded work for "
                        f"{session.session_date} without a stored approval, and automatic "
                        "clock-out is disabled."
                    ),
                    user=user,
                    session=session,
                )
            session.metadata["overtime_admin_alert_at"] = now.isoformat()
            self._append_compliance_event(
                session,
                event_type=(
                    "overtime_auto_clocked_out"
                    if automatically_clocked_out
                    else "overtime_threshold_crossed"
                ),
                now=now,
                worked_seconds=worked_seconds,
            )
            changed = True
        return changed

    def _meal_period_deadline(
        self,
        session: SessionState,
        user: UserProfile,
        now: datetime,
    ) -> datetime | None:
        bounds = self._work_segment_bounds(session, now)
        if not bounds:
            return None
        started_at = min(start for start, _end in bounds)
        timezone_name = self.resolve_user_timezone_name(user)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=resolve_timezone(timezone_name))
        return started_at.astimezone(resolve_timezone(timezone_name)) + timedelta(hours=5)

    def _overtime_coaching_applies(self, user: UserProfile) -> bool:
        return user.overtime_approval_required

    def _post_lunch_clock_out_guidance(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> str:
        if (
            not self.config
            or user.worker_type == "intern"
            or not self._overtime_coaching_applies(user)
        ):
            return ""
        self._refresh_session_time_summary(session, now)
        paid_seconds = int(session.time_summary.get("clocked_in_total_seconds") or 0)
        limit_seconds = round(self.config.labor.overtime_limit_hours * 60 * 60)
        remaining_seconds = max(0, limit_seconds - paid_seconds)
        projected_clock_out = now + timedelta(seconds=remaining_seconds)
        projected_label = projected_clock_out.strftime("%I:%M %p").lstrip("0")
        if remaining_seconds <= 0:
            return (
                "You are already at the configured daily approval threshold. "
                "Please clock out now unless additional time has been approved."
            )
        return (
            f"To stay below {self.config.labor.overtime_limit_hours:g} recorded hours, "
            f"plan to clock out by about {projected_label} unless additional time is approved."
        )

    def _append_compliance_event(
        self,
        session: SessionState,
        *,
        event_type: str,
        now: datetime,
        worked_seconds: int,
    ) -> None:
        events = session.metadata.setdefault("compliance_events", [])
        if not isinstance(events, list):
            events = []
            session.metadata["compliance_events"] = events
        events.append(
            {
                "event_type": event_type,
                "recorded_at": now.isoformat(),
                "worked_seconds": worked_seconds,
            }
        )

    async def _safe_send_compliance_admin_notice(
        self,
        client: discord.Client,
        content: str,
        *,
        user: UserProfile,
        session: SessionState,
    ) -> None:
        try:
            await self._send_admin_notice(
                client,
                content,
                user=user,
                session=session,
            )
        except Exception:
            logger.exception("Could not send compliance admin notice for %s.", user.user_key)

    def _pending_follow_up(self, session: SessionState) -> dict[str, Any] | None:
        raw = session.metadata.get(_PENDING_FOLLOW_UP_KEY)
        return raw if isinstance(raw, dict) else None

    def _pending_follow_up_aggregation(self, session: SessionState) -> dict[str, Any] | None:
        raw = session.metadata.get(_FOLLOW_UP_RESPONSE_AGGREGATION_KEY)
        return raw if isinstance(raw, dict) else None

    def _progress_probe_prompt(self, session: SessionState) -> dict[str, Any] | None:
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") == "progress_probe":
            return prompt
        return None

    def _progress_probe_history(self, session: SessionState) -> list[dict[str, Any]]:
        raw = session.metadata.get(_PROGRESS_PROBE_HISTORY_KEY)
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        return []

    def _clear_follow_up_probe_tracking(self, session: SessionState) -> None:
        session.metadata.pop(_PENDING_FOLLOW_UP_KEY, None)
        session.metadata.pop(_FOLLOW_UP_RESPONSE_AGGREGATION_KEY, None)

    def _clear_pending_follow_up_probe_aggregation_only(self, session: SessionState) -> None:
        session.metadata.pop(_FOLLOW_UP_RESPONSE_AGGREGATION_KEY, None)

    def _should_capture_follow_up_reply_candidate(
        self,
        session: SessionState,
        inbound: MessageRecord,
        signals,
    ) -> bool:
        if session.stage != "active":
            return False
        if not (self._pending_follow_up(session) or self._pending_follow_up_aggregation(session)):
            return False
        if self._progress_probe_prompt(session):
            return False
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if isinstance(prompt, dict):
            return False
        if not inbound.content.strip() and not inbound.attachments:
            return False
        if getattr(signals, "clocked_in", False) or getattr(signals, "clocking_out", False):
            return False
        if getattr(signals, "starting_lunch", False) or getattr(signals, "ending_lunch", False):
            return False
        if getattr(signals, "starting_short_rest", False) or getattr(
            signals,
            "ending_short_rest",
            False,
        ):
            return False
        if getattr(signals, "recovered", False):
            return False
        if self._signals_blocked_status(signals) or self._signals_help_requested(signals):
            return False
        if self._should_treat_as_task_completion(session, inbound.content):
            return False
        return True

    def _combined_follow_up_reply_text(self, aggregate: dict[str, Any]) -> str:
        parts = [
            str(item).strip()
            for item in aggregate.get("reply_fragments", [])
            if str(item).strip()
        ]
        return "\n".join(parts).strip()

    def _capture_follow_up_response_aggregate(
        self,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
        *,
        previous_status: str | None,
    ) -> None:
        pending = self._pending_follow_up(session)
        if not pending and not self._pending_follow_up_aggregation(session):
            return
        aggregate = self._pending_follow_up_aggregation(session)
        if not aggregate:
            aggregate = {
                "follow_up_message_id": str((pending or {}).get("message_id") or "") or None,
                "question_text": str((pending or {}).get("question_text") or "") or None,
                "first_reply_message_id": inbound.message_id,
                "reply_message_ids": [],
                "reply_fragments": [],
                "attachment_count": 0,
                "first_reply_at": now.isoformat(),
                "last_reply_at": now.isoformat(),
                "grace_window_open": True,
                "previous_status": previous_status,
            }
            session.metadata[_FOLLOW_UP_RESPONSE_AGGREGATION_KEY] = aggregate
        reply_ids = aggregate.setdefault("reply_message_ids", [])
        if inbound.message_id not in reply_ids:
            reply_ids.append(inbound.message_id)
        text = inbound.content.strip()
        if text:
            fragments = aggregate.setdefault("reply_fragments", [])
            fragments.append(text)
        aggregate["attachment_count"] = int(aggregate.get("attachment_count") or 0) + len(inbound.attachments)
        aggregate["last_reply_at"] = now.isoformat()
        aggregate["grace_window_open"] = True
        combined_text = self._combined_follow_up_reply_text(aggregate)
        if combined_text:
            session.latest_status = combined_text

    def _maybe_capture_follow_up_reply_candidate(
        self,
        session: SessionState,
        inbound: MessageRecord,
        signals,
        now: datetime,
        *,
        previous_status: str | None,
    ) -> None:
        if not self._should_capture_follow_up_reply_candidate(session, inbound, signals):
            return
        self._capture_follow_up_response_aggregate(
            session,
            inbound,
            now,
            previous_status=previous_status,
        )

    async def _maybe_send_lunch_break_check_in(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if session.stage != "on_lunch_break" or session.clocked_out_at:
            return False
        last_prompt_at = self._metadata_datetime(
            session,
            "lunch_last_prompt_at",
            reference=now,
            timezone_name=self.resolve_user_timezone_name(user),
        )
        if last_prompt_at and now - last_prompt_at < timedelta(minutes=self.config.schedule.follow_up_interval_minutes):
            return False
        await self._send_dm(client, user, session, self.config.prompts.lunch_break_check_in, now)
        session.metadata["lunch_last_prompt_at"] = now.isoformat()
        session.metadata["lunch_resume_requested_at"] = now.isoformat()
        return True

    def _default_progress_probe_questions(self) -> list[str]:
        return [
            "What specifically changed since the last check-in?",
            "What exact part, file, component, or task did you work on?",
            "What is the next step, or what is blocking you right now?",
        ]

    def _progress_probe_prompt_text(self, questions: list[str], *, clarification: bool) -> str:
        cleaned = [question.strip() for question in questions if question.strip()]
        if clarification:
            if not cleaned:
                cleaned = [
                    "What one concrete thing changed?",
                    "What exact part did you work on?",
                    "What is the next step or blocker?",
                ]
            return (
                "I still cannot tell what actually changed from that update. Be concrete.\n\n"
                + "\n".join(f"{index}. {question}" for index, question in enumerate(cleaned, start=1))
            )
        if not cleaned:
            cleaned = self._default_progress_probe_questions()
        return (
            "I still cannot tell what progress was made from that update, so I need a more specific check-in before I count it as project progress.\n\n"
            + "\n".join(f"{index}. {question}" for index, question in enumerate(cleaned, start=1))
        )

    def _progress_probe_closure_message(self, reason: str) -> str:
        mapping = {
            "captured_meaningful_progress": "Probe closed: meaningful progress captured.",
            "converted_to_blocker": "Probe closed: moved into blocker handling.",
            "converted_to_finish_confirmation": "Probe closed: moved into task-finish confirmation.",
            "converted_to_lunch": "Probe closed: moved into lunch-break handling.",
            "converted_to_clock_out": "Probe closed: moved into clock-out handling.",
            "interrupted_by_self_lookup": "Probe closed: interrupted by a self-hours or self-status lookup.",
            "probe_exhausted": "Probe closed: still no concrete progress detail after follow-up.",
            "timed_out": "Probe closed: no reply to the progress probe for 30 minutes.",
            "session_reset": "Probe closed: session state was reset or superseded.",
        }
        return mapping.get(reason, "Probe closed.")

    def _progress_probe_round(self, prompt: dict[str, Any]) -> int:
        try:
            return int(prompt.get("probe_round") or 1)
        except (TypeError, ValueError):
            return 1

    def _record_progress_probe_exchange_entry(
        self,
        container: dict[str, Any],
        *,
        role: str,
        content: str,
        message_id: str | None,
    ) -> None:
        exchange = container.setdefault("probe_exchange", [])
        if not isinstance(exchange, list):
            exchange = []
            container["probe_exchange"] = exchange
        exchange.append(
            {
                "role": role,
                "content": content,
                "message_id": message_id,
            }
        )

    def _admin_profile_by_discord_user_id(self, discord_user_id: int) -> AdminProfile | None:
        for admin in self.admin_profiles():
            if admin.discord_user_id == discord_user_id:
                return admin
        return None

    def _progress_probe_subscribed_admins(self, prompt: dict[str, Any]) -> list[AdminProfile]:
        subscribed_ids = {
            int(item)
            for item in prompt.get("subscribed_admin_ids", [])
            if str(item).strip()
        }
        return [admin for admin in self.admin_profiles() if admin.discord_user_id in subscribed_ids]

    def _render_progress_probe_exchange(self, user: UserProfile, exchange: dict[str, Any], *, include_closure: bool) -> str:
        question_text = str(exchange.get("question_text") or "Unknown follow-up question")
        original_reply_text = str(exchange.get("original_reply_text") or "").strip() or "No text captured."
        lines = [
            f"{user.display_name} triggered a progress probe.",
            "",
            f"Scheduled follow-up: {question_text}",
            "",
            "Original aggregated reply:",
            original_reply_text,
        ]
        transcript = exchange.get("probe_exchange", [])
        if isinstance(transcript, list) and transcript:
            lines.extend(["", "Probe exchange:"])
            for item in transcript:
                if not isinstance(item, dict):
                    continue
                role = "Don Pollo" if str(item.get("role") or "") == "bot" else user.display_name
                content = str(item.get("content") or "").strip()
                if content:
                    lines.append(f"{role}: {content}")
        if include_closure:
            closure_reason = str(exchange.get("closure_reason") or "").strip()
            if closure_reason:
                lines.extend(["", self._progress_probe_closure_message(closure_reason)])
        return "\n".join(lines).strip()

    async def _notify_progress_probe_subscribers(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        prompt: dict[str, Any],
        content: str,
    ) -> None:
        admins = self._progress_probe_subscribed_admins(prompt)
        if not admins:
            return
        await self._send_admin_notice(
            client,
            content,
            target_admins=admins,
            user=user,
            session=session,
        )

    async def _send_progress_probe_exchange_to_admin(
        self,
        client: discord.Client,
        admin: AdminProfile,
        user: UserProfile,
        session: SessionState,
        exchange: dict[str, Any],
        *,
        include_closure: bool,
    ) -> None:
        await self._send_admin_notice(
            client,
            self._render_progress_probe_exchange(user, exchange, include_closure=include_closure),
            target_admins=[admin],
            user=user,
            session=session,
        )

    async def _start_progress_probe(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        aggregate: dict[str, Any],
        now: datetime,
        *,
        reason: str,
        probe_questions: list[str],
    ) -> None:
        probe_id = f"probe:{aggregate.get('first_reply_message_id') or int(now.timestamp())}"
        original_reply_text = self._combined_follow_up_reply_text(aggregate)
        prompt: dict[str, Any] = {
            "type": "progress_probe",
            "probe_id": probe_id,
            "source_follow_up_message_id": str(aggregate.get("follow_up_message_id") or "") or None,
            "question_text": str(aggregate.get("question_text") or "") or None,
            "original_reply_message_ids": list(aggregate.get("reply_message_ids") or []),
            "original_reply_text": original_reply_text,
            "probe_round": 1,
            "probe_exchange": [],
            "subscribed_admin_ids": [],
            "last_activity_at": now.isoformat(),
            "reason": reason,
            "closure_reason": None,
        }
        session.latest_status = original_reply_text or session.latest_status
        session.metadata[_CLICKUP_PROMPT_KEY] = prompt
        self._clear_follow_up_probe_tracking(session)
        prompt_text = self._progress_probe_prompt_text(probe_questions, clarification=False)
        sent = await self._send_dm(client, user, session, prompt_text, now)
        self._record_progress_probe_exchange_entry(
            prompt,
            role="bot",
            content=prompt_text,
            message_id=sent.message_id if isinstance(sent, MessageRecord) else None,
        )
        prompt["last_activity_at"] = now.isoformat()
        await self._send_admin_notice(
            client,
            (
                f"{user.display_name} sent a weak scheduled check-in reply.\n\n"
                f"Scheduled follow-up:\n{str(aggregate.get('question_text') or 'Unknown question')}\n\n"
                f"Aggregated reply after the 1-minute wait:\n{original_reply_text or 'No text captured.'}\n\n"
                f"Reason: {reason}\n\n"
                "Would you like to see their response to the progress probe?"
            ),
            user=user,
            session=session,
            view_factory=lambda admin: _AdminProgressProbeView(
                self,
                admin,
                user_key=user.user_key,
                session_date=session.session_date,
                probe_id=probe_id,
            ),
        )

    async def _maybe_assess_pending_follow_up_probe(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if session.stage == "awaiting_clock_out_artifacts":
            return False
        if self._self_lookup_prompt(session):
            return False
        aggregate = self._pending_follow_up_aggregation(session)
        if not aggregate or self._progress_probe_prompt(session):
            return False
        last_reply_at = self._coerce_datetime_for_reference(
            str(aggregate.get("last_reply_at") or ""),
            reference=now,
            timezone_name=self.resolve_user_timezone_name(user),
        )
        if not last_reply_at or now - last_reply_at < _FOLLOW_UP_PROBE_GRACE_WINDOW:
            return False
        combined_text = self._combined_follow_up_reply_text(aggregate)
        if not combined_text and int(aggregate.get("attachment_count") or 0) > 0:
            self._clear_follow_up_probe_tracking(session)
            return True
        if not combined_text:
            self._clear_follow_up_probe_tracking(session)
            return True
        recent_messages = self.list_session_messages(user.user_key, session)[-8:]
        assessment = await self.advisor.assess_check_in_reply(
            user,
            session,
            str(aggregate.get("question_text") or ""),
            combined_text,
            recent_messages,
            previous_status=str(aggregate.get("previous_status") or "") or None,
            attachment_count=int(aggregate.get("attachment_count") or 0),
        )
        session.latest_status = combined_text
        if assessment.meaningful_progress or not assessment.needs_probe:
            self._clear_follow_up_probe_tracking(session)
            return True
        await self._start_progress_probe(
            client,
            user,
            session,
            aggregate,
            now,
            reason=assessment.reason,
            probe_questions=assessment.probe_questions or self._default_progress_probe_questions(),
        )
        return True

    async def _maybe_timeout_progress_probe(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if session.stage == "awaiting_clock_out_artifacts":
            return False
        prompt = self._progress_probe_prompt(session)
        if not prompt:
            return False
        last_activity_at = self._coerce_datetime_for_reference(
            str(prompt.get("last_activity_at") or ""),
            reference=now,
            timezone_name=self.resolve_user_timezone_name(user),
        )
        if not last_activity_at or now - last_activity_at < _PROGRESS_PROBE_TIMEOUT:
            return False
        await self._close_progress_probe(
            client,
            user,
            session,
            now,
            reason="timed_out",
        )
        return True

    async def _maybe_auto_clock_out_inactive(
        self,
        client: discord.Client | None,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        reference_at = self._inactivity_auto_clock_out_reference_at(
            user,
            session,
            reference=now,
        )
        if not reference_at:
            return False
        if now - reference_at < timedelta(hours=self.config.schedule.auto_clock_out_after_hours):
            return False
        note = await self._finalize_clickup_day(
            user,
            session,
            now,
            allow_status_completion=False,
            include_next_task_suggestion=False,
            pause_reason="auto_clock_out_inactive",
        )
        session.clocked_out_at = now.isoformat()
        self._close_current_work_segment(session, now)
        session.stage = "clocked_out"
        session.pending_clickup_sync = True
        session.metadata["auto_clock_out_at"] = now.isoformat()
        session.metadata["auto_clock_out_reference_at"] = reference_at.isoformat()
        session.metadata["auto_clock_out_reason"] = (
            f"No inbound check-in for {self.config.schedule.auto_clock_out_after_hours} hours."
        )
        if note:
            session.metadata["auto_clock_out_note"] = note
        if client is not None:
            try:
                await self._send_dm(
                    client,
                    user,
                    session,
                    _AUTO_CLOCK_OUT_NOTIFICATION_MESSAGE,
                    now,
                )
            except Exception:
                logger.exception(
                    "Failed to send auto clock-out notification to user %s (%s)",
                    user.user_key,
                    user.display_name,
                )
        return True

    async def _maybe_send_auto_clock_out_warning(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if not self.config or self.config.schedule.auto_clock_out_after_hours <= 0:
            return False
        reference_at = self._inactivity_auto_clock_out_reference_at(
            user,
            session,
            reference=now,
        )
        if not reference_at:
            return False
        auto_clock_out_at = reference_at + timedelta(hours=self.config.schedule.auto_clock_out_after_hours)
        warning_minutes = self.config.schedule.auto_clock_out_warning_minutes
        warning_at = auto_clock_out_at - timedelta(minutes=warning_minutes)
        if now < warning_at or now >= auto_clock_out_at:
            return False
        remaining_seconds = max(0, int((auto_clock_out_at - now).total_seconds()))
        remaining_minutes = max(1, (remaining_seconds + 59) // 60)
        warning_state = self._auto_clock_out_warning_state(session)
        reference_key = reference_at.isoformat()
        if warning_state and str(warning_state.get("reference_at") or "") == reference_key:
            return False
        try:
            await self._send_dm(
                client,
                user,
                session,
                (
                    "FINAL WARNING: You have been inactive for too long. "
                    f"If you do not respond within {remaining_minutes} minutes, I will "
                    "automatically clock you out and stop counting time until you check back in."
                ),
                now,
            )
        except Exception:
            logger.exception(
                "Failed to send auto clock-out warning to user %s (%s)",
                user.user_key,
                user.display_name,
            )
            return False
        session.metadata[_AUTO_CLOCK_OUT_WARNING_KEY] = {
            "reference_at": reference_key,
            "warning_sent_at": now.isoformat(),
            "auto_clock_out_at": auto_clock_out_at.isoformat(),
        }
        return True

    async def _maybe_alert_admin(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if not session.stuck_since or session.stuck_alerted_at:
            return False
        stuck_since = self._coerce_datetime_for_reference(
            session.stuck_since,
            reference=now,
            timezone_name=self.resolve_user_timezone_name(user),
        )
        if not stuck_since:
            return False
        if now - stuck_since < timedelta(hours=self.config.schedule.stuck_alert_after_hours):
            return False
        sent_to = await self._send_admin_notice(
            client,
            f"{user.display_name} has appeared stuck since {format_admin_datetime(stuck_since, reference=now)}. "
            f"Latest blocker: {session.latest_blocker or 'No blocker text captured.'}",
            user=user,
            session=session,
        )
        if not sent_to:
            return False
        session.stuck_alerted_at = now.isoformat()
        return True

    async def _maybe_flush_clickup(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if session.stage == "awaiting_clock_out_artifacts":
            return False
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") in {"task_onboarding", "progress_probe"}:
            return False
        if self._pending_follow_up(session) or self._pending_follow_up_aggregation(session):
            return False
        if not session.pending_clickup_sync or not session.last_user_message_at:
            return False
        last_user_message_at = self._coerce_datetime_for_reference(
            session.last_user_message_at,
            reference=now,
            timezone_name=self.resolve_user_timezone_name(user),
        )
        if not last_user_message_at:
            return False
        if now - last_user_message_at < timedelta(
            minutes=self.config.schedule.inactivity_minutes
        ):
            return False
        return await self._flush_clickup_for_session(user, session, now, force=False)

    async def force_clickup_sync(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        if not self.clickup:
            return False, "ClickUp is not configured."
        now = now or self.resolve_user_local_now(user)
        changed = await self._flush_clickup_for_session(user, session, now, force=True)
        if not changed:
            return False, "No ClickUp task could be selected for this user."
        return True, f"Posted a summary update for {user.display_name}."

    async def _flush_clickup_for_session(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        *,
        force: bool,
    ) -> bool:
        messages = self.list_session_messages(user.user_key, session)
        if session.last_clickup_sync_at and not force:
            cutoff = self._coerce_datetime(
                session.last_clickup_sync_at,
                timezone_name=self.resolve_user_timezone_name(user),
        )
            if cutoff:
                messages = [message for message in messages if message.created_at > cutoff]
        if not messages and not force:
            return False
        tracking = await self._get_task_tracking_state(user, session)
        self._reconcile_active_task_with_tracking(session, tracking)
        authoritative_task_id = str(tracking.get("timer_task_id") or self._active_task_id(session) or "") or None
        authoritative_task_name = str(
            tracking.get("timer_task_name")
            or session.metadata.get("active_clickup_task_name")
            or ""
        ) or None
        authoritative_selection_reason = str(session.metadata.get("clickup_selection_reason") or "") or None
        clickup_bundle = await self._get_clickup_context(
            user,
            session,
            messages,
            preferred_task_id=authoritative_task_id,
            preferred_task_name=authoritative_task_name,
            preferred_selection_reason=authoritative_selection_reason,
        )
        self._remember_clickup_context(
            session,
            clickup_bundle,
            preserve_active_task=bool(authoritative_task_id),
        )
        summary = await self.advisor.summarize_updates(user, session, messages, clickup_bundle.context)
        workspace = await self.store.ensure_user_workspace(user, session.session_date)
        comment_text = build_clickup_update(user, session, summary, str(workspace.daily_dir))
        if self.clickup:
            target_task_id = authoritative_task_id or clickup_bundle.active_task_id
            if not target_task_id:
                return False
            task_id = await self.clickup.post_update(
                user,
                target_task_id,
                comment_text,
                summary,
                int(now.timestamp() * 1000),
                blocker_text=session.latest_blocker,
            )
            if task_id and self.config.clickup.attach_images_to_tasks:
                await self._attach_new_files_to_clickup(task_id, messages)
            if not task_id:
                return False
        session.last_clickup_sync_at = now.isoformat()
        session.pending_clickup_sync = False
        return True

    async def _maybe_post_slack_daily_update(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        slack_client = getattr(self, "slack", None)
        if not self.config or not slack_client or not self.config.slack.daily_updates_enabled:
            return False
        if now.weekday() not in self.config.schedule.workdays:
            return False
        if not (self.config.slack.post_start_hour <= now.hour < self.config.slack.post_end_hour):
            return False
        state = self._load_slack_update_state()
        last_global_post_at = self._coerce_datetime_for_reference(
            str(state.get("last_daily_post_at") or ""),
            reference=now,
            timezone_name=self.runtime_timezone_name(),
        )
        if last_global_post_at and now - last_global_post_at < timedelta(minutes=self.config.slack.min_post_interval_minutes):
            return False
        messages = self.list_session_messages(user.user_key, session)
        active_task_id = str(self._active_task_id(session) or "")
        if not active_task_id:
            await self._report_operational_issue(
                category="slack_update_missing_task",
                severity="warning",
                summary=f"Slack update held because {user.display_name} has no active ClickUp task.",
                details={
                    "user_key": user.user_key,
                    "display_name": user.display_name,
                    "session_date": session.session_date,
                },
                fingerprint_parts=(user.user_key, session.session_date),
                now=now,
            )
            self._record_unmapped_slack_update(state, user, session, now)
            self._write_slack_update_state(state)
            return False
        channel_id, route_label, route_uncertain = await self._resolve_slack_daily_channel(
            user,
            session,
            messages,
        )
        if not channel_id:
            self._record_unmapped_slack_update(state, user, session, now)
            self._write_slack_update_state(state)
            return False
        if route_uncertain:
            await self._report_operational_issue(
                category="slack_route_uncertain",
                severity="warning",
                summary=f"Slack project routing needs review for {user.display_name}.",
                details={
                    "user_key": user.user_key,
                    "display_name": user.display_name,
                    "session_date": session.session_date,
                    "active_task_id": self._active_task_id(session),
                    "active_task_name": str(
                        session.metadata.get("active_clickup_task_name") or ""
                    ),
                    "fallback_channel_id": channel_id,
                },
                fingerprint_parts=(
                    user.user_key,
                    session.session_date,
                    str(self._active_task_id(session) or ""),
                ),
                now=now,
            )
            if self.config.slack.quarantine_uncertain_routes:
                self._record_unmapped_slack_update(state, user, session, now)
                self._write_slack_update_state(state)
                return False
        previous_update = self._slack_daily_update_state(state, user.user_key, session.session_date)
        if str(previous_update.get("active_task_id") or "") != active_task_id:
            previous_update = {}
        previous_posted_at = self._coerce_datetime_for_reference(
            str(previous_update.get("posted_at") or ""),
            reference=now,
            timezone_name=self.resolve_user_timezone_name(user),
        )
        if previous_posted_at and now - previous_posted_at < _SLACK_USER_MIN_POST_INTERVAL:
            return False
        first_update = not bool(previous_update)
        content_since = previous_posted_at
        if first_update and session.intake_completed_at:
            content_since = self._coerce_datetime_for_reference(
                session.intake_completed_at,
                reference=now,
                timezone_name=self.resolve_user_timezone_name(user),
            )
        recent_messages = self._fresh_interesting_slack_messages(
            messages,
            since=content_since,
        )
        posted_text_fingerprints = {
            str(value)
            for value in previous_update.get("posted_text_fingerprints", [])
            if str(value)
        }
        recent_messages = [
            message
            for message in recent_messages
            if self._slack_text_fingerprint(message.content or "") not in posted_text_fingerprints
        ]
        previous_image_paths = {
            str(value)
            for value in previous_update.get("posted_image_paths", [])
            if str(value)
        }
        images = self._fresh_interesting_slack_images(
            messages,
            previous_image_paths=previous_image_paths,
            since=previous_posted_at,
        )[: self.config.slack.max_images_per_update]
        blocker_changed = bool(
            session.latest_blocker
            and session.latest_blocker != previous_update.get("latest_blocker")
        )
        if not self._slack_update_has_anything_to_say(
            session,
            recent_messages,
            images,
            include_session_context=first_update,
            blocker_changed=blocker_changed,
        ):
            return False
        message = self._build_slack_daily_update_message(
            user,
            session,
            recent_messages,
            images,
            route_label=route_label,
            route_uncertain=route_uncertain,
            include_session_context=first_update,
            blocker_changed=blocker_changed,
        )
        text_fingerprints = set(posted_text_fingerprints)
        text_fingerprints.update(
            self._slack_text_fingerprint(message_record.content or "")
            for message_record in recent_messages
            if (message_record.content or "").strip()
        )
        if first_update:
            text_fingerprints.update(
                self._slack_text_fingerprint(value)
                for value in (
                    session.latest_status or "",
                    session.latest_plan or "",
                    session.latest_blocker or "",
                )
                if value.strip()
            )
        content_hash = hashlib.sha256(
            json.dumps(
                {
                    "task_id": self._active_task_id(session),
                    "texts": sorted(
                        self._slack_text_fingerprint(message_record.content or "")
                        for message_record in recent_messages
                        if (message_record.content or "").strip()
                    ),
                    "images": [str(image.get("local_path") or "") for image in images],
                    "blocker": session.latest_blocker if blocker_changed else "",
                    "initial_status": session.latest_status if first_update else "",
                    "initial_plan": session.latest_plan if first_update else "",
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        content_hash_history = {
            str(value)
            for value in previous_update.get("content_hash_history", [])
            if str(value)
        }
        if previous_update.get("content_hash"):
            content_hash_history.add(str(previous_update["content_hash"]))
        if content_hash in content_hash_history:
            return False
        try:
            root_message_ts = str(previous_update.get("root_message_ts") or "")
            thread_ts = (
                root_message_ts
                if self.config.slack.thread_daily_updates and root_message_ts
                else None
            )
            posted = await slack_client.post_message(
                channel_id,
                message,
                thread_ts=thread_ts,
            )
        except Exception:
            logger.exception("Failed to post Slack daily update for user %s.", user.user_key)
            return False
        message_ts = str(posted.get("ts") or "")
        root_message_ts = str(previous_update.get("root_message_ts") or message_ts)
        image_posts: list[dict[str, Any]] = []
        for image in images:
            image_path = Path(str(image.get("local_path") or ""))
            if not image_path.exists():
                continue
            title = f"{user.display_name}: {image_path.name}"
            try:
                uploaded = await slack_client.upload_file(
                    channel_id,
                    image_path,
                    title=title,
                    initial_comment=self._slack_image_caption(user, session, image),
                    thread_ts=(
                        root_message_ts
                        if self.config.slack.thread_daily_updates and root_message_ts
                        else None
                    ),
                )
            except Exception:
                logger.exception("Failed to upload Slack image %s for user %s.", image_path, user.user_key)
                continue
            image_ts = self._extract_slack_file_message_ts(uploaded, channel_id)
            image_posts.append(
                {
                    "user_key": user.user_key,
                    "display_name": user.display_name,
                    "session_date": session.session_date,
                    "channel_id": channel_id,
                    "message_ts": image_ts,
                    "file_path": str(image_path),
                    "title": title,
                    "caption": self._slack_image_caption(user, session, image),
                    "posted_at": now.isoformat(),
                    "parent_message_ts": message_ts,
                }
            )
        state["last_daily_post_at"] = now.isoformat()
        daily_updates = state.setdefault("daily_updates", {})
        if not isinstance(daily_updates, dict):
            daily_updates = {}
            state["daily_updates"] = daily_updates
        user_updates = daily_updates.setdefault(user.user_key, {})
        if not isinstance(user_updates, dict):
            user_updates = {}
            daily_updates[user.user_key] = user_updates
        content_hash_history.add(content_hash)
        previous_image_paths.update(
            str(item.get("file_path") or "")
            for item in image_posts
            if str(item.get("file_path") or "")
        )
        user_updates[session.session_date] = {
            "posted_at": now.isoformat(),
            "channel_id": channel_id,
            "route_label": route_label,
            "route_uncertain": route_uncertain,
            "message_ts": message_ts,
            "root_message_ts": root_message_ts,
            "active_task_id": str(self._active_task_id(session) or ""),
            "active_task_name": str(
                session.metadata.get("active_clickup_task_name") or ""
            ),
            "content_hash": content_hash,
            "content_hash_history": sorted(content_hash_history),
            "posted_text_fingerprints": sorted(text_fingerprints),
            "posted_image_paths": sorted(previous_image_paths),
            "latest_blocker": session.latest_blocker,
            "image_posts": image_posts,
        }
        posted_images = state.setdefault("posted_images", [])
        if not isinstance(posted_images, list):
            posted_images = []
            state["posted_images"] = posted_images
        posted_images.extend(image_posts)
        self._write_slack_update_state(state)
        return True

    async def _maybe_collect_slack_update_feedback(
        self,
        now: datetime,
    ) -> int:
        return await collect_slack_update_feedback(self, now)

    async def _maybe_post_slack_weekly_photo_recap(self, base_now: datetime) -> bool:
        slack_client = getattr(self, "slack", None)
        if not self.config or not slack_client or not self.config.slack.weekly_recaps_enabled:
            return False
        now = base_now.astimezone(resolve_timezone(self.runtime_timezone_name()))
        if now.weekday() != self.config.slack.weekly_recap_day or now.hour != self.config.slack.weekly_recap_hour:
            return False
        channel_id = (
            self.config.slack.practice_channel_id
            or self.config.slack.default_channel_id
        )
        if not channel_id:
            return False
        state = self._load_slack_update_state()
        week_key = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
        recaps = state.setdefault("weekly_recaps", {})
        if not isinstance(recaps, dict):
            recaps = {}
            state["weekly_recaps"] = recaps
        if recaps.get(week_key):
            return False
        candidates = self._slack_recent_image_posts(state, now)
        ranked: list[dict[str, Any]] = []
        positive = {item.strip(":") for item in self.config.slack.positive_reactions}
        for item in candidates:
            item_channel_id = str(item.get("channel_id") or "")
            message_ts = str(item.get("message_ts") or "")
            if not item_channel_id or not message_ts:
                continue
            try:
                reactions = await slack_client.get_reactions(item_channel_id, message_ts)
            except Exception:
                logger.exception("Failed to read Slack reactions for weekly recap image %s.", message_ts)
                continue
            score = 0
            reaction_summary: list[str] = []
            for reaction in reactions:
                name = str(reaction.get("name") or "").strip(":")
                count = int(reaction.get("count") or 0)
                if count <= 0:
                    continue
                if name in positive:
                    score += count
                    reaction_summary.append(f":{name}: {count}")
            if score > 0:
                ranked.append({**item, "score": score, "reaction_summary": ", ".join(reaction_summary)})
        ranked.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("posted_at") or "")))
        if not ranked:
            return False
        lines = [
            "*Top progress photos this week*",
            "A few shots people reacted to most from Don Pollo updates:",
            "",
        ]
        for index, item in enumerate(ranked[:10], start=1):
            lines.append(
                f"{index}. *{item.get('display_name') or item.get('user_key')}* "
                f"({item.get('session_date')}): {item.get('caption') or item.get('title')} "
                f"- {item.get('score')} positive reaction(s)"
                + (f" [{item.get('reaction_summary')}]" if item.get("reaction_summary") else "")
            )
        try:
            await slack_client.post_message(channel_id, "\n".join(lines))
        except Exception:
            logger.exception("Failed to post Slack weekly photo recap.")
            return False
        recaps[week_key] = now.isoformat()
        self._write_slack_update_state(state)
        return True

    def _slack_daily_update_state(
        self,
        state: dict[str, Any],
        user_key: str,
        session_date: str,
    ) -> dict[str, Any]:
        daily = state.get("daily_updates")
        if not isinstance(daily, dict):
            return {}
        user_updates = daily.get(user_key)
        if not isinstance(user_updates, dict):
            return {}
        update = user_updates.get(session_date)
        return update if isinstance(update, dict) else {}

    async def _resolve_slack_daily_channel(
        self,
        user: UserProfile,
        session: SessionState,
        messages: list[MessageRecord] | None = None,
    ) -> tuple[str | None, str, bool]:
        if not self.config:
            return None, "", True
        task_id = str(self._active_task_id(session) or "")
        route_override = self._slack_route_override(
            user_key=user.user_key,
            session_date=session.session_date,
            task_id=task_id,
        )
        if route_override:
            return (
                str(route_override.get("channel_id") or ""),
                str(route_override.get("label") or "operator override"),
                False,
            )
        task_name = str(session.metadata.get("active_clickup_task_name") or "")
        task_ids = {task_id} if task_id else set()
        task_names = [task_name] if task_name else []
        list_ids: set[str] = set()
        list_names: list[str] = []
        folder_ids: set[str] = set()
        folder_names: list[str] = []
        content_values = self._slack_route_content_values(session, messages or [])
        if task_id and self.clickup:
            task_chain = await self._load_slack_task_ancestry(task_id)
            for task in task_chain:
                chain_task_id = str(task.get("id") or "")
                chain_task_name = str(task.get("name") or "")
                if chain_task_id:
                    task_ids.add(chain_task_id)
                if chain_task_name and chain_task_name not in task_names:
                    task_names.append(chain_task_name)
                task_list = task.get("list") if isinstance(task.get("list"), dict) else {}
                list_id = str(task_list.get("id") or "")
                list_name = str(task_list.get("name") or "")
                if list_id:
                    list_ids.add(list_id)
                if list_name and list_name not in list_names:
                    list_names.append(list_name)
                task_folder = task.get("folder") if isinstance(task.get("folder"), dict) else {}
                folder_id = str(task_folder.get("id") or "")
                folder_name = str(task_folder.get("name") or "")
                if folder_id:
                    folder_ids.add(folder_id)
                if folder_name and folder_name not in folder_names:
                    folder_names.append(folder_name)
        practice_channel_id = self.config.slack.practice_channel_id
        route_matches: list[tuple[Any, bool, bool]] = []
        for route in self.config.slack.project_routes:
            task_id_match = task_ids.intersection(route.clickup_task_ids)
            list_id_match = list_ids.intersection(route.clickup_list_ids)
            folder_id_match = folder_ids.intersection(route.clickup_folder_ids)
            task_name_match = self._slack_route_matches_any_name(
                route.task_name_patterns,
                task_names,
            )
            list_name_match = self._slack_route_matches_any_name(
                route.list_name_patterns,
                list_names,
            )
            folder_name_match = self._slack_route_matches_any_name(
                route.folder_name_patterns,
                folder_names,
            )
            content_match = self._slack_route_matches_any_name(
                route.content_patterns,
                content_values,
            )
            ancestry_match = bool(
                task_id_match
                or list_id_match
                or folder_id_match
                or task_name_match
                or list_name_match
                or folder_name_match
            )
            route_matches.append((route, bool(content_match), ancestry_match))
        for require_content_match in (True, False):
            for route, content_match, ancestry_match in route_matches:
                if content_match != require_content_match:
                    continue
                if not content_match and not ancestry_match:
                    continue
                channel_id = practice_channel_id or route.channel_id
                fallback_label = task_names[0] if task_names else task_id
                return channel_id, route.label or fallback_label, False
        if practice_channel_id:
            return practice_channel_id, "mapping needed", True
        if self.config.slack.unmapped_channel_id:
            return self.config.slack.unmapped_channel_id, "mapping needed", True
        return self.config.slack.default_channel_id, "default", not bool(self.config.slack.default_channel_id)

    def _slack_route_override(
        self,
        *,
        user_key: str,
        session_date: str,
        task_id: str,
    ) -> dict[str, Any] | None:
        load_state = getattr(
            getattr(self, "state_store", None),
            "get_operational_state",
            None,
        )
        if not callable(load_state):
            return None
        overrides = load_state("slack_route_overrides") or {}
        if not isinstance(overrides, dict):
            return None
        keys = []
        if task_id:
            keys.append(f"task:{task_id}")
        keys.append(f"session:{user_key}:{session_date}")
        for key in keys:
            override = overrides.get(key)
            if isinstance(override, dict) and override.get("channel_id"):
                return override
        return None

    def set_slack_route_override(
        self,
        *,
        user_key: str,
        session_date: str,
        task_id: str,
        channel_id: str,
        label: str,
        resolved_by: str,
    ) -> dict[str, Any]:
        if not self.config:
            raise RuntimeError("Configuration is not loaded.")
        route = next(
            (
                item
                for item in self.config.slack.project_routes
                if item.channel_id == channel_id
            ),
            None,
        )
        if route is None:
            raise ValueError("Choose a configured Slack project channel.")
        key = f"task:{task_id}" if task_id else f"session:{user_key}:{session_date}"
        load_state = getattr(self.state_store, "get_operational_state", None)
        save_state = getattr(self.state_store, "set_operational_state", None)
        if not callable(load_state) or not callable(save_state):
            raise RuntimeError("Operational state storage is unavailable.")
        overrides = load_state("slack_route_overrides") or {}
        if not isinstance(overrides, dict):
            overrides = {}
        override = {
            "user_key": user_key,
            "session_date": session_date,
            "task_id": task_id,
            "channel_id": channel_id,
            "label": label or route.label,
            "resolved_by": resolved_by,
            "resolved_at": datetime.now(
                tz=resolve_timezone(self.runtime_timezone_name())
            ).isoformat(),
        }
        overrides[key] = override
        save_state("slack_route_overrides", overrides)
        return override

    def _slack_route_content_values(
        self,
        session: SessionState,
        messages: list[MessageRecord],
    ) -> list[str]:
        values = [
            str(session.latest_status or ""),
            str(session.latest_plan or ""),
            str(session.latest_blocker or ""),
        ]
        for message in messages:
            if message.direction != "inbound":
                continue
            values.append(str(message.content or ""))
            for attachment in message.attachments:
                values.extend(
                    [
                        str(attachment.filename or ""),
                        str(attachment.description or ""),
                        " ".join(str(tag) for tag in attachment.tags),
                    ]
                )
        return [value for value in values if value.strip()]

    async def _load_slack_task_ancestry(self, task_id: str) -> list[dict[str, Any]]:
        if not self.clickup:
            return []
        task_chain: list[dict[str, Any]] = []
        seen_task_ids: set[str] = set()
        current_task_id = task_id
        while current_task_id and current_task_id not in seen_task_ids and len(task_chain) < 20:
            seen_task_ids.add(current_task_id)
            try:
                task = await self.clickup.get_task(current_task_id)
            except Exception:
                logger.exception(
                    "Could not load ClickUp task %s for Slack contract routing.",
                    current_task_id,
                )
                break
            if not isinstance(task, dict):
                break
            task_chain.append(task)
            current_task_id = str(task.get("parent") or "")
        return task_chain

    def _slack_route_matches_any_name(
        self,
        patterns: list[str],
        names: list[str],
    ) -> bool:
        return any(
            self._slack_route_pattern_matches(pattern, name)
            for pattern in patterns
            for name in names
        )

    def _record_unmapped_slack_update(
        self,
        state: dict[str, Any],
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> None:
        unmapped = state.setdefault("unmapped_updates", [])
        if not isinstance(unmapped, list):
            unmapped = []
            state["unmapped_updates"] = unmapped
        key = f"{user.user_key}:{session.session_date}:{self._active_task_id(session) or ''}"
        if any(str(item.get("key") or "") == key for item in unmapped if isinstance(item, dict)):
            return
        unmapped.append(
            {
                "key": key,
                "user_key": user.user_key,
                "display_name": user.display_name,
                "session_date": session.session_date,
                "active_task_id": self._active_task_id(session),
                "active_task_name": str(session.metadata.get("active_clickup_task_name") or ""),
                "recorded_at": now.isoformat(),
            }
        )

    def _fresh_interesting_slack_messages(
        self,
        messages: list[MessageRecord],
        *,
        since: datetime | None,
    ) -> list[MessageRecord]:
        fresh: list[MessageRecord] = []
        for message in messages:
            if message.direction != "inbound":
                continue
            if not self._slack_message_is_newer_than(message, since):
                continue
            text = " ".join((message.content or "").split())
            if len(text) < 12 and not message.attachments:
                continue
            if self._slack_message_is_low_signal(text) or self._slack_message_is_workflow_chatter(text):
                continue
            fresh.append(message)
        return fresh[-5:]

    def _fresh_interesting_slack_images(
        self,
        messages: list[MessageRecord],
        *,
        previous_image_paths: set[str],
        since: datetime | None,
    ) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        for message in messages:
            if message.direction != "inbound":
                continue
            if not self._slack_message_is_newer_than(message, since):
                continue
            for attachment in message.attachments:
                local_path = str(attachment.local_path or "")
                if not local_path or local_path in previous_image_paths:
                    continue
                content_type = str(attachment.content_type or "")
                suffix = Path(local_path).suffix.lower()
                if not (content_type.startswith("image/") or suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}):
                    continue
                if not attachment.description and not attachment.tags and not message.content.strip():
                    continue
                images.append(
                    {
                        "local_path": local_path,
                        "description": attachment.description or "",
                        "tags": list(attachment.tags or []),
                        "message_text": message.content.strip(),
                        "created_at": message.created_at.isoformat(),
                    }
                )
        return images

    def _slack_update_has_anything_to_say(
        self,
        session: SessionState,
        messages: list[MessageRecord],
        images: list[dict[str, Any]],
        *,
        include_session_context: bool,
        blocker_changed: bool,
    ) -> bool:
        return bool(
            images
            or (blocker_changed and session.latest_blocker)
            or any(self._slack_text_is_interesting(message.content or "") for message in messages)
            or (
                include_session_context
                and (
                    self._slack_text_is_interesting(session.latest_plan or "")
                    or (
                        not session.latest_plan
                        and self._slack_text_is_interesting(session.latest_status or "")
                    )
                )
            )
        )

    def _build_slack_daily_update_message(
        self,
        user: UserProfile,
        session: SessionState,
        messages: list[MessageRecord],
        images: list[dict[str, Any]],
        *,
        route_label: str,
        route_uncertain: bool,
        include_session_context: bool,
        blocker_changed: bool,
    ) -> str:
        intern = f"<@{user.slack_user_id}>" if user.slack_user_id else user.display_name
        task_name = str(session.metadata.get("active_clickup_task_name") or "current task")
        task_id = str(self._active_task_id(session) or "")
        task_label = (
            f"<{self._clickup_task_url(task_id)}|{task_name}>"
            if task_id
            else task_name
        )
        lines = [f"*{intern} update* - {task_label}"]
        if route_label and route_label not in {"default", "mapping needed"}:
            lines.append(f"_Project: {route_label}_")
        interesting_bits = self._slack_interesting_bits(
            session,
            messages,
            include_session_context=include_session_context,
        )
        if interesting_bits:
            lines.append(f"*What changed:* {interesting_bits}")
        if session.latest_blocker and (include_session_context or blocker_changed):
            lines.append(f"*Needs help:* {self._compact_slack_text(session.latest_blocker, 240)}")
        next_step = self._compact_slack_text(session.latest_plan or "", 220) if include_session_context else ""
        if next_step:
            lines.append(f"*Plan today:* {next_step}")
        if images:
            image_hint = self._compact_slack_text(
                str(images[0].get("description") or images[0].get("message_text") or "new progress photo"),
                180,
            )
            lines.append(f"*Photo note:* {image_hint}")
        return "\n".join(lines)

    def _slack_interesting_bits(
        self,
        session: SessionState,
        messages: list[MessageRecord],
        *,
        include_session_context: bool,
    ) -> str:
        pieces: list[str] = []
        if (
            include_session_context
            and not session.latest_plan
            and self._slack_text_is_interesting(session.latest_status or "")
        ):
            pieces.append(str(session.latest_status))
        for message in messages:
            if self._slack_text_is_interesting(message.content or ""):
                pieces.append(message.content)
        if not pieces:
            return ""
        return self._compact_slack_text(" ".join(dict.fromkeys(pieces)), 420)

    def _slack_image_caption(self, user: UserProfile, session: SessionState, image: dict[str, Any]) -> str:
        task_name = str(session.metadata.get("active_clickup_task_name") or "current task")
        description = str(image.get("description") or image.get("message_text") or "Progress image").strip()
        return f"{user.display_name} - {task_name}: {self._compact_slack_text(description, 220)}"

    def _extract_slack_file_message_ts(self, payload: dict[str, Any], channel_id: str) -> str:
        files = payload.get("files")
        files = files if isinstance(files, list) else []
        for file_payload in files:
            if not isinstance(file_payload, dict):
                continue
            shares = file_payload.get("shares")
            if not isinstance(shares, dict):
                continue
            for share_bucket in ("public", "private"):
                bucket = shares.get(share_bucket)
                if not isinstance(bucket, dict):
                    continue
                channel_shares = bucket.get(channel_id)
                if isinstance(channel_shares, list) and channel_shares:
                    first = channel_shares[0]
                    if isinstance(first, dict) and first.get("ts"):
                        return str(first.get("ts"))
        return ""

    def _slack_recent_image_posts(self, state: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
        raw = state.get("posted_images")
        if not isinstance(raw, list):
            return []
        cutoff = now - timedelta(days=7)
        recent: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            posted_at = self._coerce_datetime_for_reference(
                str(item.get("posted_at") or ""),
                reference=now,
                timezone_name=self.runtime_timezone_name(),
            )
            if posted_at and posted_at >= cutoff:
                recent.append(item)
        return recent

    def _slack_message_is_low_signal(self, text: str) -> bool:
        return self._slack_policy().is_low_signal(text)

    def _slack_message_is_workflow_chatter(self, text: str) -> bool:
        return self._slack_policy().is_workflow_chatter(text)

    def _slack_text_fingerprint(self, text: str) -> str:
        return self._slack_policy().fingerprint(text)

    def _slack_text_is_interesting(self, text: str) -> bool:
        return self._slack_policy().is_interesting(text)

    def _slack_policy(self) -> SlackUpdatePolicy:
        policy = getattr(self, "_slack_policy_instance", None)
        if not isinstance(policy, SlackUpdatePolicy):
            policy = SlackUpdatePolicy(self._self_lookup_request_kind)
            self._slack_policy_instance = policy
        return policy

    def _slack_route_pattern_matches(self, pattern: str, value: str) -> bool:
        try:
            return bool(re.search(pattern, value, re.IGNORECASE))
        except re.error:
            logger.warning("Ignoring invalid Slack route regex pattern %r.", pattern)
            return False

    def _slack_message_is_newer_than(self, message: MessageRecord, since: datetime | None) -> bool:
        if since is None:
            return True
        created_at = message.created_at
        if created_at.tzinfo is None and since.tzinfo is not None:
            created_at = created_at.replace(tzinfo=since.tzinfo)
        elif created_at.tzinfo is not None and since.tzinfo is None:
            created_at = created_at.replace(tzinfo=None)
        return created_at > since

    def _compact_slack_text(self, text: str, limit: int) -> str:
        normalized = " ".join(text.strip().split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3].rstrip() + "..."

    def _clickup_task_url(self, task_id: str) -> str:
        return f"https://app.clickup.com/t/{task_id}"

    def _slack_update_state_path(self) -> Path | None:
        storage_root = self._storage_root_path()
        if storage_root is None:
            return None
        return storage_root / _SLACK_UPDATE_STATE_RELATIVE_PATH

    def _load_slack_update_state(self) -> dict[str, Any]:
        load_state = getattr(self.state_store, "get_operational_state", None)
        if callable(load_state):
            stored = load_state("slack_updates")
            if stored is not None:
                return stored
        path = self._slack_update_state_path()
        if path is None or not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Could not read Slack update state from %s.", path)
            return {}
        state = payload if isinstance(payload, dict) else {}
        save_state = getattr(self.state_store, "set_operational_state", None)
        if state and callable(save_state):
            save_state("slack_updates", state)
        return state

    def _write_slack_update_state(self, state: dict[str, Any]) -> None:
        save_state = getattr(self.state_store, "set_operational_state", None)
        if callable(save_state):
            save_state("slack_updates", state)
        path = self._slack_update_state_path()
        if path is None:
            return
        atomic_write_json(path, state)

    async def _send_dm(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        content: str,
        now: datetime,
        *,
        view: discord.ui.View | None = None,
    ) -> MessageRecord:
        content, included_meal_guidance = self._append_queued_meal_guidance(
            user,
            session,
            content,
            now,
        )
        if self._should_use_slack_transport(user):
            if not self.slack or not user.slack_user_id:
                raise RuntimeError(
                    f"Slack delivery is selected for {user.user_key}, but Slack is not configured."
                )
            slack_content = content
            if view is not None:
                slack_content += "\n\nReply with the option text shown above."
            posted = await self.slack.post_message(user.slack_user_id, slack_content)
            outbound = MessageRecord(
                message_id=f"slack:{posted.get('channel') or user.slack_user_id}:{posted.get('ts') or int(now.timestamp())}",
                direction="outbound",
                author_id=self._stable_external_author_id("slack:don-pollo"),
                created_at=now,
                content=slack_content,
                attachments=[],
            )
            self.state_store.append_message(user.user_key, session.session_date, outbound)
            session.last_outbound_at = now.isoformat()
            if included_meal_guidance:
                self._mark_meal_guidance_delivered(session, now)
            return outbound
        if user.discord_user_id is None:
            raise RuntimeError(
                f"Discord delivery is selected for {user.user_key}, but discord_user_id is missing."
            )
        discord_user = await client.fetch_user(user.discord_user_id)
        dm = await discord_user.create_dm()
        sent = await dm.send(content, view=view)
        outbound = MessageRecord(
            message_id=str(sent.id),
            direction="outbound",
            author_id=client.user.id if client.user else 0,
            created_at=sent.created_at,
            content=content,
            attachments=[],
        )
        self.state_store.append_message(user.user_key, session.session_date, outbound)
        session.last_outbound_at = now.isoformat()
        if included_meal_guidance:
            self._mark_meal_guidance_delivered(session, now)
        return outbound

    def _append_queued_meal_guidance(
        self,
        user: UserProfile,
        session: SessionState,
        content: str,
        now: datetime,
    ) -> tuple[str, bool]:
        if (
            not session.metadata.get("meal_guidance_queued_at")
            or session.metadata.get("meal_guidance_delivered_at")
            or session.metadata.get("lunch_started_at")
            or session.clocked_out_at
            or session.stage in {"on_lunch_break", "clocked_out", "awaiting_clock_out_artifacts"}
            or re.search(r"\b(lunch|meal)\b", content, flags=re.IGNORECASE)
        ):
            return content, False
        deadline = self._meal_period_deadline(session, user, now)
        deadline_text = (
            deadline.strftime("%I:%M %p").lstrip("0")
            if deadline
            else "the end of your fifth hour"
        )
        guidance = (
            f"Friendly lunch reminder: based on your clock-in time, please begin and "
            f"record a 30-minute lunch by {deadline_text} if you will work more than five "
            "hours today. We want everyone to get a real, uninterrupted break. If you "
            "already took it, just tell me so the record stays accurate."
        )
        return f"{content}\n\n{guidance}", True

    def _mark_meal_guidance_delivered(
        self,
        session: SessionState,
        now: datetime,
    ) -> None:
        session.metadata["meal_guidance_delivered_at"] = now.isoformat()
        session.metadata.pop("meal_guidance_queued_at", None)

    def _should_use_slack_transport(self, user: UserProfile) -> bool:
        preferred = str(user.preferred_transport or "auto").strip().lower()
        if preferred == "slack":
            return True
        if preferred == "discord":
            return False
        return user.discord_user_id is None and bool(user.slack_user_id)

    def _stable_external_author_id(self, value: str) -> int:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return int(digest[:15], 16)

    async def _send_admin_notice(
        self,
        client: discord.Client,
        content: str,
        *,
        target_admins: list[AdminProfile] | None = None,
        files: list[Path] | None = None,
        user: UserProfile | None = None,
        session: SessionState | None = None,
        view_factory: Any | None = None,
    ) -> list[str]:
        admins = target_admins or self.admin_profiles()
        if not admins:
            return []
        sent_to: list[str] = []
        for admin in admins:
            delivered = False
            try:
                discord_user = await client.fetch_user(admin.discord_user_id)
                dm = await discord_user.create_dm()
                view = view_factory(admin) if callable(view_factory) else None
                if files:
                    if view is None:
                        await dm.send(
                            content=content[:1900],
                            files=[discord.File(str(path)) for path in files[:10]],
                        )
                    else:
                        await dm.send(
                            content=content[:1900],
                            files=[discord.File(str(path)) for path in files[:10]],
                            view=view,
                        )
                else:
                    if view is None:
                        await dm.send(content)
                    else:
                        await dm.send(content, view=view)
                delivered = True
            except Exception:
                logger.exception("Failed to send Discord admin notice to %s.", admin.name)
            slack_client = getattr(self, "slack", None)
            if slack_client and admin.slack_user_id:
                try:
                    await slack_client.post_message(admin.slack_user_id, content)
                    for path in (files or [])[:3]:
                        if not path.exists():
                            continue
                        await slack_client.upload_file(
                            admin.slack_user_id,
                            path,
                            title=path.name,
                        )
                    delivered = True
                except Exception:
                    logger.exception("Failed to send Slack admin notice to %s.", admin.name)
            if delivered:
                sent_to.append(admin.name)
        return sent_to

    async def _report_operational_issue(
        self,
        *,
        category: str,
        severity: str,
        summary: str,
        details: dict[str, Any] | None = None,
        fingerprint_parts: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> dict[str, Any]:
        reporter = getattr(self, "operations", None)
        if not isinstance(reporter, OperationalIssueReporter):
            return {
                "category": category,
                "severity": severity,
                "summary": summary,
                "details": details or {},
                "occurrence_count": 1,
            }
        return await reporter.report(
            category=category,
            severity=severity,
            summary=summary,
            details=details,
            fingerprint_parts=fingerprint_parts,
            now=now,
        )

    async def _maybe_send_operational_issue_digest(self, now: datetime) -> bool:
        reporter = getattr(self, "operations", None)
        if not isinstance(reporter, OperationalIssueReporter):
            return False
        return await reporter.maybe_send_digest(now)

    def _record_interaction_inbound_message(
        self,
        user: UserProfile,
        session: SessionState,
        interaction: discord.Interaction,
        *,
        content: str,
        now: datetime,
    ) -> None:
        self.state_store.append_message(
            user.user_key,
            session.session_date,
            MessageRecord(
                message_id=f"interaction:{interaction.id}",
                direction="inbound",
                author_id=interaction.user.id,
                created_at=interaction.created_at,
                content=content,
                attachments=[],
            ),
        )
        iso_value = now.isoformat()
        session.last_user_message_at = iso_value
        session.last_contact_at = iso_value
        if not session.first_sign_of_life_at:
            session.first_sign_of_life_at = iso_value
        session.metadata.pop(_AUTO_CLOCK_OUT_WARNING_KEY, None)

    def _record_interaction_outbound_message(
        self,
        user: UserProfile,
        session: SessionState,
        interaction: discord.Interaction,
        *,
        content: str,
        now: datetime,
    ) -> None:
        if not content:
            return
        author = getattr(getattr(interaction, "client", None), "user", None)
        self.state_store.append_message(
            user.user_key,
            session.session_date,
            MessageRecord(
                message_id=f"interaction-edit:{interaction.id}",
                direction="outbound",
                author_id=getattr(author, "id", 0) or 0,
                created_at=interaction.created_at,
                content=content,
                attachments=[],
            ),
        )
        session.last_outbound_at = now.isoformat()

    async def _start_self_lookup_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> None:
        content = (
            "Do you want to know about your hours or your status?\n\n"
            "Press `Hours` to choose a time range, `Status` to see your current work summary, or `No` to remove this prompt."
        )
        outbound = await self._send_dm(
            client,
            user,
            session,
            content,
            now,
            view=self._self_lookup_chooser_view(user, session.session_date),
        )
        session.metadata[_SELF_LOOKUP_PROMPT_KEY] = {
            "type": "self_lookup",
            "step": "chooser",
            "message_id": outbound.message_id,
            "requested_at": now.isoformat(),
        }

    async def _start_day_suppression_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
    ) -> None:
        content = (
            "It sounds like you may not be working today.\n\n"
            "Do you want me to stop sending reminders and check-ins until tomorrow?"
        )
        outbound = await self._send_dm(
            client,
            user,
            session,
            content,
            now,
            view=self._day_suppression_confirmation_view(user, session.session_date),
        )
        session.metadata[_DAY_SUPPRESSION_PROMPT_KEY] = {
            "type": "day_suppression_confirmation",
            "message_id": outbound.message_id,
            "requested_at": now.isoformat(),
            "source_message_id": inbound.message_id,
            "source_excerpt": self._excerpt_text(inbound.content),
        }

    async def _build_self_hours_reply(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> str:
        return await self._build_self_hours_reply_for_range(user, session, now, "this_week")

    async def _build_self_hours_reply_for_range(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        range_key: str,
    ) -> str:
        session_copy = self._clone_session_state(session)
        return await asyncio.to_thread(
            self._build_self_hours_reply_for_range_sync,
            user,
            session_copy,
            now,
            range_key,
        )

    def _build_self_hours_reply_for_range_sync(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        range_key: str,
    ) -> str:
        _report_path, rows = self._write_time_tracking_csv_sync(
            now=now,
            session_overrides=[(user, session)],
        )
        return self._build_self_hours_reply_for_range_from_rows(
            user,
            session,
            rows,
            now,
            range_key,
        )

    def _build_self_status_reply(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> str:
        timezone_name = self.resolve_user_timezone_name(user)
        active_task = str(session.metadata.get("active_clickup_task_name") or self._active_task_id(session) or "").strip()
        lines = [
            "Here is your current status:",
            f"- Stage: {session.stage}",
        ]
        if session.clocked_out_at:
            lines.append("- Clocked-in state: clocked out")
            lines.append(
                f"- Clocked out at: {format_user_datetime(session.clocked_out_at, timezone_name, reference=now)}"
            )
        elif session.clocked_in_at:
            lines.append("- Clocked-in state: clocked in")
            lines.append(
                f"- Clocked in at: {format_user_datetime(session.clocked_in_at, timezone_name, reference=now)}"
            )
        else:
            lines.append("- Clocked-in state: not clocked in yet")
        lines.append(f"- Active task: {active_task or 'none confirmed'}")
        if session.latest_status:
            lines.append(f"- Latest status: {session.latest_status}")
        if session.latest_blocker:
            lines.append(f"- Latest blocker: {session.latest_blocker}")
        reminder = self._clock_out_artifacts_reminder_text(session)
        if reminder:
            lines.append(f"- Outstanding clock-out requirements: {reminder}")
        return "\n".join(lines)

    def _self_lookup_prompt(self, session: SessionState) -> dict[str, Any] | None:
        prompt = session.metadata.get(_SELF_LOOKUP_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") == "self_lookup":
            return prompt
        return None

    def _clear_self_lookup_prompt(self, session: SessionState) -> None:
        session.metadata.pop(_SELF_LOOKUP_PROMPT_KEY, None)

    def _day_suppression_prompt(self, session: SessionState) -> dict[str, Any] | None:
        prompt = session.metadata.get(_DAY_SUPPRESSION_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") == "day_suppression_confirmation":
            return prompt
        return None

    def _clear_day_suppression_prompt(self, session: SessionState) -> None:
        session.metadata.pop(_DAY_SUPPRESSION_PROMPT_KEY, None)

    def _day_suppression_state(self, session: SessionState) -> dict[str, Any] | None:
        state = session.metadata.get(_DAY_SUPPRESSION_STATE_KEY)
        if isinstance(state, dict) and str(state.get("session_date") or "") == session.session_date:
            return state
        return None

    def _day_suppression_active_for_session(self, session: SessionState) -> bool:
        return self._day_suppression_state(session) is not None

    def _clear_day_suppression_state(self, session: SessionState) -> None:
        session.metadata.pop(_DAY_SUPPRESSION_STATE_KEY, None)

    async def _build_inbound_record(
        self,
        message: discord.Message,
        session: SessionState,
        user: UserProfile,
    ) -> MessageRecord:
        if not self.config:
            raise RuntimeError("Configuration not loaded.")
        workspace = await self.store.ensure_user_workspace(user, session.session_date)
        recent_messages = self.list_session_messages(user.user_key, session)
        attachments: list[AttachmentRecord] = []
        for index, attachment in enumerate(message.attachments, start=1):
            content = await attachment.read()
            insight = await self.image_intelligence.analyze_attachment(
                user=user,
                session=session,
                original_filename=attachment.filename,
                content=content,
                content_type=attachment.content_type,
                inbound_text=message.content or "",
                recent_messages=recent_messages,
            )
            filename = self.image_intelligence.build_storage_filename(
                timestamp_prefix=message.created_at.strftime("%H%M%S"),
                index=index,
                original_filename=attachment.filename,
                insight=insight,
            )
            local_path = await self.store.save_attachment(workspace.images_dir, filename, content)
            attachments.append(
                AttachmentRecord(
                    filename=filename,
                    url=attachment.url,
                    content_type=attachment.content_type,
                    size=attachment.size,
                    local_path=str(local_path),
                    original_filename=attachment.filename,
                    description=insight.description,
                    tags=insight.tags,
                    analysis_model=insight.analysis_model,
                )
            )
        return MessageRecord(
            message_id=str(message.id),
            direction="inbound",
            author_id=message.author.id,
            created_at=message.created_at,
            content=message.content or "",
            attachments=attachments,
        )

    async def _build_slack_inbound_record(
        self,
        event: dict[str, Any],
        session: SessionState,
        user: UserProfile,
        created_at: datetime,
    ) -> MessageRecord:
        workspace = await self.store.ensure_user_workspace(user, session.session_date)
        recent_messages = self.list_session_messages(user.user_key, session)
        inbound_text = str(event.get("text") or "")
        attachments: list[AttachmentRecord] = []
        raw_files = event.get("files")
        raw_files = raw_files if isinstance(raw_files, list) else []
        for index, raw_file in enumerate(raw_files, start=1):
            if not isinstance(raw_file, dict):
                continue
            original_filename = str(raw_file.get("name") or f"slack-file-{index}")
            download_url = str(
                raw_file.get("url_private_download")
                or raw_file.get("url_private")
                or ""
            )
            content_type = str(raw_file.get("mimetype") or "") or None
            content = b""
            if download_url and self.slack:
                try:
                    content = await self.slack.download_file(download_url)
                except Exception:
                    logger.exception("Could not download Slack file %s.", original_filename)
            if not content:
                attachments.append(
                    AttachmentRecord(
                        filename=original_filename,
                        url=download_url,
                        content_type=content_type,
                        size=int(raw_file.get("size") or 0) or None,
                        original_filename=original_filename,
                        description="Slack attachment could not be downloaded for local analysis.",
                    )
                )
                continue
            insight = await self.image_intelligence.analyze_attachment(
                user=user,
                session=session,
                original_filename=original_filename,
                content=content,
                content_type=content_type,
                inbound_text=inbound_text,
                recent_messages=recent_messages,
            )
            filename = self.image_intelligence.build_storage_filename(
                timestamp_prefix=created_at.astimezone(
                    resolve_timezone(self.resolve_user_timezone_name(user))
                ).strftime("%H%M%S"),
                index=index,
                original_filename=original_filename,
                insight=insight,
            )
            local_path = await self.store.save_attachment(
                workspace.images_dir,
                filename,
                content,
            )
            attachments.append(
                AttachmentRecord(
                    filename=filename,
                    url=download_url,
                    content_type=content_type,
                    size=len(content),
                    local_path=str(local_path),
                    original_filename=original_filename,
                    description=insight.description,
                    tags=insight.tags,
                    analysis_model=insight.analysis_model,
                )
            )
        message_id = str(event.get("client_msg_id") or event.get("event_ts") or event.get("ts") or "")
        return MessageRecord(
            message_id=f"slack:{message_id or int(created_at.timestamp())}",
            direction="inbound",
            author_id=self._stable_external_author_id(f"slack:{event.get('user') or 'unknown'}"),
            created_at=created_at,
            content=inbound_text,
            attachments=attachments,
        )

    async def _archive_session(self, user: UserProfile, session: SessionState) -> None:
        workspace = await self.store.ensure_user_workspace(user, session.session_date)
        await self.store.touch_file(workspace.daily_dir / _STATE_MACHINE_CHANGES_FILENAME)
        messages = self.state_store.list_messages(user.user_key, session.session_date)
        transcript = build_transcript_markdown(user, session, messages)
        await self.store.write_text_file(workspace.daily_dir / "transcript.md", transcript)
        manifest = build_image_manifest(messages)
        await self.store.write_text_file(
            workspace.daily_dir / "images_manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True),
        )
        await self.store.write_session_snapshot(workspace, session)

    async def _persist_session_state(
        self,
        user: UserProfile,
        session: SessionState,
        *,
        now: datetime,
        previous_session: SessionState,
        trigger: str,
        details: dict[str, Any] | None = None,
    ) -> bool:
        self._refresh_session_time_summary(session, now)
        self.state_store.save_session(session)
        transition_logged = await self._write_state_machine_transition(
            user,
            previous_session=previous_session,
            session=session,
            now=now,
            trigger=trigger,
            details=details,
        )
        await self._archive_session(user, session)
        try:
            await self._write_time_tracking_csv(now=now, session_overrides=[(user, session)])
        except Exception:
            logger.exception(
                "Failed to rebuild time tracking report after persisting session for %s.",
                user.user_key,
            )
        return transition_logged

    async def _write_state_machine_transition(
        self,
        user: UserProfile,
        *,
        previous_session: SessionState,
        session: SessionState,
        now: datetime,
        trigger: str,
        details: dict[str, Any] | None = None,
    ) -> bool:
        previous_snapshot = self._build_state_machine_snapshot(previous_session, now)
        current_snapshot = self._build_state_machine_snapshot(session, now)
        changed_fields = self._diff_state_machine_snapshots(previous_snapshot, current_snapshot)
        if not changed_fields:
            return False
        workspace = await self.store.ensure_user_workspace(user, session.session_date)
        entry = {
            "recorded_at": now.isoformat(),
            "trigger": trigger,
            "trigger_details": details or {},
            "from_stage": previous_snapshot.get("stage"),
            "to_stage": current_snapshot.get("stage"),
            "changed_fields": changed_fields,
            "previous": previous_snapshot,
            "current": current_snapshot,
        }
        await self.store.append_json_line(
            workspace.daily_dir / _STATE_MACHINE_CHANGES_FILENAME,
            entry,
        )
        return True

    async def _get_clickup_context(
        self,
        user: UserProfile,
        session: SessionState,
        messages: list[MessageRecord],
        *,
        preferred_task_id: str | None = None,
        preferred_task_name: str | None = None,
        preferred_selection_reason: str | None = None,
    ) -> ClickUpContextBundle:
        if not self.clickup:
            return ClickUpContextBundle()
        return await self.clickup.get_context_bundle(
            user,
            session,
            messages,
            preferred_task_id=preferred_task_id,
            preferred_task_name=preferred_task_name,
            preferred_selection_reason=preferred_selection_reason,
        )

    def _remember_clickup_context(
        self,
        session: SessionState,
        bundle: ClickUpContextBundle,
        *,
        preserve_active_task: bool = False,
    ) -> None:
        if not preserve_active_task or not self._active_task_id(session):
            if bundle.active_task_id:
                session.metadata["active_clickup_task_id"] = bundle.active_task_id
            else:
                session.metadata.pop("active_clickup_task_id", None)
            if bundle.active_task_name:
                session.metadata["active_clickup_task_name"] = bundle.active_task_name
            else:
                session.metadata.pop("active_clickup_task_name", None)
            if bundle.selection_reason:
                session.metadata["clickup_selection_reason"] = bundle.selection_reason
            else:
                session.metadata.pop("clickup_selection_reason", None)
        if bundle.candidate_task_ids:
            session.metadata["clickup_candidate_task_ids"] = bundle.candidate_task_ids
        else:
            session.metadata.pop("clickup_candidate_task_ids", None)

    async def _attach_new_files_to_clickup(self, task_id: str, messages: list[MessageRecord]) -> None:
        if not self.clickup:
            return
        uploaded_paths: set[str] = set()
        for message in messages:
            for attachment in message.attachments:
                if not attachment.local_path or attachment.local_path in uploaded_paths:
                    continue
                uploaded_paths.add(attachment.local_path)
                path = Path(attachment.local_path)
                if path.exists():
                    await self.clickup.upload_task_attachment(task_id, path)

    async def _apply_post_route_clickup_automation(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        signals,
        now: datetime,
    ) -> None:
        if session.stage == "on_lunch_break" or getattr(signals, "starting_lunch", False) or getattr(signals, "ending_lunch", False):
            return
        if signals.recovered:
            await self.resume_user_after_unblock(
                client,
                user,
                session,
                now=now,
                source="intern_recovered",
                actor_note=inbound.content.strip(),
                notify_user=True,
            )
        if self._should_treat_as_task_completion(session, inbound.content):
            await self._start_task_finish_confirmation(client, user, session, inbound, now)
            return
        if self._should_offer_blocker_resolution(session, inbound.content, signals):
            await self._maybe_prompt_stuck_assistance(client, user, session, inbound, now)

    async def _start_task_finish_confirmation(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
    ) -> None:
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") == "task_finish_confirmation":
            return
        self._clear_follow_up_probe_tracking(session)
        task_id = self._active_task_id(session)
        task_name = str(session.metadata.get("active_clickup_task_name") or "the active task")
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_finish_confirmation",
            "task_id": task_id,
            "task_name": task_name,
            "summary_candidate": inbound.content.strip(),
            "requested_at": now.isoformat(),
        }
        await self._send_dm(
            client,
            user,
            session,
            f"Are you saying `{task_name}` is finished and ready for admin review? Reply yes or no.",
            now,
        )

    async def _start_task_review_submission(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
        *,
        summary_text_override: str | None = None,
    ) -> None:
        summary_text = (summary_text_override or inbound.content.strip()).strip()
        photo_paths = self._message_attachment_paths(inbound)
        needs_summary = self._completion_requires_summary(summary_text)
        needs_photo = not bool(photo_paths)
        if not needs_summary and not needs_photo:
            await self._initiate_admin_review(
                client,
                user,
                session,
                inbound,
                now,
                review_summary=summary_text,
                completion_photo_paths=photo_paths,
            )
            return
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_review_submission",
            "summary": "" if needs_summary else summary_text,
            "needs_summary": needs_summary,
            "needs_photo": needs_photo,
            "photo_paths": photo_paths,
            "task_id": self._active_task_id(session),
            "task_name": session.metadata.get("active_clickup_task_name"),
            "requested_at": now.isoformat(),
        }
        if needs_summary and needs_photo:
            prompt_text = (
                "Got it. Before I send this task to admin for review, send me a short summary of what you finished "
                "and a picture of the finished work."
            )
        elif needs_summary:
            prompt_text = (
                "Got it. I already have the picture, but I still need a short summary of what you finished "
                "before I send this to admin for review."
            )
        else:
            prompt_text = (
                "I can send this to admin for review, but I still need a picture of the finished work. "
                "Send it and I will forward both the summary and the image."
            )
        await self._send_dm(client, user, session, prompt_text, now)

    async def _handle_clickup_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        signals,
        now: datetime,
    ) -> bool:
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if not isinstance(prompt, dict):
            return False
        prompt_type = str(prompt.get("type") or "")
        if signals.recovered and prompt_type in {"stuck_assistance", "blocker_resolution", "unblocker_task_draft"}:
            await self.resume_user_after_unblock(
                client,
                user,
                session,
                now=now,
                source="intern_recovered",
                actor_note=inbound.content.strip(),
                notify_user=True,
            )
            return True
        if prompt_type == "task_finish_confirmation":
            return await self._handle_task_finish_confirmation_prompt(client, user, session, inbound, now, prompt)
        if prompt_type == "task_review_submission":
            return await self._handle_task_review_submission_prompt(client, user, session, inbound, now, prompt)
        if prompt_type == "queued_review_rework_decision":
            return await self._handle_queued_review_rework_decision_prompt(client, user, session, inbound, now, prompt)
        if prompt_type == "task_onboarding":
            return await self._handle_task_onboarding_prompt(client, user, session, inbound, now, prompt)
        if prompt_type == "task_creation":
            return await self._handle_task_creation_prompt(client, user, session, inbound, now, prompt)
        if prompt_type == "task_creation_pending_approval":
            return await self._handle_pending_task_proposal_prompt(
                client,
                user,
                session,
                inbound,
                now,
            )
        if prompt_type == "progress_probe":
            return await self._handle_progress_probe_prompt(client, user, session, inbound, now, prompt)
        if prompt_type in {"stuck_assistance", "blocker_resolution"}:
            return await self._handle_blocker_resolution_prompt(client, user, session, inbound, now, prompt)
        if prompt_type == "unblocker_task_draft":
            return await self._handle_unblocker_task_prompt(client, user, session, inbound, now, prompt)
        if prompt_type != "blocker_task":
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            return False
        step = str(prompt.get("step") or "opt_in")
        draft = prompt.get("draft")
        if not isinstance(draft, dict):
            draft = {}
            prompt["draft"] = draft
        text = inbound.content.strip()
        lowered = text.lower()
        if lowered in {"cancel", "never mind", "nevermind", "stop"}:
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(client, user, session, "Okay, I cancelled the blocker-task draft.", now)
            return True
        if step != "opt_in" and await self._resolve_task_draft_intent(
            text,
            prompt_type="blocker_task",
            step=step,
        ) == "mistaken_task_creation":
            return await self._restore_blocker_task_draft_to_opt_in(client, user, session, now, prompt)
        if step == "opt_in":
            if self._is_negative_reply(text):
                session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
                await self._send_dm(client, user, session, "Got it. I will keep the blocker in your log without opening a ClickUp task.", now)
                return True
            if not self._is_affirmative_reply(text):
                await self._send_dm(
                    client,
                    user,
                    session,
                    "Reply `yes` if you want me to open a Mission Board blocker task, or `no` if you only want it logged here.",
                    now,
                )
                return True
            prompt["step"] = "title"
            await self._send_dm(client, user, session, "Give me a short title for the blocker task.", now)
            return True
        if step == "title":
            if not text:
                await self._send_dm(client, user, session, "I still need a short blocker-task title.", now)
                return True
            draft["title"] = text
            prompt["step"] = "description"
            await self._send_dm(client, user, session, "What exactly is blocked right now? Give me the context I should put into the task.", now)
            return True
        if step == "description":
            if not text:
                await self._send_dm(client, user, session, "I still need the blocker details for the task description.", now)
                return True
            draft["description"] = text
            prompt["step"] = "help_needed"
            await self._send_dm(client, user, session, "What exact help, information, approval, or resource do you need to unblock it?", now)
            return True
        if step == "help_needed":
            if not text:
                await self._send_dm(client, user, session, "Tell me what exact help is needed so I can finish the blocker task.", now)
                return True
            draft["help_needed"] = text
            prompt["step"] = "priority"
            await self._send_dm(client, user, session, "How urgent is this? Reply with `urgent`, `high`, `normal`, or `low`.", now)
            return True
        if step == "priority":
            priority = self._normalize_priority(text)
            if not priority:
                await self._send_dm(client, user, session, "Use one of these priority values: `urgent`, `high`, `normal`, or `low`.", now)
                return True
            draft["priority"] = priority
            prompt["step"] = "due_date"
            await self._send_dm(client, user, session, "Any due date? Reply `none` or use `YYYY-MM-DD`.", now)
            return True
        if step == "due_date":
            due_date_ms = self._parse_due_date_ms(text)
            if due_date_ms is False:
                await self._send_dm(client, user, session, "Use `none` or a date like `2026-05-30`.", now)
                return True
            if due_date_ms is not None:
                draft["due_date_ms"] = due_date_ms
            if self.config and self.config.clickup.new_task_approval_required:
                session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
                await self._submit_unblocker_task_for_admin_review(
                    client,
                    user,
                    session,
                    draft,
                    now,
                    user_message=(
                        "I sent this blocker-task draft to Erik and George for approval. "
                        "It will not be created in ClickUp until one of them approves it."
                    ),
                )
                return True
            created = await self._create_blocker_task_from_prompt(user, session, draft)
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            if not created:
                await self._send_dm(
                    client,
                    user,
                    session,
                    "I captured the blocker details, but I could not create the ClickUp task from here. I left everything in your local log.",
                    now,
                )
                return True
            await self._send_dm(
                client,
                user,
                session,
                f"Done. I opened the blocker task `{created.get('name')}` in Mission Board and linked it to today's context.",
                now,
            )
            return True
        session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
        return False

    async def _handle_task_onboarding_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
        prompt: dict[str, Any],
    ) -> bool:
        text = inbound.content.strip()
        lowered = text.lower()
        source = str(prompt.get("source") or "task_onboarding")
        if lowered in {"cancel", "never mind", "nevermind", "stop"}:
            if source in {"intern_switch", "review_rework_switch"} and await self._restore_cancelled_intern_task_switch(client, user, session, now):
                return True
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(client, user, session, "Okay, I cancelled the task-onboarding flow.", now)
            return True
        step = str(prompt.get("step") or "select_task")
        if step in {
            "confirm_task",
            "plan",
            "tangible_result",
            "necessity",
            "effectiveness",
            "estimated_duration",
            "reconsider_threshold",
            "fallback_plan",
            "risk",
            "photo",
        }:
            correction_hint = self._extract_task_onboarding_correction_hint(text)
            if correction_hint is not None:
                return await self._restart_task_onboarding_for_correction(
                    client,
                    user,
                    session,
                    prompt,
                    correction_hint,
                    now,
                )
        if step in {
            "plan",
            "tangible_result",
            "necessity",
            "effectiveness",
            "estimated_duration",
            "reconsider_threshold",
            "fallback_plan",
            "risk",
            "photo",
        } and self._parse_intern_task_switch_request(text) is not None:
            return await self._return_task_onboarding_to_selection(
                client,
                user,
                session,
                now,
                prompt,
                message_prefix="Got it. Let's switch tasks before this one officially starts.",
            )
        if step == "select_task":
            if not self.clickup:
                session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
                await self._send_dm(client, user, session, "I cannot resolve tasks right now because ClickUp is unavailable.", now)
                return True
            if self._is_task_creation_request(text):
                await self._start_task_creation_from_selection(client, user, session, now, prompt)
                return True
            recommended_task_id = str(prompt.get("recommended_task_id") or "")
            recommended_task_name = str(prompt.get("recommended_task_name") or "")
            task: dict[str, Any] | None
            if recommended_task_id and (lowered == "recommended" or self._is_affirmative_reply(text)):
                try:
                    task = await self.clickup.get_task(recommended_task_id)
                except Exception:
                    task = None
                if task and prompt.get("recommended_workspace_option"):
                    task = {**task, "_don_pollo_workspace_option": True}
            else:
                task = await self._resolve_intern_task_option(user, session, text)
            if not task:
                await self._send_dm(
                    client,
                    user,
                    session,
                    await self._task_selection_prompt(user, session),
                    now,
                )
                return True
            task_id = str(task.get("id") or "")
            if task_id and task_id in self._recently_closed_task_ids(session):
                await self._send_dm(
                    client,
                    user,
                    session,
                    (
                        f"`{task.get('name') or task_id}` was just closed, so do not pick it back up right now.\n\n"
                        f"{await self._task_selection_prompt(user, session)}"
                    ),
                    now,
                )
                return True
            self._set_task_onboarding_candidate(prompt, task)
            prompt["step"] = "confirm_task"
            session.stage = "awaiting_task_selection"
            await self._send_dm(
                client,
                user,
                session,
                self._task_onboarding_confirmation_prompt(prompt),
                now,
                view=self._task_confirmation_view(user),
            )
            return True
        if step == "confirm_task":
            if self._is_affirmative_reply(text):
                if prompt.get("candidate_workspace_option"):
                    candidate_task_id = str(prompt.get("candidate_task_id") or "")
                    try:
                        candidate_task = await self.clickup.get_task(candidate_task_id)
                        assigned = await self.clickup.ensure_task_assigned_to_user(
                            candidate_task,
                            user,
                        )
                    except Exception:
                        assigned = False
                    if not assigned:
                        self._reset_task_onboarding_prompt_to_select_task(prompt)
                        session.stage = "awaiting_task_selection"
                        await self._send_dm(
                            client,
                            user,
                            session,
                            (
                                "I found that workspace task, but I could not safely assign it to you. "
                                "I left it unchanged; choose another option or ask Erik or George to assign it.\n\n"
                                + await self._task_selection_prompt(user, session)
                            ),
                            now,
                        )
                        return True
                self._confirm_task_onboarding_candidate(prompt, session)
                await self._send_dm(
                    client,
                    user,
                    session,
                    (
                        f"Got it. You are onboarding onto `{prompt['task_name']}`.\n\n"
                        + self._task_onboarding_question(prompt, "plan")
                    ),
                    now,
                )
                return True
            if self._is_negative_reply(text):
                self._reset_task_onboarding_prompt_to_select_task(prompt)
                session.stage = "awaiting_task_selection"
                await self._send_dm(
                    client,
                    user,
                    session,
                    await self._task_selection_prompt(user, session),
                    now,
                )
                return True
            if self._is_task_creation_request(text):
                await self._start_task_creation_from_selection(client, user, session, now, prompt)
                return True
            if not self.clickup:
                session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
                await self._send_dm(client, user, session, "I cannot resolve tasks right now because ClickUp is unavailable.", now)
                return True
            task = await self._resolve_intern_task_option(user, session, text)
            if not task:
                await self._send_dm(
                    client,
                    user,
                    session,
                    self._task_onboarding_confirmation_prompt(prompt),
                    now,
                    view=self._task_confirmation_view(user),
                )
                return True
            task_id = str(task.get("id") or "")
            if task_id and task_id in self._recently_closed_task_ids(session):
                self._reset_task_onboarding_prompt_to_select_task(prompt)
                session.stage = "awaiting_task_selection"
                await self._send_dm(
                    client,
                    user,
                    session,
                    (
                        f"`{task.get('name') or task_id}` was just closed, so do not pick it back up right now.\n\n"
                        f"{await self._task_selection_prompt(user, session)}"
                    ),
                    now,
                )
                return True
            self._set_task_onboarding_candidate(prompt, task)
            await self._send_dm(
                client,
                user,
                session,
                self._task_onboarding_confirmation_prompt(prompt),
                now,
                view=self._task_confirmation_view(user),
            )
            return True
        if step == "plan":
            if not text:
                await self._send_dm(client, user, session, self._task_onboarding_missing_text(prompt, step), now)
                return True
            draft = self._task_onboarding_draft(prompt)
            draft["plan"] = text
            prompt["step"] = "tangible_result"
            session.stage = "awaiting_plan"
            await self._send_dm(
                client,
                user,
                session,
                self._task_onboarding_question(prompt, "tangible_result"),
                now,
            )
            return True
        if step == "tangible_result":
            if not text:
                await self._send_dm(client, user, session, self._task_onboarding_missing_text(prompt, step), now)
                return True
            draft = self._task_onboarding_draft(prompt)
            draft["tangible_result"] = text
            prompt["step"] = "necessity"
            session.stage = "awaiting_plan"
            await self._send_dm(
                client,
                user,
                session,
                self._task_onboarding_question(prompt, "necessity"),
                now,
            )
            return True
        if step == "necessity":
            if not text:
                await self._send_dm(client, user, session, self._task_onboarding_missing_text(prompt, step), now)
                return True
            draft = self._task_onboarding_draft(prompt)
            draft["necessity"] = text
            prompt["step"] = "effectiveness"
            session.stage = "awaiting_plan"
            await self._send_dm(
                client,
                user,
                session,
                self._task_onboarding_question(prompt, "effectiveness"),
                now,
            )
            return True
        if step == "effectiveness":
            if not text:
                await self._send_dm(client, user, session, self._task_onboarding_missing_text(prompt, step), now)
                return True
            draft = self._task_onboarding_draft(prompt)
            draft["effectiveness"] = text
            prompt["step"] = "estimated_duration"
            session.stage = "awaiting_plan"
            await self._send_dm(
                client,
                user,
                session,
                self._task_onboarding_question(prompt, "estimated_duration"),
                now,
            )
            return True
        if step == "estimated_duration":
            if not text:
                await self._send_dm(client, user, session, self._task_onboarding_missing_text(prompt, step), now)
                return True
            draft = self._task_onboarding_draft(prompt)
            draft["estimated_duration"] = text
            prompt["step"] = "reconsider_threshold"
            session.stage = "awaiting_plan"
            await self._send_dm(
                client,
                user,
                session,
                self._task_onboarding_question(prompt, "reconsider_threshold"),
                now,
            )
            return True
        if step == "reconsider_threshold":
            if not text:
                await self._send_dm(client, user, session, self._task_onboarding_missing_text(prompt, step), now)
                return True
            draft = self._task_onboarding_draft(prompt)
            draft["reconsider_threshold"] = text
            prompt["step"] = "fallback_plan"
            session.stage = "awaiting_plan"
            await self._send_dm(
                client,
                user,
                session,
                self._task_onboarding_question(prompt, "fallback_plan"),
                now,
            )
            return True
        if step == "fallback_plan":
            if not text:
                await self._send_dm(client, user, session, self._task_onboarding_missing_text(prompt, step), now)
                return True
            draft = self._task_onboarding_draft(prompt)
            draft["fallback_plan"] = text
            plan_summary = self._task_onboarding_summary(prompt)
            if plan_summary:
                session.latest_plan = plan_summary
            feedback = await self._task_onboarding_feedback(user, prompt, plan_summary or text)
            session.latest_feedback = feedback
            prompt["step"] = "photo"
            session.stage = "awaiting_start_photo"
            session.awaiting_start_photo = True
            photo_prompt = (
                self.config.prompts.start_photo_question
                if source in {"daily_clock_in", "clock_in_recovery"}
                else "Send me a fresh picture of the project before you continue on this task."
            )
            await self._send_dm(
                client,
                user,
                session,
                f"{feedback}\n\n{photo_prompt}",
                now,
            )
            return True
        if step == "risk":
            draft = self._task_onboarding_draft(prompt)
            draft["risk"] = text
            session.latest_blocker = text or None
            prompt["step"] = "photo"
            session.stage = "awaiting_start_photo"
            session.awaiting_start_photo = True
            photo_prompt = (
                self.config.prompts.start_photo_question
                if source in {"daily_clock_in", "clock_in_recovery"}
                else "Send me a fresh picture of the project before you continue on this task."
            )
            await self._send_dm(
                client,
                user,
                session,
                photo_prompt,
                now,
            )
            return True
        if step == "photo":
            if not inbound.attachments:
                if self._text_contains_url(text):
                    await self._send_dm(
                        client,
                        user,
                        session,
                        "I need the image uploaded as an attachment here. A link by itself does not count as the task-start photo.",
                        now,
                    )
                    return True
                await self._send_dm(client, user, session, self._task_onboarding_missing_text(prompt, step), now)
                return True
            existing_progress_photo_paths = [str(path) for path in self._coerce_path_list(session.metadata.get("progress_photo_paths"))]
            session.awaiting_start_photo = False
            try:
                activation = await self._finish_task_onboarding(user, session, prompt, now)
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code != 404:
                    raise
                if existing_progress_photo_paths:
                    session.metadata["progress_photo_paths"] = existing_progress_photo_paths
                else:
                    session.metadata.pop("progress_photo_paths", None)
                return await self._return_task_onboarding_to_selection(
                    client,
                    user,
                    session,
                    now,
                    prompt,
                    message_prefix=(
                        "That ClickUp task no longer exists, so I cannot start tracking it. "
                        "Let's pick a different task."
                    ),
                    clear_active_task=True,
                    clear_onboarding_metadata=True,
                )
            self._record_attachment_paths(session, "progress_photo_paths", inbound)
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(
                client,
                user,
                session,
                self._task_activation_notice(activation),
                now,
            )
            return True
        session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
        return False

    async def _handle_task_review_submission_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
        prompt: dict[str, Any],
    ) -> bool:
        text = inbound.content.strip()
        lowered = text.lower()
        if lowered in {"cancel", "never mind", "nevermind", "stop"} or self._looks_like_review_cancellation(text):
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(
                client,
                user,
                session,
                "Okay, I cancelled the admin-review submission and left the task active.",
                now,
            )
            return True
        needs_summary = bool(prompt.get("needs_summary"))
        needs_photo = bool(prompt.get("needs_photo"))
        photo_paths = [
            path for path in prompt.get("photo_paths", [])
            if isinstance(path, str) and path
        ]
        if inbound.attachments:
            self._record_attachment_paths(session, "progress_photo_paths", inbound)
            for path in self._message_attachment_paths(inbound):
                if path not in photo_paths:
                    photo_paths.append(path)
            prompt["photo_paths"] = photo_paths
            needs_photo = False
            prompt["needs_photo"] = False
        summary = str(prompt.get("summary") or session.latest_status or "").strip()
        if text:
            if needs_summary and self._completion_requires_summary(text):
                summary = ""
            else:
                summary = text
                prompt["summary"] = summary
                session.latest_status = summary
                needs_summary = False
                prompt["needs_summary"] = False
        if needs_summary or needs_photo:
            missing_parts: list[str] = []
            if needs_summary:
                missing_parts.append("a short summary of what you finished")
            if needs_photo:
                missing_parts.append("a picture of the finished work")
            await self._send_dm(
                client,
                user,
                session,
                f"I still need {' and '.join(missing_parts)} before I send this task to admin for review.",
                now,
            )
            return True
        session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
        await self._initiate_admin_review(
            client,
            user,
            session,
            inbound,
            now,
            review_summary=summary,
            completion_photo_paths=photo_paths,
        )
        return True

    async def _handle_task_finish_confirmation_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
        prompt: dict[str, Any],
    ) -> bool:
        text = inbound.content.strip()
        task_name = str(prompt.get("task_name") or session.metadata.get("active_clickup_task_name") or "the active task")
        if self._is_affirmative_reply(text):
            summary_candidate = str(prompt.get("summary_candidate") or "").strip()
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._start_task_review_submission(
                client,
                user,
                session,
                inbound,
                now,
                summary_text_override=summary_candidate,
            )
            return True
        if self._is_negative_reply(text) or self._looks_like_review_cancellation(text):
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(
                client,
                user,
                session,
                "Got it. I will keep this as normal progress and leave the task active.",
                now,
            )
            return True
        await self._send_dm(
            client,
            user,
            session,
            f"Please reply yes or no. Are you saying `{task_name}` is finished and ready for admin review?",
            now,
        )
        return True

    async def _handle_progress_probe_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
        prompt: dict[str, Any],
    ) -> bool:
        text = inbound.content.strip()
        if not text:
            await self._send_dm(
                client,
                user,
                session,
                "Reply in text with what specifically changed, what exact part you worked on, and what comes next.",
                now,
            )
            return True
        signals = detect_signals(text)
        self._record_progress_probe_exchange_entry(
            prompt,
            role="intern",
            content=text,
            message_id=inbound.message_id,
        )
        prompt["last_activity_at"] = now.isoformat()
        await self._notify_progress_probe_subscribers(
            client,
            user,
            session,
            prompt,
            f"{user.display_name} replied to the progress probe:\n\n{text}",
        )
        if self._signals_blocked_status(signals) or self._signals_help_requested(signals):
            self._update_blocker_tracking_from_inbound(session, inbound, signals, now)
            await self._close_progress_probe(
                client,
                user,
                session,
                now,
                reason="converted_to_blocker",
            )
            await self._maybe_prompt_stuck_assistance(client, user, session, inbound, now)
            return True
        if self._should_treat_as_task_completion(session, inbound.content):
            await self._close_progress_probe(
                client,
                user,
                session,
                now,
                reason="converted_to_finish_confirmation",
            )
            await self._start_task_finish_confirmation(client, user, session, inbound, now)
            return True
        recent_messages = self.list_session_messages(user.user_key, session)[-8:]
        assessment = await self.advisor.assess_check_in_reply(
            user,
            session,
            str(prompt.get("question_text") or "Progress probe"),
            text,
            recent_messages,
            previous_status=str(prompt.get("original_reply_text") or "") or None,
            attachment_count=len(inbound.attachments),
        )
        if assessment.meaningful_progress or not assessment.needs_probe:
            session.latest_status = text
            await self._close_progress_probe(
                client,
                user,
                session,
                now,
                reason="captured_meaningful_progress",
            )
            await self._send_dm(
                client,
                user,
                session,
                "Got it. That gives me a concrete progress update, so I will keep it as normal progress.",
                now,
            )
            return True
        if self._progress_probe_round(prompt) < 2:
            clarification_text = self._progress_probe_prompt_text(
                assessment.probe_questions or self._default_progress_probe_questions(),
                clarification=True,
            )
            prompt["probe_round"] = self._progress_probe_round(prompt) + 1
            sent = await self._send_dm(client, user, session, clarification_text, now)
            self._record_progress_probe_exchange_entry(
                prompt,
                role="bot",
                content=clarification_text,
                message_id=sent.message_id if isinstance(sent, MessageRecord) else None,
            )
            prompt["last_activity_at"] = now.isoformat()
            await self._notify_progress_probe_subscribers(
                client,
                user,
                session,
                prompt,
                f"Don Pollo asked a follow-up progress probe:\n\n{clarification_text}",
            )
            return True
        await self._close_progress_probe(
            client,
            user,
            session,
            now,
            reason="probe_exhausted",
        )
        await self._send_dm(
            client,
            user,
            session,
            (
                "I still could not tell what changed from that response, so I am ending the probe for now. "
                "On the next check-in, tell me one concrete change, the exact part you worked on, and the next step or blocker."
            ),
            now,
        )
        return True

    async def _handle_stuck_assistance_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
        prompt: dict[str, Any],
    ) -> bool:
        return await self._handle_blocker_resolution_prompt(client, user, session, inbound, now, prompt)

    async def _handle_blocker_resolution_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
        prompt: dict[str, Any],
        *,
        action: str | None = None,
    ) -> bool:
        step = str(prompt.get("step") or "offer_help")
        if step == "offer":
            step = "offer_help"
        if not prompt.get("blocker_text"):
            draft = prompt.get("draft")
            if isinstance(draft, dict):
                prompt["blocker_text"] = str(draft.get("blocker_text") or "")
                if draft.get("origin_message_id") and not prompt.get("origin_message_id"):
                    prompt["origin_message_id"] = draft.get("origin_message_id")
        text = inbound.content.strip()
        if action is None:
            action = self._resolve_blocker_prompt_action(text, prompt)
        if step == "offer_help":
            return await self._handle_blocker_offer_help_step(client, user, session, text, now, prompt, action)
        if step == "choose_admin":
            return await self._handle_blocker_choose_admin_step(client, user, session, text, now, prompt, action)
        if step == "declined_help_followup":
            return await self._handle_blocker_declined_followup_step(client, user, session, now, prompt, action)
        session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
        return False

    async def _handle_blocker_offer_help_step(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        text: str,
        now: datetime,
        prompt: dict[str, Any],
        action: str | None,
    ) -> bool:
        requested_admins = self._resolve_requested_admins(text)
        if requested_admins:
            self._set_blocker_state(session, "blocked_help_requested", now)
            await self._notify_requested_admins(client, user, session, text, requested_admins, now)
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(
                client,
                user,
                session,
                (
                    f"Got it. I messaged {', '.join(admin.name for admin in requested_admins)} about your blocker.\n\n"
                    "If you also want me to draft an unblocker task for someone else, say that and I will walk you through it."
                ),
                now,
            )
            return True
        if action == "draft_task":
            self._set_blocker_state(session, "blocked_help_requested", now)
            await self._begin_unblocker_task_draft(client, user, session, prompt, now)
            return True
        if action == "ask_admin":
            prompt["step"] = "choose_admin"
            prompt["help_decision"] = "admin"
            await self._send_dm(
                client,
                user,
                session,
                self._blocker_choose_admin_prompt(),
                now,
                view=self._blocker_resolution_view(user, "choose_admin"),
            )
            return True
        if action == "no_help_needed":
            prompt["step"] = "declined_help_followup"
            prompt["help_decision"] = "declined"
            await self._send_dm(
                client,
                user,
                session,
                self._blocker_declined_help_followup_prompt(),
                now,
                view=self._blocker_resolution_view(user, "declined_help_followup"),
            )
            return True
        if action == "not_blocked":
            self._clear_blocker_state(session, mark_not_blocked=True)
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(
                client,
                user,
                session,
                "Okay, I cleared that blocker and I will treat you as not blocked.",
                now,
            )
            return True
        await self._send_dm(
            client,
            user,
            session,
            self._blocker_offer_help_clarification(),
            now,
            view=self._blocker_resolution_view(user, "offer_help"),
        )
        return True

    async def _handle_blocker_choose_admin_step(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        text: str,
        now: datetime,
        prompt: dict[str, Any],
        action: str | None,
    ) -> bool:
        request_text = text
        if action and action.startswith("admin:"):
            requested_admins = self.find_admin_profiles(action.split(":", 1)[1])
            request_text = str(prompt.get("blocker_text") or session.latest_blocker or text)
        else:
            requested_admins = self._resolve_requested_admins(text)
        if requested_admins:
            self._set_blocker_state(session, "blocked_help_requested", now)
            await self._notify_requested_admins(client, user, session, request_text, requested_admins, now)
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(
                client,
                user,
                session,
                (
                    f"Got it. I messaged {', '.join(admin.name for admin in requested_admins)} about your blocker.\n\n"
                    "If you also want me to draft an unblocker task for someone else, say that and I will walk you through it."
                ),
                now,
            )
            return True
        if action == "draft_task":
            self._set_blocker_state(session, "blocked_help_requested", now)
            await self._begin_unblocker_task_draft(client, user, session, prompt, now)
            return True
        if action == "no_help_needed":
            prompt["step"] = "declined_help_followup"
            prompt["help_decision"] = "declined"
            await self._send_dm(
                client,
                user,
                session,
                self._blocker_declined_help_followup_prompt(),
                now,
                view=self._blocker_resolution_view(user, "declined_help_followup"),
            )
            return True
        if action == "not_blocked":
            self._clear_blocker_state(session, mark_not_blocked=True)
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(
                client,
                user,
                session,
                "Okay, I cleared that blocker and I will treat you as not blocked.",
                now,
            )
            return True
        await self._send_dm(
            client,
            user,
            session,
            self._blocker_choose_admin_prompt(),
            now,
            view=self._blocker_resolution_view(user, "choose_admin"),
        )
        return True

    async def _handle_blocker_declined_followup_step(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        prompt: dict[str, Any],
        action: str | None,
    ) -> bool:
        if action == "keep_logged":
            self._set_blocker_state(session, "blocked_no_help", now)
            prompt["blocked_state_after_decline"] = "blocked_no_help"
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(
                client,
                user,
                session,
                "Okay. I will keep the blocker logged locally, but I will not escalate it or keep asking about help unless you bring it up again.",
                now,
            )
            return True
        if action == "clear_it" or action == "not_blocked":
            self._clear_blocker_state(session, mark_not_blocked=True)
            prompt["blocked_state_after_decline"] = "not_blocked"
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(
                client,
                user,
                session,
                "Okay, I cleared the blocker and I will stop treating this as blocked work.",
                now,
            )
            return True
        await self._send_dm(
            client,
            user,
            session,
            self._blocker_declined_help_followup_prompt(),
            now,
            view=self._blocker_resolution_view(user, "declined_help_followup"),
        )
        return True

    async def _handle_unblocker_task_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
        prompt: dict[str, Any],
    ) -> bool:
        text = inbound.content.strip()
        lowered = text.lower()
        if lowered in {"cancel", "never mind", "nevermind", "stop"}:
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(client, user, session, "Okay, I cancelled the unblocker-task draft.", now)
            return True
        draft = prompt.get("draft")
        if not isinstance(draft, dict):
            draft = {}
            prompt["draft"] = draft
        step = str(prompt.get("step") or "title")
        if await self._resolve_task_draft_intent(
            text,
            prompt_type="unblocker_task_draft",
            step=step,
        ) == "mistaken_task_creation":
            return await self._restore_unblocker_task_draft_return_prompt(client, user, session, now, prompt)
        if step == "title":
            if not text:
                await self._send_dm(client, user, session, "I still need a short title for the unblocker task.", now)
                return True
            draft["title"] = text
            prompt["step"] = "description"
            await self._send_dm(
                client,
                user,
                session,
                "What exactly should someone do, and how would it unblock you? Give me the context I should put in the task.",
                now,
            )
            return True
        if step == "description":
            if not text:
                await self._send_dm(client, user, session, "I still need the task details and why it would unblock you.", now)
                return True
            draft["description"] = text
            prompt["step"] = "assignee"
            await self._send_dm(
                client,
                user,
                session,
                (
                    "Who should own this task? Reply with a roster/admin name, reply with your own name or say `me` / `myself` if you want me to create it immediately and switch you onto it, or say `unassigned` if you want admin to decide.\n\n"
                    f"Admins I know: {self.admin_name_list_text()}."
                ),
                now,
            )
            return True
        if step == "assignee":
            if not text:
                await self._send_dm(
                    client,
                    user,
                    session,
                    "Tell me who should own this task, say `me` if you want to take it yourself, or say `unassigned`.",
                    now,
                )
                return True
            assignee_label, assignee_id, assignee_note, self_assigned = await self._resolve_unblocker_assignee(user, text)
            draft["assignee_label"] = assignee_label
            draft["assignee_id"] = assignee_id
            draft["self_assigned"] = self_assigned
            if assignee_note:
                draft["assignee_note"] = assignee_note
            else:
                draft.pop("assignee_note", None)
            prompt["step"] = "priority"
            note_line = f"\n\nNote: {assignee_note}" if assignee_note else ""
            await self._send_dm(
                client,
                user,
                session,
                f"Got it. What priority should this task be: `urgent`, `high`, `normal`, or `low`?{note_line}",
                now,
            )
            return True
        if step == "priority":
            priority = self._normalize_priority(text)
            if not priority:
                await self._send_dm(client, user, session, "Use one of these priority values: `urgent`, `high`, `normal`, or `low`.", now)
                return True
            draft["priority"] = priority
            prompt["step"] = "due_date"
            await self._send_dm(client, user, session, "Any due date? Reply `none` or use `YYYY-MM-DD`.", now)
            return True
        if step == "due_date":
            due_date_ms = self._parse_due_date_ms(text)
            if due_date_ms is False:
                await self._send_dm(client, user, session, "Use `none` or a date like `2026-05-30`.", now)
                return True
            if due_date_ms is not None:
                draft["due_date_ms"] = due_date_ms
                draft["due_date_text"] = text
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            if draft.get("self_assigned"):
                fallback_reason = self._self_assigned_unblocker_fallback_reason(draft)
                if fallback_reason:
                    await self._submit_unblocker_task_for_admin_review(
                        client,
                        user,
                        session,
                        draft,
                        now,
                        user_message=(
                            f"{fallback_reason} I sent the unblocker-task draft to admin for review instead."
                        ),
                    )
                    return True
                await self._create_self_assigned_unblocker_task_and_switch(
                    client,
                    user,
                    session,
                    draft,
                    now,
                )
                return True
            await self._submit_unblocker_task_for_admin_review(client, user, session, draft, now)
            return True
        session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
        return False

    async def _task_onboarding_feedback(
        self,
        user: UserProfile,
        prompt: dict[str, Any],
        plan_text: str,
    ) -> str:
        task_name = str(prompt.get("task_name") or "")
        task_id = str(prompt.get("task_id") or "")
        draft = self._task_onboarding_draft(prompt)
        context = ""
        if self.clickup and task_id:
            try:
                task = await self.clickup.get_task(task_id)
            except Exception:
                context = f"Target ClickUp task: {task_name or task_id}"
            else:
                description = str(task.get("description") or "").strip()
                context = (
                    f"Target ClickUp task: {task.get('name') or task_name} ({task_id})\n"
                    f"Status: {(task.get('status') or {}).get('status') or 'unknown'}"
                )
                if description:
                    context += f"\nDescription:\n{description[:1200]}"
        admin_feedback = str(draft.get("admin_feedback") or "").strip()
        if admin_feedback:
            context += f"\nAdmin feedback:\n{admin_feedback[:1200]}"
        return await self.advisor.plan_feedback(user, plan_text, context)

    def _set_task_onboarding_candidate(self, prompt: dict[str, Any], task: dict[str, Any]) -> None:
        prompt["candidate_task_id"] = str(task.get("id") or "")
        prompt["candidate_task_name"] = str(task.get("name") or "Unnamed task")
        if task.get("_don_pollo_workspace_option"):
            prompt["candidate_workspace_option"] = True
        else:
            prompt.pop("candidate_workspace_option", None)

    def _confirm_task_onboarding_candidate(self, prompt: dict[str, Any], session: SessionState) -> None:
        prompt["task_id"] = str(prompt.get("candidate_task_id") or "")
        prompt["task_name"] = str(prompt.get("candidate_task_name") or "Unnamed task")
        prompt.pop("candidate_task_id", None)
        prompt.pop("candidate_task_name", None)
        prompt.pop("candidate_workspace_option", None)
        prompt["step"] = "plan"
        session.stage = "awaiting_plan"
        session.awaiting_start_photo = False
        session.latest_plan = None
        session.latest_feedback = None
        session.latest_blocker = None

    def _reset_task_onboarding_prompt_to_select_task(self, prompt: dict[str, Any]) -> None:
        prompt["step"] = "select_task"
        prompt["draft"] = {}
        prompt.pop("task_id", None)
        prompt.pop("task_name", None)
        prompt.pop("candidate_task_id", None)
        prompt.pop("candidate_task_name", None)
        prompt.pop("candidate_workspace_option", None)
        prompt.pop("recommended_workspace_option", None)

    def _task_onboarding_confirmation_prompt(self, prompt: dict[str, Any]) -> str:
        task_name = str(prompt.get("candidate_task_name") or prompt.get("task_name") or "that task")
        task_id = str(prompt.get("candidate_task_id") or prompt.get("task_id") or "")
        summary = f"`{task_name}`" + (f" ({task_id})" if task_id else "")
        return (
            f"I matched {summary}. Is that the task you want to onboard right now?\n\n"
            "Use the buttons, reply `yes` to confirm, reply `no` to pick again, or send the correct task name or ID."
        )

    def _task_confirmation_view(self, user: UserProfile) -> discord.ui.View:
        return _InternTaskConfirmationView(self, user)

    def _self_lookup_chooser_view(self, user: UserProfile, session_date: str) -> discord.ui.View:
        return _InternSelfLookupView(self, user, session_date=session_date, step="chooser")

    def _self_lookup_hours_range_view(self, user: UserProfile, session_date: str) -> discord.ui.View:
        return _InternSelfLookupView(self, user, session_date=session_date, step="range")

    def _day_suppression_confirmation_view(self, user: UserProfile, session_date: str) -> discord.ui.View:
        return _InternDaySuppressionView(self, user, session_date=session_date)

    def _extract_task_onboarding_correction_hint(self, text: str) -> str | None:
        stripped = text.strip()
        if not stripped:
            return None
        for pattern in _TASK_CORRECTION_WITH_HINT_PATTERNS:
            match = pattern.match(stripped)
            if match:
                return str(match.group(1) or "").strip() or ""
        for pattern in _TASK_CORRECTION_NO_HINT_PATTERNS:
            if pattern.match(stripped):
                return ""
        return None

    async def _resolve_intern_task_option(
        self,
        user: UserProfile,
        session: SessionState,
        task_hint: str,
    ) -> dict[str, Any] | None:
        if not self.clickup:
            return None
        assigned_task = await self.clickup.resolve_task_for_user(
            user,
            task_hint,
            include_mission_board=False,
        )
        if assigned_task:
            return assigned_task
        if not hasattr(self.clickup, "suggest_next_tasks") or not hasattr(self.clickup, "match_task_hint"):
            return None
        workspace_options = await self.clickup.suggest_next_tasks(
            user,
            session,
            self.list_session_messages(user.user_key, session),
            exclude_task_ids=self._recently_closed_task_ids(session),
            limit=8,
        )
        matched = self.clickup.match_task_hint(workspace_options, task_hint)
        if not matched:
            return None
        return {**matched, "_don_pollo_workspace_option": True}

    async def _restart_task_onboarding_for_correction(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        prompt: dict[str, Any],
        correction_hint: str,
        now: datetime,
    ) -> bool:
        session.awaiting_start_photo = False
        session.latest_plan = None
        session.latest_feedback = None
        session.latest_blocker = None
        if not correction_hint:
            self._reset_task_onboarding_prompt_to_select_task(prompt)
            session.stage = "awaiting_task_selection"
            await self._send_dm(
                client,
                user,
                session,
                "Okay, let's correct the task.\n\n" + await self._task_selection_prompt(user, session),
                now,
            )
            return True
        if not self.clickup:
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(client, user, session, "I cannot resolve tasks right now because ClickUp is unavailable.", now)
            return True
        task = await self._resolve_intern_task_option(user, session, correction_hint)
        if not task:
            self._reset_task_onboarding_prompt_to_select_task(prompt)
            session.stage = "awaiting_task_selection"
            await self._send_dm(
                client,
                user,
                session,
                (
                    "I could not match that corrected task cleanly yet.\n\n"
                    + await self._task_selection_prompt(user, session)
                ),
                now,
            )
            return True
        task_id = str(task.get("id") or "")
        if task_id and task_id in self._recently_closed_task_ids(session):
            self._reset_task_onboarding_prompt_to_select_task(prompt)
            session.stage = "awaiting_task_selection"
            await self._send_dm(
                client,
                user,
                session,
                (
                    f"`{task.get('name') or task_id}` was just closed, so do not pick it back up right now.\n\n"
                    f"{await self._task_selection_prompt(user, session)}"
                ),
                now,
            )
            return True
        if task.get("_don_pollo_workspace_option"):
            prompt["draft"] = {}
            self._set_task_onboarding_candidate(prompt, task)
            prompt["step"] = "confirm_task"
            session.stage = "awaiting_task_selection"
            await self._send_dm(
                client,
                user,
                session,
                self._task_onboarding_confirmation_prompt(prompt),
                now,
                view=self._task_confirmation_view(user),
            )
            return True
        prompt["draft"] = {}
        prompt["task_id"] = task_id
        prompt["task_name"] = str(task.get("name") or "Unnamed task")
        prompt.pop("candidate_task_id", None)
        prompt.pop("candidate_task_name", None)
        prompt["step"] = "plan"
        session.stage = "awaiting_plan"
        await self._send_dm(
            client,
            user,
            session,
            (
                f"Okay, restarting onboarding on `{prompt['task_name']}`.\n\n"
                + self._task_onboarding_question(prompt, "plan")
            ),
            now,
        )
        return True

    def _parse_intern_task_switch_request(self, text: str) -> str | None:
        stripped = text.strip()
        if not stripped:
            return None
        for pattern in _INTERN_TASK_SWITCH_WITH_HINT_PATTERNS:
            match = pattern.match(stripped)
            if match:
                return str(match.group(1) or "").strip() or ""
        for pattern in _INTERN_TASK_SWITCH_BARE_PATTERNS:
            if pattern.match(stripped):
                return ""
        return None

    def _is_task_creation_back_navigation(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        return any(pattern.match(stripped) for pattern in _TASK_CREATION_BACK_PATTERNS)

    def _text_contains_url(self, text: str) -> bool:
        return bool(_URL_PATTERN.search(text or ""))

    async def _maybe_handle_intern_task_switch_request(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
    ) -> bool:
        if session.stage not in {"active", "awaiting_admin_review"}:
            return False
        task_hint = self._parse_intern_task_switch_request(inbound.content)
        if task_hint is None:
            return False
        await self._start_intern_task_switch(
            client,
            user,
            session,
            now,
            task_hint=task_hint or None,
            source="intern_switch",
            reason=(
                "Okay, let's switch tasks."
                if session.stage == "active"
                else "Okay, that earlier task is already waiting on admin review. Let's pick what you are switching to next."
            ),
        )
        return True

    def _pending_intern_task_switch(self, session: SessionState) -> dict[str, Any] | None:
        value = session.metadata.get(_PENDING_INTERN_TASK_SWITCH_KEY)
        return value if isinstance(value, dict) else None

    def _clear_pending_intern_task_switch(self, session: SessionState) -> None:
        session.metadata.pop(_PENDING_INTERN_TASK_SWITCH_KEY, None)

    async def _start_intern_task_switch(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        *,
        task_hint: str | None = None,
        target_task: dict[str, Any] | None = None,
        source: str,
        reason: str,
        admin_feedback: str | None = None,
        direct_plan: bool = False,
        open_task_creation: bool = False,
    ) -> None:
        current_task_id = self._active_task_id(session)
        current_task_name = str(session.metadata.get("active_clickup_task_name") or "")
        previous_stage = session.stage
        previous_selection_reason = str(session.metadata.get("clickup_selection_reason") or "")
        pause_note: str | None = None
        if previous_stage == "active" and current_task_id:
            pause_note = await self._pause_current_task_tracking(
                user,
                session,
                now,
                set_hold=True,
                end_reason="intern_switch",
            )
        session.metadata[_PENDING_INTERN_TASK_SWITCH_KEY] = {
            "source": source,
            "previous_stage": previous_stage,
            "previous_task_id": current_task_id,
            "previous_task_name": current_task_name,
            "previous_selection_reason": previous_selection_reason,
            "started_at": now.isoformat(),
        }
        self._clear_active_task_metadata(session)
        session.awaiting_start_photo = False
        session.awaiting_clock_out_photo = False
        session.awaiting_clock_out_summary = False
        session.latest_plan = None
        session.latest_feedback = None
        prompt: dict[str, Any] = {
            "type": "task_onboarding",
            "source": source,
            "step": "select_task",
            "reason": reason,
            "draft": {},
        }
        prefix_parts = [reason]
        if pause_note:
            prefix_parts.append(pause_note)
        if open_task_creation:
            await self._start_task_creation_from_selection(client, user, session, now, prompt)
            return
        task = target_task
        if not task and task_hint and self.clickup:
            task = await self._resolve_intern_task_option(user, session, task_hint)
        if task:
            task_id = str(task.get("id") or "")
            if task_id and task_id in self._recently_closed_task_ids(session):
                session.stage = "awaiting_task_selection"
                session.metadata[_CLICKUP_PROMPT_KEY] = prompt
                session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
                await self._send_dm(
                    client,
                    user,
                    session,
                    (
                        "\n\n".join(prefix_parts)
                        + f"\n\n`{task.get('name') or task_id}` was just closed, so do not pick it back up right now.\n\n"
                        + await self._task_selection_prompt(user, session)
                    ),
                    now,
                )
                return
            if direct_plan:
                prompt["step"] = "plan"
                prompt["task_id"] = task_id
                prompt["task_name"] = str(task.get("name") or "Unnamed task")
                if admin_feedback:
                    prompt["draft"] = {"admin_feedback": admin_feedback}
                session.stage = "awaiting_plan"
                session.metadata[_CLICKUP_PROMPT_KEY] = prompt
                session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
                await self._send_dm(
                    client,
                    user,
                    session,
                    (
                        "\n\n".join(prefix_parts)
                        + f"\n\nSwitching you onto `{prompt['task_name']}`.\n\n"
                        + self._task_onboarding_question(prompt, "plan")
                    ),
                    now,
                )
                return
            self._set_task_onboarding_candidate(prompt, task)
            prompt["step"] = "confirm_task"
            session.stage = "awaiting_task_selection"
            session.metadata[_CLICKUP_PROMPT_KEY] = prompt
            session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
            await self._send_dm(
                client,
                user,
                session,
                "\n\n".join(prefix_parts + [self._task_onboarding_confirmation_prompt(prompt)]),
                now,
                view=self._task_confirmation_view(user),
            )
            return
        session.stage = "awaiting_task_selection"
        session.metadata[_CLICKUP_PROMPT_KEY] = prompt
        session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
        selection_prompt = await self._task_selection_prompt(user, session)
        if task_hint:
            prefix_parts.append(f"I could not match `{task_hint}` cleanly, so pick from your assigned tasks below.")
        await self._send_dm(
            client,
            user,
            session,
            "\n\n".join(prefix_parts + [selection_prompt]),
            now,
        )

    def _clear_task_onboarding_metadata(self, session: SessionState) -> None:
        for metadata_key in _TASK_ONBOARDING_METADATA_FIELDS.values():
            session.metadata.pop(metadata_key, None)
        session.metadata.pop("last_task_onboarding_summary", None)

    async def _return_task_onboarding_to_selection(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        prompt: dict[str, Any],
        *,
        message_prefix: str | None = None,
        clear_active_task: bool = False,
        clear_onboarding_metadata: bool = False,
    ) -> bool:
        if clear_active_task:
            self._clear_active_task_metadata(session)
        if clear_onboarding_metadata:
            self._clear_task_onboarding_metadata(session)
        self._reset_task_onboarding_prompt_to_select_task(prompt)
        session.stage = "awaiting_task_selection"
        session.awaiting_start_photo = False
        session.latest_plan = None
        session.latest_feedback = None
        selection_prompt = await self._task_selection_prompt(user, session)
        content = selection_prompt if not message_prefix else f"{message_prefix}\n\n{selection_prompt}"
        await self._send_dm(client, user, session, content, now)
        return True

    async def _return_task_creation_to_selection(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        prompt: dict[str, Any],
        *,
        message_prefix: str | None = None,
    ) -> bool:
        source = str(prompt.get("source") or "task_onboarding")
        session.stage = "awaiting_task_selection"
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_onboarding",
            "source": source,
            "step": "select_task",
            "reason": str(prompt.get("reason") or ""),
            "draft": {},
        }
        session.awaiting_start_photo = False
        session.latest_plan = None
        session.latest_feedback = None
        session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
        selection_prompt = await self._task_selection_prompt(user, session)
        content = selection_prompt if not message_prefix else f"{message_prefix}\n\n{selection_prompt}"
        await self._send_dm(client, user, session, content, now)
        return True

    async def _restore_cancelled_intern_task_switch(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        pending = self._pending_intern_task_switch(session)
        if not pending:
            return False
        previous_stage = str(pending.get("previous_stage") or "active")
        previous_task_id = str(pending.get("previous_task_id") or "")
        previous_task_name = str(pending.get("previous_task_name") or "")
        previous_selection_reason = str(pending.get("previous_selection_reason") or "")
        self._clear_pending_intern_task_switch(session)
        session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
        if previous_stage == "active" and previous_task_id:
            session.metadata["active_clickup_task_id"] = previous_task_id
            if previous_task_name:
                session.metadata["active_clickup_task_name"] = previous_task_name
            if previous_selection_reason:
                session.metadata["clickup_selection_reason"] = previous_selection_reason
            session.stage = "active"
            await self._activate_clickup_task(user, session, now, previous_task_id, previous_task_name)
            await self._send_dm(
                client,
                user,
                session,
                f"Okay, I cancelled the task switch and resumed `{previous_task_name or previous_task_id}`.",
                now,
            )
            return True
        self._clear_active_task_metadata(session)
        session.stage = previous_stage
        if previous_stage == "awaiting_admin_review":
            await self._send_dm(
                client,
                user,
                session,
                "Okay, I cancelled the task switch. Your earlier task is still waiting on admin review.",
                now,
            )
        else:
            await self._send_dm(client, user, session, "Okay, I cancelled the task switch.", now)
        return True

    def _queued_review_choice_view(self, user: UserProfile) -> discord.ui.View:
        return _InternQueuedReviewChoiceView(self, user)

    async def _handle_queued_review_rework_decision_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
        prompt: dict[str, Any],
        *,
        action: str | None = None,
    ) -> bool:
        choice = (action or inbound.content.strip()).strip().lower()
        if choice in {"switch_now", "switch now", "switch", "yes"} or choice.startswith("switch"):
            target_task = {
                "id": str(prompt.get("task_id") or ""),
                "name": str(prompt.get("task_name") or "Unnamed task"),
            }
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._start_intern_task_switch(
                client,
                user,
                session,
                now,
                target_task=target_task,
                source="review_rework_switch",
                reason="Okay, let's switch back to the rework task.",
                admin_feedback=str(prompt.get("admin_feedback") or "").strip() or None,
                direct_plan=True,
            )
            return True
        if choice in {"stay_current", "stay current", "stay on current task", "stay", "no"} or choice.startswith("stay"):
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(
                client,
                user,
                session,
                (
                    f"Okay, I left your current task active. `{prompt.get('task_name') or 'That task'}` is available when you are ready to switch back."
                ),
                now,
            )
            return True
        await self._send_dm(
            client,
            user,
            session,
            "Reply `switch now` or `stay current`, or use the buttons.",
            now,
            view=self._queued_review_choice_view(user),
        )
        return True

    def _is_task_creation_request(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        return any(pattern.match(stripped) for pattern in _TASK_CREATION_REQUEST_PATTERNS)

    def _extract_task_parent_id(self, task: dict[str, Any]) -> str | None:
        raw_parent = task.get("parent")
        if isinstance(raw_parent, dict):
            parent_id = str(raw_parent.get("id") or "").strip()
            return parent_id or None
        parent_id = str(raw_parent or "").strip()
        return parent_id or None

    def _extract_task_list_id(self, task: dict[str, Any]) -> str | None:
        list_payload = task.get("list")
        if not isinstance(list_payload, dict):
            return None
        list_id = str(list_payload.get("id") or "").strip()
        return list_id or None

    def _task_option_location_label(self, task: dict[str, Any]) -> str:
        if self.clickup and hasattr(self.clickup, "task_location_label"):
            return str(self.clickup.task_location_label(task))
        labels: list[str] = []
        for key in ("space", "folder", "list"):
            payload = task.get(key)
            if not isinstance(payload, dict):
                continue
            label = str(payload.get("name") or "").strip()
            if label and label not in labels:
                labels.append(label)
        return " / ".join(labels) or "ClickUp workspace"

    def _task_workspace_option_line(self, task: dict[str, Any], *, recommended: bool) -> str:
        task_id = str(task.get("id") or "unknown")
        task_name = str(task.get("name") or "Unnamed task")
        priority = (task.get("priority") or {}).get("priority") or "none"
        marker = " [recommended]" if recommended else ""
        line = (
            f"- `{task_name}` | id={task_id} | {self._task_option_location_label(task)} | "
            f"priority={priority}{marker}"
        )
        reason = str(task.get("_don_pollo_suggestion_reason") or "").strip()
        if reason:
            line += f" | why: {reason}"
        return line

    async def _task_selection_context(
        self,
        user: UserProfile,
        session: SessionState | None = None,
        *,
        tasks: list[dict[str, Any]] | None = None,
        recommended_task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.clickup:
            return {
                "message": "Tell me which ClickUp task you are working on right now.",
                "tree_text": "",
                "candidate_tasks": [],
                "hidden_count": 0,
            }
        assigned_tasks = list(tasks) if tasks is not None else await self.clickup.list_assigned_tasks(user, limit=8)
        hidden_count = 0
        recently_closed_ids: set[str] = set()
        if session is not None:
            recently_closed_ids = self._recently_closed_task_ids(session)
            if recently_closed_ids:
                original_count = len(assigned_tasks)
                assigned_tasks = [
                    task for task in assigned_tasks
                    if str(task.get("id") or "") not in recently_closed_ids
                ]
                hidden_count = original_count - len(assigned_tasks)
        assigned_ids = {
            str(task.get("id") or "")
            for task in assigned_tasks
            if str(task.get("id") or "")
        }
        workspace_options: list[dict[str, Any]] = []
        if session is not None and hasattr(self.clickup, "suggest_next_tasks"):
            workspace_options = await self.clickup.suggest_next_tasks(
                user,
                session,
                self.list_session_messages(user.user_key, session),
                exclude_task_ids=assigned_ids | recently_closed_ids,
                limit=3,
            )
        if not assigned_tasks and not workspace_options:
            message = (
                "I cannot see any assigned tasks or strong open workspace options yet. "
                "Reply `create task` to propose a new one for Erik or George to approve."
            )
            if hidden_count:
                message = "I hid tasks that were already closed today. " + message
            return {
                "message": message,
                "tree_text": "",
                "candidate_tasks": [],
                "hidden_count": hidden_count,
            }
        raw_tasks_by_id: dict[str, dict[str, Any]] = {}
        tree_text = ""
        if assigned_tasks and hasattr(self.clickup, "build_assigned_task_hierarchy") and hasattr(self.clickup, "render_assigned_task_hierarchy"):
            hierarchy = await self.clickup.build_assigned_task_hierarchy(
                user,
                tasks=assigned_tasks,
                limit=max(len(assigned_tasks), 8),
            )
            recommended_task_id = str((recommended_task or {}).get("id") or "") or None
            tree_text = self.clickup.render_assigned_task_hierarchy(
                hierarchy,
                recommended_task_id=recommended_task_id,
            )
            raw_tasks_by_id = hierarchy.get("tasks_by_id")
            raw_tasks_by_id = raw_tasks_by_id if isinstance(raw_tasks_by_id, dict) else {}
        elif assigned_tasks:
            raw_tasks_by_id = {
                str(task.get("id") or ""): task
                for task in assigned_tasks
                if str(task.get("id") or "").strip()
            }
            tree_lines = [
                f"\\- {str(task.get('name') or task_id)} | id={task_id} [assigned]"
                for task_id, task in raw_tasks_by_id.items()
            ]
            tree_text = "\n".join(tree_lines)
        candidate_tasks = [
            {
                "id": str(task_id),
                "name": str((task or {}).get("name") or task_id),
                "parent_task_id": self._extract_task_parent_id(task or {}) or "",
                "list_id": self._extract_task_list_id(task or {}) or "",
            }
            for task_id, task in raw_tasks_by_id.items()
            if str(task_id).strip()
        ]
        candidate_tasks.extend(
            {
                "id": str(task.get("id") or ""),
                "name": str(task.get("name") or "Unnamed task"),
                "parent_task_id": self._extract_task_parent_id(task) or "",
                "list_id": self._extract_task_list_id(task) or "",
                "workspace_option": True,
                "location": self._task_option_location_label(task),
            }
            for task in workspace_options
            if str(task.get("id") or "")
        )
        lines = ["I need to confirm your active ClickUp task before you continue. Reply with the task name or ID."]
        if assigned_tasks:
            lines.extend(["", "Assigned task tree:", tree_text])
        if workspace_options:
            recommendation_id = str((recommended_task or {}).get("id") or "")
            lines.extend(["", "Other open options across ClickUp spaces:"])
            lines.extend(
                self._task_workspace_option_line(
                    task,
                    recommended=(
                        str(task.get("id") or "") == recommendation_id
                        or (not recommendation_id and index == 0)
                    ),
                )
                for index, task in enumerate(workspace_options)
            )
        if hidden_count:
            lines.extend(
                [
                    "",
                    "I hid tasks that were already closed today.",
                ]
            )
        lines.extend(
            [
                "",
                "Reply with the name or ID of an `[assigned]` task or a shown workspace option, or reply `create task` if none of these fit. New tasks still require Erik or George approval.",
            ]
        )
        return {
            "message": "\n".join(lines),
            "tree_text": tree_text,
            "candidate_tasks": candidate_tasks,
            "hidden_count": hidden_count,
        }

    async def _start_task_creation_from_selection(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        prompt: dict[str, Any],
    ) -> None:
        source = str(prompt.get("source") or "task_onboarding")
        selection_context = await self._task_selection_context(user, session)
        session.stage = "awaiting_task_selection"
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_creation",
            "source": source,
            "reason": str(prompt.get("reason") or ""),
            "step": "placement",
            "tree_text": selection_context["tree_text"],
            "placement_candidates": selection_context["candidate_tasks"],
            "draft": {},
        }
        session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
        await self._send_dm(
            client,
            user,
            session,
            self._task_creation_placement_prompt(session.metadata[_CLICKUP_PROMPT_KEY]),
            now,
        )

    async def _return_task_creation_to_existing_selection(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        prompt: dict[str, Any],
    ) -> bool:
        source = str(prompt.get("source") or "task_onboarding")
        session.stage = "awaiting_task_selection"
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_onboarding",
            "source": source,
            "step": "select_task",
            "reason": str(prompt.get("reason") or ""),
            "draft": {},
        }
        session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
        await self._send_dm(
            client,
            user,
            session,
            "Okay, I won't create a new task.\n\n" + await self._task_selection_prompt(user, session),
            now,
        )
        return True

    def _task_creation_placement_prompt(self, prompt: dict[str, Any]) -> str:
        tree_text = str(prompt.get("tree_text") or "").strip()
        candidates = prompt.get("placement_candidates")
        candidates = candidates if isinstance(candidates, list) else []
        workspace_options = [
            candidate
            for candidate in candidates
            if isinstance(candidate, dict) and candidate.get("workspace_option")
        ]
        sections: list[str] = []
        if tree_text:
            sections.extend(
                [
                    "Here is the current assigned task hierarchy I can see:",
                    tree_text,
                ]
            )
        if workspace_options:
            option_lines = ["Other possible parent tasks across ClickUp spaces:"]
            option_lines.extend(
                f"- `{candidate.get('name') or candidate.get('id')}` | "
                f"id={candidate.get('id')} | {candidate.get('location') or 'ClickUp workspace'}"
                for candidate in workspace_options
            )
            sections.append("\n".join(option_lines))
        if sections:
            return (
                "Okay, let's create a new task.\n\n"
                + "\n\n".join(sections)
                + "\n\nReply with a shown parent task name or ID if the new task belongs under it, "
                "or reply `top level` if it does not fit anywhere shown."
            )
        return (
            "Okay, let's create a new task.\n\n"
            "I do not have any visible assigned branches to place it under right now. Reply `top level` to create it at the top level."
        )

    async def _cancel_task_creation_to_selection(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        prompt: dict[str, Any],
    ) -> bool:
        source = str(prompt.get("source") or "")
        if source in {"intern_switch", "review_rework_switch"} and await self._restore_cancelled_intern_task_switch(client, user, session, now):
            return True
        session.stage = "awaiting_task_selection"
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_onboarding",
            "source": source or "task_onboarding",
            "step": "select_task",
            "reason": str(prompt.get("reason") or ""),
            "draft": {},
        }
        session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
        await self._send_dm(
            client,
            user,
            session,
            "Okay, I cancelled new-task creation.\n\n" + await self._task_selection_prompt(user, session),
            now,
        )
        return True

    async def _restore_blocker_task_draft_to_opt_in(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        prompt: dict[str, Any],
    ) -> bool:
        draft = prompt.get("draft")
        origin_message_id = None
        if isinstance(draft, dict):
            origin_message_id = draft.get("origin_message_id")
        prompt["step"] = "opt_in"
        prompt["draft"] = {"origin_message_id": origin_message_id} if origin_message_id else {}
        session.metadata[_CLICKUP_PROMPT_KEY] = prompt
        await self._send_dm(
            client,
            user,
            session,
            "Okay, I won't create a blocker task.\n\n" + self._blocker_task_opt_in_prompt(),
            now,
        )
        return True

    async def _restore_unblocker_task_draft_return_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        prompt: dict[str, Any],
    ) -> bool:
        return_prompt = prompt.get("return_prompt")
        if not isinstance(return_prompt, dict):
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(client, user, session, "Okay, I cancelled the unblocker-task draft.", now)
            return True
        restored_prompt = deepcopy(return_prompt)
        step = str(restored_prompt.get("step") or "offer_help")
        session.metadata[_CLICKUP_PROMPT_KEY] = restored_prompt
        await self._send_dm(
            client,
            user,
            session,
            "Okay, I won't create an unblocker task.\n\n" + self._blocker_resolution_prompt_text(restored_prompt),
            now,
            view=self._blocker_resolution_view(user, step),
        )
        return True

    async def _resolve_task_draft_intent(
        self,
        text: str,
        *,
        prompt_type: str,
        step: str,
        source: str | None = None,
    ) -> str | None:
        interpreter = getattr(self, "interface_intelligence", None)
        if not interpreter or not hasattr(interpreter, "resolve_task_draft_intent"):
            return None
        try:
            match = await interpreter.resolve_task_draft_intent(
                text,
                prompt_type=prompt_type,
                step=step,
                source=source,
            )
        except Exception:
            return None
        if not match:
            return None
        action = str(getattr(match, "action", "") or "").strip().lower()
        return action or None

    async def _resolve_daily_availability_intent(
        self,
        text: str,
        *,
        stage: str,
    ) -> str | None:
        interpreter = getattr(self, "interface_intelligence", None)
        if interpreter and hasattr(interpreter, "resolve_daily_availability_intent"):
            try:
                match = await interpreter.resolve_daily_availability_intent(
                    text,
                    stage=stage,
                )
            except Exception:
                match = None
            if match:
                action = str(getattr(match, "action", "") or "").strip().lower()
                if action:
                    return action
        if self._looks_like_not_working_today_request(text):
            return "not_working_today"
        return None

    def _task_creation_candidates(self, prompt: dict[str, Any]) -> list[dict[str, Any]]:
        raw = prompt.get("placement_candidates")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def _match_task_creation_parent_candidate(
        self,
        prompt: dict[str, Any],
        text: str,
    ) -> dict[str, Any] | None:
        candidates = self._task_creation_candidates(prompt)
        if not candidates or not text.strip():
            return None
        if self.clickup and hasattr(self.clickup, "match_task_hint"):
            return self.clickup.match_task_hint(candidates, text)
        normalized_hint = self._normalize_identifier_value(text)
        lowered_hint = text.strip().lower()
        for candidate in candidates:
            candidate_id = str(candidate.get("id") or "")
            candidate_name = str(candidate.get("name") or "")
            if candidate_id and candidate_id.lower() == lowered_hint:
                return candidate
            normalized_name = self._normalize_identifier_value(candidate_name)
            if normalized_hint and (normalized_name == normalized_hint or normalized_hint in normalized_name):
                return candidate
        return None

    async def _send_created_task_admin_notice(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        *,
        created_task_id: str,
        created_task_name: str,
        parent_task_id: str | None,
        parent_task_name: str | None,
        description: str,
    ) -> None:
        placement_line = (
            f"Placed under `{parent_task_name or parent_task_id}`"
            if parent_task_id
            else "Placed at top level in Mission Board"
        )
        message = (
            f"{user.display_name} created a new ClickUp task and switched onto it.\n\n"
            f"Task: `{created_task_name}`"
            + (f" ({created_task_id})" if created_task_id else "")
            + "\n"
            f"{placement_line}\n\n"
            f"Intern-provided context:\n{description}"
        )
        await self._send_admin_notice(client, message, user=user, session=session)

    def _task_proposal_category(self, draft: dict[str, Any]) -> str:
        if not self.config:
            return "project"
        text = " ".join(
            str(draft.get(key) or "")
            for key in ("title", "description", "parent_task_name")
        )
        for pattern in self.config.labor.overhead_name_patterns:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return "overhead"
            except re.error:
                logger.warning("Ignoring invalid overhead task regex pattern %r.", pattern)
        return "project"

    def _task_approval_admins(self) -> list[AdminProfile]:
        admins = self.admin_profiles()
        if not self.config:
            return admins
        allowed = {
            self._normalize_identifier_value(name)
            for name in self.config.clickup.new_task_approver_names
            if name.strip()
        }
        selected = [
            admin
            for admin in admins
            if any(
                allowed_name == self._normalize_identifier_value(admin.name)
                or self._normalize_identifier_value(admin.name).startswith(allowed_name)
                or allowed_name.startswith(self._normalize_identifier_value(admin.name))
                for allowed_name in allowed
            )
        ]
        return selected or admins

    async def _submit_new_task_for_admin_review(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        prompt: dict[str, Any],
        draft: dict[str, Any],
        now: datetime,
    ) -> None:
        proposal = {
            "submitted_at": now.isoformat(),
            "category": self._task_proposal_category(draft),
            "source": str(prompt.get("source") or "task_onboarding"),
            "reason": str(prompt.get("reason") or ""),
            "draft": deepcopy(draft),
            "original_prompt": deepcopy(prompt),
        }
        session.metadata["pending_admin_task_proposal"] = proposal
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_creation_pending_approval",
            "submitted_at": now.isoformat(),
        }
        session.stage = "awaiting_task_selection"
        category = str(proposal["category"])
        await self._send_dm(
            client,
            user,
            session,
            (
                f"I submitted `{draft.get('title')}` as a {category} task proposal to Erik and George. "
                "I have not created it in ClickUp or started its timer. I will continue onboarding "
                "after one of them approves it."
            ),
            now,
        )
        await self._send_admin_task_proposal_request(client, user, session)

    async def _send_admin_task_proposal_request(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
    ) -> None:
        proposal = self._pending_admin_task_proposal(session) or {}
        draft = proposal.get("draft") if isinstance(proposal, dict) else {}
        if not isinstance(draft, dict):
            return
        placement = (
            f"under `{draft.get('parent_task_name') or draft.get('parent_task_id')}`"
            if draft.get("parent_task_id")
            else "at the top level in Mission Board"
        )
        message = (
            f"{user.display_name} proposed a new {proposal.get('category') or 'project'} task. "
            "It has not been created in ClickUp.\n\n"
            f"Task: `{draft.get('title') or 'Untitled task'}`\n"
            f"Placement: {placement}\n"
            f"Requested assignee: {user.display_name}\n\n"
            f"Context:\n{draft.get('description') or 'No description provided.'}\n\n"
            f"Approve with `run review.task_proposal_approve user={user.user_key}`.\n"
            f"Request changes with `run review.task_proposal_revise user={user.user_key} comments=\"...\"`."
        )
        await self._send_admin_notice(
            client,
            message,
            target_admins=self._task_approval_admins(),
            user=user,
            session=session,
        )

    async def _handle_pending_task_proposal_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
    ) -> bool:
        del inbound
        proposal = self._pending_admin_task_proposal(session)
        if not proposal:
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            return False
        draft = proposal.get("draft") if isinstance(proposal, dict) else {}
        title = str(draft.get("title") or "the proposed task") if isinstance(draft, dict) else "the proposed task"
        await self._send_dm(
            client,
            user,
            session,
            f"`{title}` is still waiting for Erik or George to approve it. It has not been created or timed yet.",
            now,
        )
        return True

    async def _handle_task_creation_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
        prompt: dict[str, Any],
    ) -> bool:
        text = inbound.content.strip()
        lowered = text.lower()
        if lowered in {"cancel", "never mind", "nevermind", "stop"}:
            return await self._cancel_task_creation_to_selection(client, user, session, now, prompt)
        draft = prompt.get("draft")
        if not isinstance(draft, dict):
            draft = {}
            prompt["draft"] = draft
        step = str(prompt.get("step") or "placement")
        if step == "placement":
            if self._is_task_creation_back_navigation(text):
                return await self._return_task_creation_to_selection(
                    client,
                    user,
                    session,
                    now,
                    prompt,
                    message_prefix="Okay, let's go back to your task tree.",
                )
            if await self._resolve_task_draft_intent(
                text,
                prompt_type="task_creation",
                step=step,
                source=str(prompt.get("source") or ""),
            ) == "mistaken_task_creation":
                return await self._return_task_creation_to_existing_selection(client, user, session, now, prompt)
            if lowered in {"top level", "toplevel", "top"}:
                mission_board_list_id = self.config.clickup.mission_board_list_id if self.config else None
                if not mission_board_list_id:
                    await self._send_dm(
                        client,
                        user,
                        session,
                        "I cannot create a top-level task because Mission Board is not configured here. Reply with a shown parent task name or ID instead.",
                        now,
                    )
                    return True
                draft["parent_task_id"] = None
                draft["parent_task_name"] = None
                draft["list_id"] = mission_board_list_id
                prompt["step"] = "title"
                await self._send_dm(
                    client,
                    user,
                    session,
                    "Got it. What should the new task be called?",
                    now,
                )
                return True
            candidate = self._match_task_creation_parent_candidate(prompt, text)
            if not candidate:
                await self._send_dm(
                    client,
                    user,
                    session,
                    self._task_creation_placement_prompt(prompt),
                    now,
                )
                return True
            list_id = str(candidate.get("list_id") or "").strip()
            if not list_id:
                await self._send_dm(
                    client,
                    user,
                    session,
                    (
                        f"I could not confirm the ClickUp list for `{candidate.get('name') or candidate.get('id')}`, "
                        "so choose another shown parent task or reply `top level`."
                    ),
                    now,
                )
                return True
            draft["parent_task_id"] = str(candidate.get("id") or "")
            draft["parent_task_name"] = str(candidate.get("name") or "")
            draft["list_id"] = list_id
            prompt["step"] = "title"
            await self._send_dm(
                client,
                user,
                session,
                f"Got it. I will place the new task under `{draft['parent_task_name']}`. What should the new task be called?",
                now,
            )
            return True
        if step == "title":
            if self._is_task_creation_back_navigation(text):
                draft.pop("title", None)
                prompt["step"] = "placement"
                await self._send_dm(
                    client,
                    user,
                    session,
                    self._task_creation_placement_prompt(prompt),
                    now,
                )
                return True
            if await self._resolve_task_draft_intent(
                text,
                prompt_type="task_creation",
                step=step,
                source=str(prompt.get("source") or ""),
            ) == "mistaken_task_creation":
                return await self._return_task_creation_to_existing_selection(client, user, session, now, prompt)
            if not text:
                await self._send_dm(client, user, session, "I still need a task title before I can create it.", now)
                return True
            draft["title"] = text
            prompt["step"] = "description"
            await self._send_dm(
                client,
                user,
                session,
                "Give me a short description or context for the new task so I can create it cleanly in ClickUp.",
                now,
            )
            return True
        if step == "description":
            if self._is_task_creation_back_navigation(text):
                draft.pop("description", None)
                draft.pop("title", None)
                prompt["step"] = "title"
                await self._send_dm(
                    client,
                    user,
                    session,
                    "Okay, let's go back. What should the new task be called?",
                    now,
                )
                return True
            if await self._resolve_task_draft_intent(
                text,
                prompt_type="task_creation",
                step=step,
                source=str(prompt.get("source") or ""),
            ) == "mistaken_task_creation":
                return await self._return_task_creation_to_existing_selection(client, user, session, now, prompt)
            if not text:
                await self._send_dm(client, user, session, "I still need a short description for the new task.", now)
                return True
            if not self.config or not self.clickup:
                await self._send_dm(client, user, session, "I cannot create ClickUp tasks right now because ClickUp is unavailable.", now)
                return True
            assignee_id = await self.clickup.resolve_clickup_user_id(user)
            if not assignee_id:
                await self._send_dm(
                    client,
                    user,
                    session,
                    "I could not confirm your ClickUp assignee ID, so I did not create the task yet. Ask admin to fix your ClickUp user mapping and then try again.",
                    now,
                )
                return True
            list_id = str(draft.get("list_id") or "").strip()
            if not list_id:
                await self._send_dm(
                    client,
                    user,
                    session,
                    "I lost the placement details for that new task. Reply `cancel` and start task creation again.",
                    now,
                )
                return True
            draft["description"] = text
            draft["assignee_id"] = assignee_id
            if self.config.clickup.new_task_approval_required:
                await self._submit_new_task_for_admin_review(
                    client,
                    user,
                    session,
                    prompt,
                    draft,
                    now,
                )
                return True
            created = await self.clickup.create_task(
                list_id,
                name=str(draft.get("title") or "Untitled task"),
                description=text,
                assignee_ids=[assignee_id],
                parent_task_id=str(draft.get("parent_task_id") or "").strip() or None,
            )
            created_task_id = str(created.get("id") or "")
            created_task_name = str(created.get("name") or draft.get("title") or "the new task")
            source = str(prompt.get("source") or "task_onboarding")
            session.metadata[_CLICKUP_PROMPT_KEY] = {
                "type": "task_onboarding",
                "source": source,
                "step": "plan",
                "task_id": created_task_id,
                "task_name": created_task_name,
                "reason": str(prompt.get("reason") or "Created a new ClickUp task because none of the shown tasks fit."),
                "draft": {},
            }
            session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
            session.stage = "awaiting_plan"
            session.latest_plan = None
            session.latest_feedback = None
            await self._send_created_task_admin_notice(
                client,
                user,
                session,
                now,
                created_task_id=created_task_id,
                created_task_name=created_task_name,
                parent_task_id=str(draft.get("parent_task_id") or "").strip() or None,
                parent_task_name=str(draft.get("parent_task_name") or "").strip() or None,
                description=text,
            )
            placement_text = (
                f"under `{draft.get('parent_task_name')}`"
                if draft.get("parent_task_id")
                else "at the top level in Mission Board"
            )
            await self._send_dm(
                client,
                user,
                session,
                (
                    f"Done. I created `{created_task_name}` {placement_text} and assigned it to you.\n\n"
                    + self._task_onboarding_question(session.metadata[_CLICKUP_PROMPT_KEY], "plan")
                ),
                now,
            )
            return True
        session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
        return False

    async def _task_selection_prompt(
        self,
        user: UserProfile,
        session: SessionState | None = None,
        *,
        tasks: list[dict[str, Any]] | None = None,
        recommended_task: dict[str, Any] | None = None,
    ) -> str:
        context = await self._task_selection_context(
            user,
            session,
            tasks=tasks,
            recommended_task=recommended_task,
        )
        return str(context.get("message") or "Tell me which ClickUp task you are working on right now.")

    async def _finish_task_onboarding(
        self,
        user: UserProfile,
        session: SessionState,
        prompt: dict[str, Any],
        now: datetime,
    ) -> TaskActivationResult:
        task_id = str(prompt.get("task_id") or "")
        task_name = str(prompt.get("task_name") or "")
        self._apply_task_onboarding_metadata(session, prompt)
        if task_id:
            session.metadata["active_clickup_task_id"] = task_id
        if task_name:
            session.metadata["active_clickup_task_name"] = task_name
        source = str(prompt.get("source") or "task_onboarding")
        if source == "admin_switch":
            selection_reason = "Confirmed by intern during admin task switch."
        elif source == "intern_switch":
            selection_reason = "Confirmed by intern during a self-initiated task switch."
        elif source == "review_rework_switch":
            selection_reason = "Confirmed by intern while switching back onto a rework task."
        elif source == "self_assigned_unblocker_task":
            selection_reason = "Started by the intern through a self-assigned unblocker task."
        elif source in {"daily_clock_in", "clock_in_recovery"}:
            selection_reason = "Confirmed by intern during daily clock-in onboarding."
        elif source == "same_day_reclockin":
            selection_reason = "Confirmed by intern after same-day re-clock-in."
        else:
            selection_reason = "Confirmed by intern during task onboarding."
        session.metadata["clickup_selection_reason"] = selection_reason
        if source in {"intern_switch", "review_rework_switch"}:
            self._clear_pending_intern_task_switch(session)
        session.stage = "active"
        session.clocked_out_at = None
        session.awaiting_clock_out_photo = False
        session.awaiting_clock_out_summary = False
        if not session.intake_completed_at:
            session.intake_completed_at = now.isoformat()
        session.last_follow_up_at = now.isoformat()
        activation = await self._activate_clickup_task(user, session, now, task_id, task_name)
        session.metadata["last_task_onboarding_completed_at"] = now.isoformat()
        return activation

    async def _maybe_start_lunch_break(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        *,
        automatic: bool = False,
    ) -> bool:
        self._clear_pending_lunch_confirmation(session)
        if self._progress_probe_prompt(session):
            await self._close_progress_probe(
                client,
                user,
                session,
                now,
                reason="converted_to_lunch",
            )
        else:
            self._clear_follow_up_probe_tracking(session)
        if not session.clocked_in_at or session.clocked_out_at:
            await self._send_dm(
                client,
                user,
                session,
                "I can log a lunch break once you are clocked in for the day.",
                now,
            )
            return True
        if session.stage == "on_lunch_break":
            await self._send_dm(
                client,
                user,
                session,
                "You are already marked on lunch break. I will check back every 30 minutes until you tell me you are back.",
                now,
            )
            return True
        if session.stage in {"awaiting_clock_out_artifacts", "clocked_out"} or (
            session.stage == "awaiting_admin_review" and not automatic
        ):
            await self._send_dm(
                client,
                user,
                session,
                "I cannot start a lunch break from this part of the workflow right now.",
                now,
            )
            return True
        allowed_stages = {
            "active",
            "awaiting_task_selection",
            "awaiting_plan",
            "awaiting_start_photo",
            "awaiting_risk",
        }
        if automatic:
            allowed_stages.add("awaiting_admin_review")
        if session.stage not in allowed_stages:
            return False
        active_task_id = self._active_task_id(session)
        active_task_name = str(session.metadata.get("active_clickup_task_name") or "")
        previous_stage = session.stage
        pause_note = await self._pause_current_task_tracking(
            user,
            session,
            now,
            set_hold=False,
            end_reason="lunch_break",
        )
        self._close_current_work_segment(session, now)
        self._record_lunch_window_start(session, now)
        session.metadata["lunch_started_at"] = now.isoformat()
        session.metadata["lunch_last_prompt_at"] = now.isoformat()
        session.metadata["lunch_resume_stage"] = previous_stage
        session.metadata.pop("meal_guidance_queued_at", None)
        session.metadata.pop("lunch_ended_at", None)
        session.metadata.pop("lunch_resume_requested_at", None)
        if active_task_id:
            session.metadata["lunch_resume_task_id"] = active_task_id
        if active_task_name:
            session.metadata["lunch_resume_task_name"] = active_task_name
        session.stage = "on_lunch_break"
        if automatic:
            parts = [
                "I automatically paused your work time because you reached the meal-break deadline without starting lunch.",
                "You are now marked on lunch. I will check back every 30 minutes; tell me when you are back before resuming work.",
            ]
        else:
            parts = [
                "Got it. I marked you on lunch break.",
                "I will check back every 30 minutes until you tell me you are back.",
            ]
        if pause_note:
            parts.append(pause_note)
        await self._send_dm(client, user, session, "\n\n".join(parts), now)
        return True

    async def _maybe_handle_lunch_confirmation(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        signals,
        now: datetime,
    ) -> bool:
        if session.stage == "on_lunch_break":
            return False
        text = inbound.content.strip()
        pending = bool(session.metadata.get(_LUNCH_CONFIRMATION_REQUESTED_AT_KEY))
        if pending:
            if not self._can_offer_lunch_confirmation(session):
                self._clear_pending_lunch_confirmation(session)
                return False
            if getattr(signals, "starting_lunch", False) or self._is_affirmative_reply(text):
                self._clear_pending_lunch_confirmation(session)
                return await self._maybe_start_lunch_break(client, user, session, now)
            if self._is_negative_reply(text):
                self._clear_pending_lunch_confirmation(session)
                await self._send_dm(
                    client,
                    user,
                    session,
                    "Okay, I will keep you on your current work. Tell me when you want to start lunch.",
                    now,
                )
                return True
            await self._send_dm(
                client,
                user,
                session,
                "You mentioned lunch. Do you want me to start a lunch break right now? Reply `yes` or `no`.",
                now,
            )
            return True
        if not self._can_offer_lunch_confirmation(session):
            self._clear_pending_lunch_confirmation(session)
            return False
        if not text or getattr(signals, "starting_lunch", False) or getattr(signals, "ending_lunch", False):
            return False
        if not self._message_mentions_lunch(text):
            return False
        session.metadata[_LUNCH_CONFIRMATION_REQUESTED_AT_KEY] = now.isoformat()
        await self._send_dm(
            client,
            user,
            session,
            "You mentioned lunch. Do you want me to start a lunch break right now? Reply `yes` or `no`.",
            now,
        )
        return True

    async def _handle_lunch_break_message(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        signals,
        now: datetime,
    ) -> None:
        text = inbound.content.strip()
        if signals.clocking_out:
            await self._start_clock_out(client, user, session, inbound, now)
            return
        if (
            getattr(signals, "ending_lunch", False)
            or getattr(signals, "clocked_in", False)
            or self._looks_like_lunch_resume_reply(text)
            or self._is_affirmative_reply(text)
        ):
            returned_at = self._reported_lunch_return_at(user, session, text, now)
            await self._end_lunch_break(
                client,
                user,
                session,
                now,
                returned_at=returned_at,
            )
            return
        if self._is_negative_reply(text):
            session.metadata["lunch_last_prompt_at"] = now.isoformat()
            await self._send_dm(
                client,
                user,
                session,
                "Okay, enjoy lunch. I will check back again in 30 minutes.",
                now,
            )
            return
        await self._send_dm(
            client,
            user,
            session,
            "You are marked on lunch break. Tell me when you are back, or reply `no` if you are still at lunch.",
            now,
        )

    async def _end_lunch_break(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        *,
        returned_at: datetime | None = None,
    ) -> None:
        lunch_ended_at = returned_at or now
        resume_stage = str(session.metadata.get("lunch_resume_stage") or "active")
        task_id = str(session.metadata.get("lunch_resume_task_id") or self._active_task_id(session) or "")
        task_name = str(
            session.metadata.get("lunch_resume_task_name")
            or session.metadata.get("active_clickup_task_name")
            or ""
        )
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        session.metadata["lunch_ended_at"] = lunch_ended_at.isoformat()
        self._record_lunch_window_end(session, lunch_ended_at)
        self._start_new_work_segment(session, lunch_ended_at)
        if returned_at is not None and returned_at != now:
            session.metadata["last_reported_lunch_return"] = {
                "returned_at": returned_at.isoformat(),
                "reported_at": now.isoformat(),
                "source": "intern_message",
            }
        session.last_follow_up_at = now.isoformat()
        session.metadata.pop("lunch_last_prompt_at", None)
        session.metadata.pop("lunch_resume_requested_at", None)
        clock_out_guidance = self._post_lunch_clock_out_guidance(user, session, now)
        if resume_stage == "awaiting_admin_review":
            session.stage = "awaiting_admin_review"
            await self._send_dm(
                client,
                user,
                session,
                "Welcome back. Your completed task is still waiting on admin review; I will message you when they respond.",
                now,
            )
            session.metadata.pop("lunch_resume_stage", None)
            session.metadata.pop("lunch_resume_task_id", None)
            session.metadata.pop("lunch_resume_task_name", None)
            return
        if resume_stage in {"awaiting_task_selection", "awaiting_plan", "awaiting_start_photo", "awaiting_risk"} and isinstance(prompt, dict):
            session.stage = resume_stage
            if resume_stage == "awaiting_start_photo":
                session.awaiting_start_photo = True
            tracking = await self._get_task_tracking_state(user, session)
            reminder = await self._task_onboarding_reminder(user, session, tracking, prompt)
            await self._send_dm(
                client,
                user,
                session,
                "\n\n".join(
                    part
                    for part in (
                        "Welcome back. Let's pick up where we left off.",
                        reminder,
                        clock_out_guidance,
                    )
                    if part
                ),
                now,
            )
            session.metadata.pop("lunch_resume_stage", None)
            session.metadata.pop("lunch_resume_task_id", None)
            session.metadata.pop("lunch_resume_task_name", None)
            return
        if task_id:
            session.stage = "active"
            session.awaiting_start_photo = False
            await self._start_task_timer(user, session, lunch_ended_at, task_id, task_name)
            label = task_name or task_id
            await self._send_dm(
                client,
                user,
                session,
                "\n\n".join(
                    part
                    for part in (
                        f"Welcome back. I resumed task tracking for `{label}`.",
                        clock_out_guidance,
                    )
                    if part
                ),
                now,
            )
            session.metadata.pop("lunch_resume_stage", None)
            session.metadata.pop("lunch_resume_task_id", None)
            session.metadata.pop("lunch_resume_task_name", None)
            return
        session.stage = "awaiting_task_selection"
        session.awaiting_start_photo = False
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_onboarding",
            "source": "lunch_resume",
            "step": "select_task",
            "reason": "Lunch break ended, but the active task needs to be re-confirmed before work resumes.",
            "draft": {},
        }
        session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
        tracking = await self._get_task_tracking_state(user, session)
        await self._send_dm(
            client,
            user,
            session,
            "\n\n".join(
                part
                for part in (
                    "Welcome back.",
                    await self._task_onboarding_intro(user, session, tracking),
                    clock_out_guidance,
                )
                if part
            ),
            now,
        )
        session.metadata.pop("lunch_resume_stage", None)
        session.metadata.pop("lunch_resume_task_id", None)
        session.metadata.pop("lunch_resume_task_name", None)

    async def _maybe_begin_clickup_work(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> None:
        if not self.config or not self.clickup:
            return
        task_id = self._active_task_id(session)
        if not task_id:
            messages = self.list_session_messages(user.user_key, session)
            bundle = await self._get_clickup_context(user, session, messages)
            self._remember_clickup_context(session, bundle)
            task_id = bundle.active_task_id
        if not task_id:
            return
        await self._activate_clickup_task(
            user,
            session,
            now,
            task_id,
            str(session.metadata.get("active_clickup_task_name") or ""),
        )

    async def _maybe_prompt_stuck_assistance(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
    ) -> None:
        if session.metadata.get(_CLICKUP_PROMPT_KEY):
            return
        self._clear_follow_up_probe_tracking(session)
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "blocker_resolution",
            "step": "offer_help",
            "origin_message_id": inbound.message_id,
            "blocker_text": inbound.content.strip() or session.latest_blocker or "",
            "help_decision": None,
            "blocked_state_after_decline": None,
        }
        await self._send_dm(
            client,
            user,
            session,
            self._blocker_offer_help_prompt(),
            now,
            view=self._blocker_resolution_view(user, "offer_help"),
        )

    def _stuck_assistance_prompt(self) -> str:
        return self._blocker_offer_help_prompt()

    def _blocker_offer_help_prompt(self) -> str:
        admin_names = self.admin_name_list_text()
        return (
            f"That sounds blocked. What do you want me to do about it?\n\n"
            f"- Ask admin for help ({admin_names})\n"
            "- Draft an unblocker task\n"
            "- No help needed\n"
            "- Not actually blocked\n\n"
            "Use the buttons, reply with an admin name, say `create task`, say `no help`, or say `not blocked`."
        )

    def _blocker_offer_help_clarification(self) -> str:
        return (
            "I am waiting on a blocker decision. Use the buttons, reply with an admin name, say `create task`, "
            "say `no help`, or say `not blocked`."
        )

    def _blocker_task_opt_in_prompt(self) -> str:
        return (
            "I can open a Mission Board blocker task for this. Reply `yes` if you want that, "
            "or `no` if you only want me to log the blocker locally."
        )

    def _blocker_choose_admin_prompt(self) -> str:
        admin_names = ", ".join(admin.name for admin in self.admin_profiles()) or "the configured admins"
        return (
            f"Which admin should I message about this blocker?\n\n"
            f"Use the buttons or reply with one of these names: {admin_names}."
        )

    def _blocker_declined_help_followup_prompt(self) -> str:
        return (
            "Got it. Do you want me to keep this blocker logged for visibility, or clear it?\n\n"
            "Use the buttons or reply with `keep logged` or `clear it`."
        )

    def _blocker_resolution_prompt_text(self, prompt: dict[str, Any]) -> str:
        step = str(prompt.get("step") or "offer_help")
        if step == "choose_admin":
            return self._blocker_choose_admin_prompt()
        if step == "declined_help_followup":
            return self._blocker_declined_help_followup_prompt()
        return self._blocker_offer_help_prompt()

    def _blocker_resolution_view(self, user: UserProfile, step: str) -> discord.ui.View | None:
        if step == "choose_admin" and not self.admin_profiles():
            return None
        return _InternBlockerResolutionView(self, user, step)

    async def _begin_unblocker_task_draft(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        stuck_prompt: dict[str, Any],
        now: datetime,
    ) -> None:
        blocker_text = str(stuck_prompt.get("blocker_text") or "")
        origin_message_id = stuck_prompt.get("origin_message_id")
        if not blocker_text:
            draft = stuck_prompt.get("draft")
            if isinstance(draft, dict):
                blocker_text = str(draft.get("blocker_text") or "")
                if origin_message_id is None:
                    origin_message_id = draft.get("origin_message_id")
        self._set_blocker_state(session, "blocked_help_requested", now)
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "unblocker_task_draft",
            "step": "title",
            "draft": {
                "blocker_text": blocker_text or session.latest_blocker or "",
                "origin_message_id": origin_message_id,
            },
            "return_prompt": deepcopy(stuck_prompt),
        }
        await self._send_dm(
            client,
            user,
            session,
            (
                "Okay. I will draft the unblocker task.\n\n"
                "If you assign it to yourself, I can create it immediately and switch you onto it. "
                "Otherwise I will send it to admin for review before it is created in ClickUp.\n\n"
                "Give me a short title for the task."
            ),
            now,
        )

    def _resolve_requested_admins(self, text: str) -> list[AdminProfile]:
        direct_matches = self.find_admin_profiles(text)
        if direct_matches:
            return direct_matches
        lowered = text.strip().lower()
        if self._looks_like_help_declined(lowered) or self._looks_like_not_blocked_reply(lowered):
            return []
        admins = self.admin_profiles()
        if not admins:
            return []
        if any(phrase in lowered for phrase in ("all admins", "everyone on admin", "any admin", "someone from admin")):
            return admins
        if len(admins) == 1 and (
            "admin" in lowered
            or "help" in lowered
            or self._is_affirmative_reply(text)
        ):
            return admins
        return []

    async def _notify_requested_admins(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        request_text: str,
        admins: list[AdminProfile],
        now: datetime,
    ) -> None:
        task_name = str(session.metadata.get("active_clickup_task_name") or "the current task")
        task_id = str(session.metadata.get("active_clickup_task_id") or "")
        message = (
            f"{user.display_name} asked for admin help on `{task_name}`"
            + (f" ({task_id})" if task_id else "")
            + ".\n\n"
            f"Intern request:\n{request_text.strip() or session.latest_blocker or 'No blocker text captured.'}\n\n"
            f"Latest blocker:\n{session.latest_blocker or 'No blocker text captured.'}"
        )
        await self._send_admin_notice(
            client,
            message,
            target_admins=admins,
            user=user,
            session=session,
        )
        session.metadata["last_direct_admin_help_request_at"] = now.isoformat()
        session.metadata["last_direct_admin_help_targets"] = [admin.name for admin in admins]
        self._set_blocker_state(session, "blocked_help_requested", now)

    async def handle_blocker_resolution_interaction(
        self,
        interaction: discord.Interaction,
        user_key: str,
        action: str,
    ) -> None:
        user = self.roster_by_key.get(user_key)
        if not user:
            if interaction.response.is_done():
                await interaction.followup.send("I could not find that user anymore. Send me a message if you still need help.")
            else:
                await interaction.response.edit_message(
                    content="I could not find that user anymore. Send me a message if you still need help.",
                    view=None,
                )
            return
        async with self._user_session_lock(user.user_key):
            session, now = self.get_user_session_for_moment(user, interaction.created_at)
            previous_session = self._clone_session_state(session)
            prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
            if not isinstance(prompt, dict) or str(prompt.get("type") or "") not in {"blocker_resolution", "stuck_assistance"}:
                if interaction.response.is_done():
                    await interaction.followup.send("That blocker prompt is no longer active.")
                else:
                    await interaction.response.edit_message(content="That blocker prompt is no longer active.", view=None)
                return
            self._record_interaction_inbound_message(
                user,
                session,
                interaction,
                content=action,
                now=now,
            )
            await self._handle_blocker_resolution_prompt(
                interaction.client,
                user,
                session,
                MessageRecord(
                    message_id=f"interaction:{interaction.id}",
                    direction="inbound",
                    author_id=interaction.user.id,
                    created_at=interaction.created_at,
                    content=action,
                    attachments=[],
                ),
                now,
                prompt,
                action=action,
            )
            session.pending_clickup_sync = True
            self._record_interaction_outbound_message(
                user,
                session,
                interaction,
                content="Recorded.",
                now=now,
            )
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="blocker_resolution_interaction",
                details={"action": action},
            )
            await self.write_dashboard()
        if interaction.response.is_done():
            await interaction.edit_original_response(content="Recorded.", view=None)
        else:
            await interaction.response.edit_message(content="Recorded.", view=None)

    async def handle_task_onboarding_confirmation_interaction(
        self,
        interaction: discord.Interaction,
        user_key: str,
        action: str,
    ) -> None:
        user = self.roster_by_key.get(user_key)
        if not user:
            if interaction.response.is_done():
                await interaction.followup.send("I could not find that user anymore. Send me a new message and I'll restart task selection.")
            else:
                await interaction.response.edit_message(
                    content="I could not find that user anymore. Send me a new message and I'll restart task selection.",
                    view=None,
                )
            return
        async with self._user_session_lock(user.user_key):
            session, now = self.get_user_session_for_moment(user, interaction.created_at)
            previous_session = self._clone_session_state(session)
            prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
            if not isinstance(prompt, dict) or str(prompt.get("type") or "") != "task_onboarding" or str(prompt.get("step") or "") != "confirm_task":
                if interaction.response.is_done():
                    await interaction.followup.send("That task confirmation is no longer active.")
                else:
                    await interaction.response.edit_message(content="That task confirmation is no longer active.", view=None)
                return
            content = "yes" if action == "confirm" else "no"
            self._record_interaction_inbound_message(
                user,
                session,
                interaction,
                content=content,
                now=now,
            )
            await self._handle_task_onboarding_prompt(
                interaction.client,
                user,
                session,
                MessageRecord(
                    message_id=f"interaction:{interaction.id}",
                    direction="inbound",
                    author_id=interaction.user.id,
                    created_at=interaction.created_at,
                    content=content,
                    attachments=[],
                ),
                now,
                prompt,
            )
            session.pending_clickup_sync = True
            self._record_interaction_outbound_message(
                user,
                session,
                interaction,
                content="Recorded.",
                now=now,
            )
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="task_onboarding_confirmation_interaction",
                details={"action": action},
            )
            await self.write_dashboard()
        if interaction.response.is_done():
            await interaction.edit_original_response(content="Recorded.", view=None)
        else:
            await interaction.response.edit_message(content="Recorded.", view=None)

    async def handle_queued_review_choice_interaction(
        self,
        interaction: discord.Interaction,
        user_key: str,
        action: str,
    ) -> None:
        user = self.roster_by_key.get(user_key)
        if not user:
            if interaction.response.is_done():
                await interaction.followup.send("I could not find that user anymore.")
            else:
                await interaction.response.edit_message(content="I could not find that user anymore.", view=None)
            return
        async with self._user_session_lock(user.user_key):
            session, now = self.get_user_session_for_moment(user, interaction.created_at)
            previous_session = self._clone_session_state(session)
            prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
            if not isinstance(prompt, dict) or str(prompt.get("type") or "") != "queued_review_rework_decision":
                if interaction.response.is_done():
                    await interaction.followup.send("That rework decision is no longer active.")
                else:
                    await interaction.response.edit_message(content="That rework decision is no longer active.", view=None)
                return
            self._record_interaction_inbound_message(
                user,
                session,
                interaction,
                content=action,
                now=now,
            )
            await self._handle_queued_review_rework_decision_prompt(
                interaction.client,
                user,
                session,
                MessageRecord(
                    message_id=f"interaction:{interaction.id}",
                    direction="inbound",
                    author_id=interaction.user.id,
                    created_at=interaction.created_at,
                    content=action,
                    attachments=[],
                ),
                now,
                prompt,
                action=action,
            )
            session.pending_clickup_sync = True
            self._record_interaction_outbound_message(
                user,
                session,
                interaction,
                content="Recorded.",
                now=now,
            )
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="queued_review_choice_interaction",
                details={"action": action},
            )
            await self.write_dashboard()
        if interaction.response.is_done():
            await interaction.edit_original_response(content="Recorded.", view=None)
        else:
            await interaction.response.edit_message(content="Recorded.", view=None)

    async def _edit_interaction_message(
        self,
        interaction: discord.Interaction,
        *,
        content: str,
        view: discord.ui.View | None = None,
    ) -> None:
        if interaction.response.is_done():
            edit_original = getattr(interaction, "edit_original_response", None)
            if callable(edit_original):
                await edit_original(content=content, view=view)
                return
            message = getattr(interaction, "message", None)
            edit_message = getattr(message, "edit", None)
            if callable(edit_message):
                await edit_message(content=content, view=view)
                return
            followup = getattr(interaction, "followup", None)
            send = getattr(followup, "send", None)
            if callable(send):
                await send(content)
                return
            return
        await interaction.response.edit_message(content=content, view=view)

    async def _defer_interaction_response(self, interaction: discord.Interaction) -> None:
        response = getattr(interaction, "response", None)
        if response is None or response.is_done():
            return
        defer = getattr(response, "defer", None)
        if callable(defer):
            await defer()

    async def _dismiss_interaction_message(
        self,
        interaction: discord.Interaction,
        *,
        fallback_content: str,
    ) -> None:
        message = getattr(interaction, "message", None)
        delete_message = getattr(message, "delete", None)
        if callable(delete_message):
            try:
                await delete_message()
            except Exception:
                pass
            else:
                if not interaction.response.is_done():
                    defer = getattr(interaction.response, "defer", None)
                    if callable(defer):
                        await defer()
                return
        await self._edit_interaction_message(
            interaction,
            content=fallback_content,
            view=None,
        )

    async def handle_self_lookup_interaction(
        self,
        interaction: discord.Interaction,
        user_key: str,
        session_date: str,
        action: str,
    ) -> None:
        await self._defer_interaction_response(interaction)
        user = self.roster_by_key.get(user_key)
        if not user:
            await self._edit_interaction_message(
                interaction,
                content="I could not find that user anymore.",
                view=None,
            )
            return
        async with self._user_session_lock(user.user_key):
            session = self.state_store.get_session(user.user_key, session_date)
            self._normalize_session_state(session, user=user)
            now = self.resolve_user_local_now(user, interaction.created_at)
            previous_session = self._clone_session_state(session)
            prompt = self._self_lookup_prompt(session)
            prompt_message_id = str((prompt or {}).get("message_id") or "")
            interaction_message_id = str(getattr(getattr(interaction, "message", None), "id", "") or "")
            if (
                not prompt
                or (
                    prompt_message_id
                    and interaction_message_id
                    and prompt_message_id != interaction_message_id
                )
            ):
                await self._edit_interaction_message(
                    interaction,
                    content="That info request is no longer active.",
                    view=None,
                )
                return
            self._record_interaction_inbound_message(
                user,
                session,
                interaction,
                content=action,
                now=now,
            )
            prompt_step = str(prompt.get("step") or "chooser")
            if action == "dismiss":
                self._clear_self_lookup_prompt(session)
                await self._persist_session_state(
                    user,
                    session,
                    now=now,
                    previous_session=previous_session,
                    trigger="self_lookup_interaction",
                    details={"action": action},
                )
                await self.write_dashboard()
                await self._dismiss_interaction_message(
                    interaction,
                    fallback_content="Okay, dismissed.",
                )
                return
            if prompt_step == "chooser" and action == "hours":
                reply = "What range of hours do you want to know?"
                prompt["step"] = "range"
                prompt["last_action_at"] = now.isoformat()
                if interaction_message_id:
                    prompt["message_id"] = interaction_message_id
                self._record_interaction_outbound_message(
                    user,
                    session,
                    interaction,
                    content=reply,
                    now=now,
                )
                await self._persist_session_state(
                    user,
                    session,
                    now=now,
                    previous_session=previous_session,
                    trigger="self_lookup_interaction",
                    details={"action": action},
                )
                await self.write_dashboard()
                await self._edit_interaction_message(
                    interaction,
                    content=reply,
                    view=self._self_lookup_hours_range_view(user, session.session_date),
                )
                return
            if prompt_step == "chooser" and action == "status":
                self._clear_self_lookup_prompt(session)
                reply = self._build_self_status_reply(user, session, now)
                self._record_interaction_outbound_message(
                    user,
                    session,
                    interaction,
                    content=reply,
                    now=now,
                )
                await self._persist_session_state(
                    user,
                    session,
                    now=now,
                    previous_session=previous_session,
                    trigger="self_lookup_interaction",
                    details={"action": action},
                )
                await self.write_dashboard()
                await self._edit_interaction_message(
                    interaction,
                    content=reply,
                    view=None,
                )
                return
            if prompt_step == "range" and action in _SELF_LOOKUP_RANGE_LABELS:
                self._clear_self_lookup_prompt(session)
                reply = await self._build_self_hours_reply_for_range(user, session, now, action)
                self._record_interaction_outbound_message(
                    user,
                    session,
                    interaction,
                    content=reply,
                    now=now,
                )
                await self._persist_session_state(
                    user,
                    session,
                    now=now,
                    previous_session=previous_session,
                    trigger="self_lookup_interaction",
                    details={"action": action},
                )
                await self.write_dashboard()
                await self._edit_interaction_message(
                    interaction,
                    content=reply,
                    view=None,
                )
                return
        await self._edit_interaction_message(
            interaction,
            content="That info request is no longer active.",
            view=None,
        )

    async def handle_day_suppression_interaction(
        self,
        interaction: discord.Interaction,
        user_key: str,
        session_date: str,
        action: str,
    ) -> None:
        user = self.roster_by_key.get(user_key)
        if not user:
            await self._edit_interaction_message(
                interaction,
                content="I could not find that user anymore.",
                view=None,
            )
            return
        async with self._user_session_lock(user.user_key):
            session = self.state_store.get_session(user.user_key, session_date)
            self._normalize_session_state(session, user=user)
            now = self.resolve_user_local_now(user, interaction.created_at)
            previous_session = self._clone_session_state(session)
            prompt = self._day_suppression_prompt(session)
            prompt_message_id = str((prompt or {}).get("message_id") or "")
            interaction_message_id = str(getattr(getattr(interaction, "message", None), "id", "") or "")
            if (
                not prompt
                or (
                    prompt_message_id
                    and interaction_message_id
                    and prompt_message_id != interaction_message_id
                )
            ):
                await self._edit_interaction_message(
                    interaction,
                    content="That schedule pause request is no longer active.",
                    view=None,
                )
                return
            self._record_interaction_inbound_message(
                user,
                session,
                interaction,
                content=action,
                now=now,
            )
            if action == "confirm":
                self._clear_day_suppression_prompt(session)
                session.metadata[_DAY_SUPPRESSION_STATE_KEY] = {
                    "session_date": session.session_date,
                    "confirmed_at": now.isoformat(),
                    "source_message_id": str(prompt.get("source_message_id") or ""),
                    "source_excerpt": str(prompt.get("source_excerpt") or ""),
                }
                reply = "Okay, I will stop reminders and check-ins for the rest of today."
                if session.clocked_in_at and not session.clocked_out_at:
                    reply += " If you still need to end your shift, tell me to clock out when you are ready."
                self._record_interaction_outbound_message(
                    user,
                    session,
                    interaction,
                    content=reply,
                    now=now,
                )
                await self._persist_session_state(
                    user,
                    session,
                    now=now,
                    previous_session=previous_session,
                    trigger="day_suppression_interaction",
                    details={"action": action},
                )
                await self.write_dashboard()
                await self._edit_interaction_message(
                    interaction,
                    content=reply,
                    view=None,
                )
                return
            if action == "cancel":
                self._clear_day_suppression_prompt(session)
                reply = "Okay, I will keep today's normal reminders on."
                self._record_interaction_outbound_message(
                    user,
                    session,
                    interaction,
                    content=reply,
                    now=now,
                )
                await self._persist_session_state(
                    user,
                    session,
                    now=now,
                    previous_session=previous_session,
                    trigger="day_suppression_interaction",
                    details={"action": action},
                )
                await self.write_dashboard()
                await self._edit_interaction_message(
                    interaction,
                    content=reply,
                    view=None,
                )
                return
        await self._edit_interaction_message(
            interaction,
            content="That schedule pause request is no longer active.",
            view=None,
        )

    async def handle_progress_probe_admin_interaction(
        self,
        interaction: discord.Interaction,
        user_key: str,
        session_date: str,
        probe_id: str,
    ) -> None:
        admin = self._admin_profile_by_discord_user_id(interaction.user.id)
        if not admin:
            if interaction.response.is_done():
                await interaction.followup.send("You are not configured as an admin for this bot.")
            else:
                await interaction.response.edit_message(
                    content="You are not configured as an admin for this bot.",
                    view=None,
                )
            return
        user = self.roster_by_key.get(user_key)
        if not user:
            if interaction.response.is_done():
                await interaction.followup.send("I could not find that user anymore.")
            else:
                await interaction.response.edit_message(content="I could not find that user anymore.", view=None)
            return
        async with self._user_session_lock(user.user_key):
            session = self.state_store.get_session(user.user_key, session_date)
            now = self.resolve_user_local_now(user, interaction.created_at)
            previous_session = self._clone_session_state(session)
            prompt = self._progress_probe_prompt(session)
            if prompt and str(prompt.get("probe_id") or "") == probe_id:
                subscribed = prompt.setdefault("subscribed_admin_ids", [])
                if admin.discord_user_id not in subscribed:
                    subscribed.append(admin.discord_user_id)
                prompt["last_activity_at"] = now.isoformat()
                session.pending_clickup_sync = True
                await self._persist_session_state(
                    user,
                    session,
                    now=now,
                    previous_session=previous_session,
                    trigger="progress_probe_admin_subscription",
                    details={"admin": admin.name, "probe_id": probe_id},
                )
                await self.write_dashboard()
                await self._send_progress_probe_exchange_to_admin(
                    interaction.client,
                    admin,
                    user,
                    session,
                    prompt,
                    include_closure=False,
                )
                if interaction.response.is_done():
                    await interaction.edit_original_response(
                        content="Subscribed. I will forward later probe replies here.",
                        view=None,
                    )
                else:
                    await interaction.response.edit_message(
                        content="Subscribed. I will forward later probe replies here.",
                        view=None,
                    )
                return
            for exchange in reversed(self._progress_probe_history(session)):
                if str(exchange.get("probe_id") or "") != probe_id:
                    continue
                await self._send_progress_probe_exchange_to_admin(
                    interaction.client,
                    admin,
                    user,
                    session,
                    exchange,
                    include_closure=True,
                )
                if interaction.response.is_done():
                    await interaction.edit_original_response(
                        content="That probe is already closed. I sent you the recorded exchange.",
                        view=None,
                    )
                else:
                    await interaction.response.edit_message(
                        content="That probe is already closed. I sent you the recorded exchange.",
                        view=None,
                    )
                return
        if interaction.response.is_done():
            await interaction.followup.send("That progress probe is no longer available.")
        else:
            await interaction.response.edit_message(content="That progress probe is no longer available.", view=None)

    async def _close_progress_probe(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        *,
        reason: str,
    ) -> None:
        prompt = self._progress_probe_prompt(session)
        if not prompt:
            self._clear_follow_up_probe_tracking(session)
            return
        prompt["closure_reason"] = reason
        exchange = {
            "probe_id": str(prompt.get("probe_id") or ""),
            "question_text": str(prompt.get("question_text") or ""),
            "original_reply_message_ids": list(prompt.get("original_reply_message_ids") or []),
            "original_reply_text": str(prompt.get("original_reply_text") or ""),
            "probe_exchange": deepcopy(prompt.get("probe_exchange") or []),
            "closure_reason": reason,
            "closed_at": now.isoformat(),
        }
        history = self._progress_probe_history(session)
        history.append(exchange)
        session.metadata[_PROGRESS_PROBE_HISTORY_KEY] = history[-5:]
        session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
        self._clear_follow_up_probe_tracking(session)
        subscribed_admins = self._progress_probe_subscribed_admins(prompt)
        if subscribed_admins:
            await self._send_admin_notice(
                client,
                self._progress_probe_closure_message(reason),
                target_admins=subscribed_admins,
                user=user,
                session=session,
            )

    def _resolve_blocker_prompt_action(self, text: str, prompt: dict[str, Any]) -> str | None:
        lowered = " ".join(text.strip().lower().split())
        step = str(prompt.get("step") or "offer_help")
        if not lowered:
            return None
        if self._looks_like_not_blocked_reply(lowered):
            return "not_blocked"
        if step == "declined_help_followup":
            if self._looks_like_keep_blocker_logged(lowered):
                return "keep_logged"
            if self._looks_like_clear_blocker(lowered):
                return "clear_it"
            return None
        if self._looks_like_unblocker_task_request(text):
            return "draft_task"
        if self._looks_like_help_declined(lowered) or self._is_negative_reply(text):
            return "no_help_needed"
        if self._resolve_requested_admins(text):
            return "ask_admin" if step == "offer_help" else text
        if self._looks_like_help_request(lowered):
            return "ask_admin"
        return None

    async def _resolve_unblocker_assignee(
        self,
        requester: UserProfile,
        text: str,
    ) -> tuple[str, str | None, str | None, bool]:
        lowered = text.strip().lower()
        if lowered in {"unassigned", "none", "no one", "admin decides"}:
            return "unassigned", None, None, False
        normalized = self._normalize_identifier_value(text)
        requester_candidates = {
            self._normalize_identifier_value(requester.user_key),
            self._normalize_identifier_value(requester.display_name),
            self._normalize_identifier_value(requester.discord_username),
            "me",
            "myself",
            "assignittome",
            "illdoit",
            "iwilldoit",
        }
        if normalized in requester_candidates:
            assignee_id = await self.clickup.resolve_clickup_user_id(requester) if self.clickup else None
            note = (
                None
                if assignee_id
                else "I could not resolve your ClickUp assignee ID, so I will send this draft to admin instead of creating it directly."
            )
            return requester.display_name, assignee_id, note, True
        for roster_user in self.roster_by_key.values():
            candidates = {
                self._normalize_identifier_value(roster_user.user_key),
                self._normalize_identifier_value(roster_user.display_name),
                self._normalize_identifier_value(roster_user.discord_username),
            }
            if normalized in candidates:
                assignee_id = await self.clickup.resolve_clickup_user_id(roster_user) if self.clickup else None
                if roster_user.user_key == requester.user_key:
                    note = (
                        None
                        if assignee_id
                        else "I could not resolve your ClickUp assignee ID, so I will send this draft to admin instead of creating it directly."
                    )
                    return roster_user.display_name, assignee_id, note, True
                note = None if assignee_id else f"I could not resolve {roster_user.display_name} to a ClickUp assignee, so the task may stay unassigned until admin adjusts it."
                return roster_user.display_name, assignee_id, note, False
        for admin in self.find_admin_profiles(text):
            resolved_admin_id = admin.clickup_user_id
            note = None
            member_lookup = getattr(self.clickup, "resolve_workspace_member_id", None) if self.clickup else None
            if not resolved_admin_id and callable(member_lookup):
                resolved_admin_id = await member_lookup(
                    name=admin.name,
                    email=admin.clickup_user_email,
                )
            if not resolved_admin_id:
                note = f"I can show {admin.name} as the requested owner, but they are not mapped to a ClickUp assignee in config, so the task will be created unassigned unless admin changes it."
            return admin.name, resolved_admin_id, note, False
        return (
            text.strip(),
            None,
            "I could not map that person to a ClickUp assignee, so I will show the requested owner to admin and leave the task unassigned unless they change it.",
            False,
        )

    async def _submit_unblocker_task_for_admin_review(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        draft: dict[str, Any],
        now: datetime,
        *,
        user_message: str | None = None,
    ) -> None:
        proposal = {
            "submitted_at": now.isoformat(),
            "draft": draft,
            "active_task_id": self._active_task_id(session),
            "active_task_name": str(session.metadata.get("active_clickup_task_name") or ""),
        }
        session.metadata["pending_admin_unblocker_task"] = proposal
        await self._send_dm(
            client,
            user,
            session,
            user_message or "Got it. I sent the unblocker-task draft to admin for review. I will only create it in ClickUp after approval.",
            now,
        )
        await self._send_admin_unblocker_task_request(client, user, session, now)

    def _self_assigned_unblocker_fallback_reason(self, draft: dict[str, Any]) -> str | None:
        if self.config and self.config.clickup.new_task_approval_required:
            return "New ClickUp tasks require Erik or George approval."
        if not self.clickup or not self.config:
            return "I could not create that task directly because ClickUp is unavailable."
        if not self.config.clickup.mission_board_list_id:
            return "I could not create that task directly because Mission Board is not configured here."
        if not draft.get("assignee_id"):
            return "I could not create that task directly because I could not resolve your ClickUp assignee ID."
        return None

    def _remember_created_blocker_task_id(self, session: SessionState, created_id: str) -> None:
        if not created_id:
            return
        created_ids = [
            task_id
            for task_id in session.metadata.get("created_blocker_task_ids", [])
            if isinstance(task_id, str)
        ]
        if created_id not in created_ids:
            created_ids.append(created_id)
        session.metadata["created_blocker_task_ids"] = created_ids

    async def _send_self_assigned_unblocker_admin_notice(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        *,
        created_task_id: str,
        created_task_name: str,
        blocked_task_id: str | None,
        blocked_task_name: str | None,
        blocker_text: str,
        draft: dict[str, Any],
    ) -> None:
        blocked_label = blocked_task_name or blocked_task_id or "unresolved"
        message = (
            f"{user.display_name} created a self-assigned unblocker task and switched onto it.\n\n"
            f"Blocked task: `{blocked_label}`"
            + (f" ({blocked_task_id})" if blocked_task_id and blocked_task_name else "")
            + "\n"
            f"Created task: `{created_task_name}`"
            + (f" ({created_task_id})" if created_task_id else "")
            + "\n\n"
            f"{self._format_unblocker_task_preview(draft)}"
        )
        if blocker_text:
            message += f"\n\nBlocking context:\n{blocker_text}"
        await self._send_admin_notice(client, message, user=user, session=session)

    async def _create_self_assigned_unblocker_task_and_switch(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        draft: dict[str, Any],
        now: datetime,
    ) -> None:
        if not self.config or not self.clickup or not self.config.clickup.mission_board_list_id:
            raise RuntimeError("Self-assigned unblocker creation requires ClickUp Mission Board configuration.")
        blocked_task_id = self._active_task_id(session)
        blocked_task_name = str(session.metadata.get("active_clickup_task_name") or "")
        blocker_text = str(draft.get("blocker_text") or session.latest_blocker or "").strip()
        created = await self.clickup.create_task(
            self.config.clickup.mission_board_list_id,
            name=str(draft.get("title") or "Unspecified unblocker task"),
            description=self._build_unblocker_task_description(user, session, draft),
            assignee_ids=[str(draft["assignee_id"])],
            priority=str(draft.get("priority") or "normal"),
            due_date=int(draft["due_date_ms"]) if "due_date_ms" in draft else None,
            tags=["unblocker", user.user_key.lower()],
        )
        created_task_id = str(created.get("id") or "")
        created_task_name = str(created.get("name") or draft.get("title") or "the task")
        self._remember_created_blocker_task_id(session, created_task_id)
        pause_note = await self._pause_current_task_tracking(
            user,
            session,
            now,
            set_hold=True,
            end_reason="self_assigned_unblocker_switch",
        )
        self._clear_blocker_state(session, mark_not_blocked=True)
        self._set_blocker_state(session, "not_blocked", now)
        if blocker_text:
            session.metadata["last_resolved_blocker"] = blocker_text
        session.metadata["last_unblocked_at"] = now.isoformat()
        session.metadata["last_unblocked_source"] = "self_assigned_unblocker_task"
        self._clear_active_task_metadata(session)
        session.awaiting_start_photo = False
        session.awaiting_clock_out_photo = False
        session.awaiting_clock_out_summary = False
        session.latest_plan = None
        session.latest_feedback = None
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_onboarding",
            "source": "self_assigned_unblocker_task",
            "step": "plan",
            "task_id": created_task_id,
            "task_name": created_task_name,
            "reason": "Created a self-assigned unblocker task to move past the blocker.",
            "draft": {},
        }
        session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
        session.stage = "awaiting_plan"
        await self._send_self_assigned_unblocker_admin_notice(
            client,
            user,
            session,
            now,
            created_task_id=created_task_id,
            created_task_name=created_task_name,
            blocked_task_id=blocked_task_id,
            blocked_task_name=blocked_task_name or None,
            blocker_text=blocker_text,
            draft=draft,
        )
        message_parts = [
            f"Done. I created `{created_task_name}` as your unblocker task and switched you onto it.",
        ]
        if pause_note:
            message_parts.append(pause_note)
        message_parts.append(
            self._task_onboarding_question(session.metadata[_CLICKUP_PROMPT_KEY], "plan")
        )
        await self._send_dm(
            client,
            user,
            session,
            "\n\n".join(message_parts),
            now,
        )

    async def _maybe_prompt_blocker_task(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
    ) -> None:
        if not self.config or not self.clickup or not self.config.clickup.mission_board_list_id:
            return
        if session.metadata.get(_CLICKUP_PROMPT_KEY):
            return
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "blocker_task",
            "step": "opt_in",
            "draft": {
                "origin_message_id": inbound.message_id,
            },
        }
        await self._send_dm(
            client,
            user,
            session,
            self._blocker_task_opt_in_prompt(),
            now,
        )

    async def _create_blocker_task_from_prompt(
        self,
        user: UserProfile,
        session: SessionState,
        draft: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.config or not self.clickup or not self.config.clickup.mission_board_list_id:
            return None
        description = self._build_blocker_task_description(user, session, draft)
        created = await self.clickup.create_task(
            self.config.clickup.mission_board_list_id,
            name=str(draft.get("title") or "Unspecified blocker"),
            description=description,
            priority=str(draft.get("priority") or "normal"),
            due_date=int(draft["due_date_ms"]) if "due_date_ms" in draft else None,
            tags=["blocker", user.user_key.lower()],
        )
        created_id = str(created.get("id") or "")
        self._remember_created_blocker_task_id(session, created_id)
        active_task_id = self._active_task_id(session)
        if active_task_id:
            await self._safe_set_task_state(session, active_task_id, "hold")
        return created

    async def _initiate_admin_review(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
        *,
        review_summary: str | None = None,
        completion_photo_paths: list[str] | None = None,
    ) -> None:
        task_id = self._active_task_id(session)
        task_name = str(session.metadata.get("active_clickup_task_name") or "the active task")
        summary = (review_summary or inbound.content.strip() or session.latest_status or "").strip()
        photo_paths = [
            path for path in (completion_photo_paths or self._message_attachment_paths(inbound))
            if isinstance(path, str) and path
        ]
        if not photo_paths:
            await self._send_dm(
                client,
                user,
                session,
                "I still need a completion picture before I can send this task to admin for review.",
                now,
            )
            session.metadata[_CLICKUP_PROMPT_KEY] = {
                "type": "task_review_submission",
                "summary": summary,
                "needs_summary": not bool(summary),
                "needs_photo": True,
                "photo_paths": [],
                "task_id": task_id,
                "task_name": task_name,
                "requested_at": now.isoformat(),
            }
            return
        pause_note = await self._pause_current_task_tracking(
            user,
            session,
            now,
            set_hold=True,
            end_reason="awaiting_admin_review",
        )
        reviews = self._pending_admin_reviews(session)
        review_entry = {
            "review_id": f"{task_id or 'task'}:{int(now.timestamp() * 1000)}",
            "task_id": task_id,
            "task_name": task_name,
            "submitted_at": now.isoformat(),
            "completion_message_id": inbound.message_id,
            "completion_summary": summary,
            "completion_photo_paths": photo_paths,
            "pause_note": pause_note,
        }
        reviews = [
            review for review in reviews
            if str(review.get("task_id") or "") != str(task_id or "")
        ]
        reviews.append(review_entry)
        self._set_pending_admin_reviews(session, reviews)
        self._clear_active_task_metadata(session)
        session.stage = "awaiting_admin_review"
        if self.clickup and task_id:
            await self.clickup.comment_on_task(
                task_id,
                f"Intern reported this task ready for admin review. Summary: {summary}",
            )
        await self._send_dm(
            client,
            user,
            session,
            (
                f"Got it. I marked `{task_name}` as waiting on admin review and paused the task tracking.\n\n"
                "I will message you when admin either closes it or sends back comments."
            ),
            now,
        )
        await self._send_admin_review_request(client, user, session, now, review=review_entry)

    async def _send_admin_review_request(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        *,
        review: dict[str, Any] | None = None,
    ) -> None:
        if not self.config:
            return
        review = review or self._pending_admin_review(session) or {}
        task_name = str(review.get("task_name") or session.metadata.get("active_clickup_task_name") or "the task")
        task_id = str(review.get("task_id") or self._active_task_id(session) or "")
        summary = str(review.get("completion_summary") or session.latest_status or "No completion summary captured.")
        photo_paths = self._resolve_existing_paths(review.get("completion_photo_paths"))
        close_command = f"run review.close user={user.user_key}"
        rework_command = f'run review.rework user={user.user_key} comments="..."'
        if task_id:
            close_command += f" task_id={task_id}"
            rework_command = f'run review.rework user={user.user_key} task_id={task_id} comments="..."'
        message = (
            f"{user.display_name} says they finished `{task_name}`"
            + (f" ({task_id})" if task_id else "")
            + ".\n\n"
            f"Intern summary:\n{summary}\n\n"
            f"Use `{close_command}` to close it.\n"
            f"Or use `{rework_command}` if they need to keep working on it."
        )
        await self._send_admin_notice(
            client,
            message,
            files=photo_paths[:10],
            user=user,
            session=session,
        )

    async def _send_admin_unblocker_task_request(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> None:
        proposal = self._pending_admin_unblocker_task(session) or {}
        draft = proposal.get("draft") if isinstance(proposal, dict) else {}
        if not isinstance(draft, dict):
            return
        task_name = str(proposal.get("active_task_name") or session.metadata.get("active_clickup_task_name") or "the active task")
        message = (
            f"{user.display_name} wants to create an unblocker task before they can continue on `{task_name}`.\n\n"
            f"{self._format_unblocker_task_preview(draft)}\n\n"
            "Use `run review.unblocker_approve user="
            f"{user.user_key}` to publish it to ClickUp.\n"
            "Or use `run review.unblocker_revise user="
            f"{user.user_key} comments=\"...\"` if you want the intern to revise the draft before it is created."
        )
        await self._send_admin_notice(
            client,
            message,
            target_admins=self._task_approval_admins(),
            user=user,
            session=session,
        )

    async def resume_user_after_unblock(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        *,
        now: datetime | None = None,
        source: str,
        actor_note: str | None = None,
        notify_user: bool,
    ) -> str:
        now = now or self.resolve_user_local_now(user)
        previous_session = self._clone_session_state(session)
        self._clear_follow_up_probe_tracking(session)
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") in {"stuck_assistance", "blocker_resolution", "unblocker_task_draft"}:
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
        pending_unblocker = self._pending_admin_unblocker_task(session)
        cancelled_admin_review = False
        if pending_unblocker:
            session.metadata.pop("pending_admin_unblocker_task", None)
            session.metadata["cancelled_unblocker_task_after_recovery_at"] = now.isoformat()
            cancelled_admin_review = True
            if source != "admin":
                await self._send_admin_notice(
                    client,
                    (
                        f"{user.display_name} says they are unblocked now, so I cancelled the pending unblocker-task draft"
                        " before it was created in ClickUp."
                    ),
                    user=user,
                    session=session,
                )
        previous_blocker = session.latest_blocker
        session.stuck_since = None
        session.stuck_alerted_at = None
        session.latest_blocker = None
        self._set_blocker_state(session, "not_blocked", now)
        session.stage = "active"
        session.metadata["last_unblocked_at"] = now.isoformat()
        session.metadata["last_unblocked_source"] = source
        if actor_note:
            session.metadata["last_unblocked_note"] = actor_note
        if previous_blocker:
            session.metadata["last_resolved_blocker"] = previous_blocker
        task_id = self._active_task_id(session)
        task_name = str(session.metadata.get("active_clickup_task_name") or "").strip()
        if task_id:
            activation = await self._activate_clickup_task(user, session, now, task_id, task_name)
            session.last_follow_up_at = now.isoformat()
            if notify_user:
                notice = self._task_activation_notice(activation, resumed=True)
                if cancelled_admin_review:
                    notice += "\n\nI also cancelled the pending unblocker-task draft because you no longer need it."
                notice += "\n\nKeep moving, and tell me right away if anything blocks you again."
                await self._send_dm(client, user, session, notice, now)
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="resume_after_unblock",
                details={
                    "source": source,
                    "actor_note_excerpt": self._excerpt_text(actor_note or ""),
                    "task_id": task_id,
                    "task_name": task_name,
                    "cancelled_pending_unblocker_task": cancelled_admin_review,
                },
            )
            await self.write_dashboard()
            return self._task_activation_notice(activation, resumed=True)
        await self._maybe_begin_clickup_work(user, session, now)
        task_id = self._active_task_id(session)
        task_name = str(session.metadata.get("active_clickup_task_name") or "").strip()
        if task_id:
            activation = TaskActivationResult(
                task_id=task_id,
                task_name=task_name or None,
                clickup_status_name=None,
                tracking_state=await self._get_task_tracking_state(user, session),
            )
            session.last_follow_up_at = now.isoformat()
            if notify_user:
                notice = self._task_activation_notice(activation, resumed=True)
                if cancelled_admin_review:
                    notice += "\n\nI also cancelled the pending unblocker-task draft because you no longer need it."
                notice += "\n\nKeep moving, and tell me right away if anything blocks you again."
                await self._send_dm(client, user, session, notice, now)
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="resume_after_unblock",
                details={
                    "source": source,
                    "actor_note_excerpt": self._excerpt_text(actor_note or ""),
                    "task_id": task_id,
                    "task_name": task_name,
                    "cancelled_pending_unblocker_task": cancelled_admin_review,
                },
            )
            await self.write_dashboard()
            return self._task_activation_notice(activation, resumed=True)
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_onboarding",
            "source": "resume_after_unblock",
            "step": "select_task",
            "reason": "The blocker cleared, but the active task needs to be reconfirmed before work resumes.",
            "draft": {},
        }
        session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
        if notify_user:
            message = "Glad you're unblocked. I cleared the blocker."
            if cancelled_admin_review:
                message += " I also cancelled the pending unblocker-task draft."
            message += "\n\nNow tell me the task name or task ID you are back on so I can resume tracking."
            await self._send_dm(client, user, session, message, now)
        await self._persist_session_state(
            user,
            session,
            now=now,
            previous_session=previous_session,
            trigger="resume_after_unblock",
            details={
                "source": source,
                "actor_note_excerpt": self._excerpt_text(actor_note or ""),
                "task_reconfirmation_required": True,
                "cancelled_pending_unblocker_task": cancelled_admin_review,
            },
        )
        await self.write_dashboard()
        return "The blocker is cleared, but I still need the active task re-confirmed before I can resume it."

    async def resolve_admin_task_proposal(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        *,
        approve_create: bool,
        admin_message: str,
        now: datetime | None = None,
    ) -> str:
        await self.refresh_configuration()
        if not self.config or not self.clickup:
            return "ClickUp is not configured."
        proposal = self._pending_admin_task_proposal(session)
        if not proposal:
            return f"{user.display_name} does not have a new-task proposal waiting on approval."
        draft = proposal.get("draft")
        if not isinstance(draft, dict):
            return f"{user.display_name} does not have a valid new-task proposal saved."
        previous_session = self._clone_session_state(session)
        now = now or self.resolve_user_local_now(user)
        if approve_create:
            list_id = str(draft.get("list_id") or "").strip()
            if not list_id:
                return "The proposal is missing its ClickUp list placement. Request a revision instead."
            created = await self.clickup.create_task(
                list_id,
                name=str(draft.get("title") or "Untitled task"),
                description=str(draft.get("description") or ""),
                assignee_ids=[str(draft["assignee_id"])] if draft.get("assignee_id") else None,
                parent_task_id=str(draft.get("parent_task_id") or "").strip() or None,
            )
            created_id = str(created.get("id") or "")
            created_name = str(created.get("name") or draft.get("title") or "the new task")
            session.metadata.pop("pending_admin_task_proposal", None)
            session.metadata["last_admin_task_proposal_resolution"] = {
                "decision": "created",
                "at": now.isoformat(),
                "message": admin_message,
                "created_task_id": created_id,
                "category": str(proposal.get("category") or "project"),
            }
            session.metadata[_CLICKUP_PROMPT_KEY] = {
                "type": "task_onboarding",
                "source": str(proposal.get("source") or "task_onboarding"),
                "step": "plan",
                "task_id": created_id,
                "task_name": created_name,
                "reason": str(proposal.get("reason") or "Approved new task proposal."),
                "draft": {},
            }
            session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
            session.stage = "awaiting_plan"
            session.latest_plan = None
            session.latest_feedback = None
            await self._send_dm(
                client,
                user,
                session,
                (
                    f"Erik or George approved your {proposal.get('category') or 'project'} task proposal. "
                    f"I created `{created_name}` in ClickUp and assigned it to you.\n\n"
                    + self._task_onboarding_question(
                        session.metadata[_CLICKUP_PROMPT_KEY],
                        "plan",
                    )
                ),
                now,
            )
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="admin_task_proposal_resolution",
                details={
                    "decision": "create",
                    "created_task_id": created_id,
                    "created_task_name": created_name,
                    "category": str(proposal.get("category") or "project"),
                    "admin_message_excerpt": self._excerpt_text(admin_message),
                },
            )
            await self.write_dashboard()
            return f"Created `{created_name}` for {user.display_name}."
        original_prompt = proposal.get("original_prompt")
        restored = deepcopy(original_prompt) if isinstance(original_prompt, dict) else {
            "type": "task_creation",
            "source": str(proposal.get("source") or "task_onboarding"),
            "draft": deepcopy(draft),
        }
        restored["type"] = "task_creation"
        restored["step"] = "description"
        restored["draft"] = deepcopy(draft)
        session.metadata.pop("pending_admin_task_proposal", None)
        session.metadata["last_admin_task_proposal_resolution"] = {
            "decision": "revise",
            "at": now.isoformat(),
            "message": admin_message,
        }
        session.metadata[_CLICKUP_PROMPT_KEY] = restored
        session.stage = "awaiting_task_selection"
        await self._send_dm(
            client,
            user,
            session,
            (
                "Erik or George requested a revision before this task can be created.\n\n"
                f"Feedback:\n{admin_message}\n\n"
                "Reply with a revised description, or reply `back` to change the title."
            ),
            now,
        )
        await self._persist_session_state(
            user,
            session,
            now=now,
            previous_session=previous_session,
            trigger="admin_task_proposal_resolution",
            details={
                "decision": "revise",
                "category": str(proposal.get("category") or "project"),
                "admin_message_excerpt": self._excerpt_text(admin_message),
            },
        )
        await self.write_dashboard()
        return f"Sent task-proposal revision feedback to {user.display_name}."

    async def resolve_admin_unblocker_task(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        *,
        approve_create: bool,
        admin_message: str,
        now: datetime | None = None,
    ) -> str:
        await self.refresh_configuration()
        if not self.config or not self.clickup or not self.config.clickup.mission_board_list_id:
            return "ClickUp Mission Board is not configured."
        proposal = self._pending_admin_unblocker_task(session)
        if not proposal:
            return f"{user.display_name} does not have an unblocker task draft waiting on admin review."
        draft = proposal.get("draft")
        if not isinstance(draft, dict):
            return f"{user.display_name} does not have a valid unblocker task draft saved."
        previous_session = self._clone_session_state(session)
        now = now or self.resolve_user_local_now(user)
        if approve_create:
            created = await self.clickup.create_task(
                self.config.clickup.mission_board_list_id,
                name=str(draft.get("title") or "Unspecified unblocker task"),
                description=self._build_unblocker_task_description(user, session, draft),
                assignee_ids=[str(draft["assignee_id"])] if draft.get("assignee_id") else None,
                priority=str(draft.get("priority") or "normal"),
                due_date=int(draft["due_date_ms"]) if "due_date_ms" in draft else None,
                tags=["unblocker", user.user_key.lower()],
            )
            created_id = str(created.get("id") or "")
            created_name = str(created.get("name") or draft.get("title") or "the task")
            self._remember_created_blocker_task_id(session, created_id)
            if self._active_task_id(session):
                await self._safe_set_task_state(session, self._active_task_id(session), "hold")
            session.metadata.pop("pending_admin_unblocker_task", None)
            session.metadata["last_admin_unblocker_resolution"] = {
                "decision": "created",
                "at": now.isoformat(),
                "message": admin_message,
                "created_task_id": created_id,
            }
            await self._send_dm(
                client,
                user,
                session,
                f"Admin approved the unblocker task, so I created `{created_name}` in ClickUp.",
                now,
            )
            await self._persist_session_state(
                user,
                session,
                now=now,
                previous_session=previous_session,
                trigger="admin_unblocker_resolution",
                details={
                    "decision": "create",
                    "created_task_id": created_id,
                    "created_task_name": created_name,
                    "admin_message_excerpt": self._excerpt_text(admin_message),
                },
            )
            await self.write_dashboard()
            return f"Created `{created_name}` for {user.display_name}."
        session.metadata["last_admin_unblocker_resolution"] = {
            "decision": "revise",
            "at": now.isoformat(),
            "message": admin_message,
        }
        session.metadata.pop("pending_admin_unblocker_task", None)
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "unblocker_task_draft",
            "step": "title",
            "draft": draft,
            "revision_feedback": admin_message,
        }
        await self._send_dm(
            client,
            user,
            session,
            (
                "Admin wants a revision before I create that unblocker task.\n\n"
                f"Feedback:\n{admin_message}\n\n"
                "Send me a revised short title to start the draft again."
            ),
            now,
        )
        await self._persist_session_state(
            user,
            session,
            now=now,
            previous_session=previous_session,
            trigger="admin_unblocker_resolution",
            details={
                "decision": "revise",
                "admin_message_excerpt": self._excerpt_text(admin_message),
            },
        )
        await self.write_dashboard()
        return f"Sent unblocker-task revision feedback back to {user.display_name}."

    def _build_blocker_task_description(
        self,
        user: UserProfile,
        session: SessionState,
        draft: dict[str, Any],
    ) -> str:
        lines = [
            f"Intern: {user.display_name}",
            f"Discord user: {user.discord_username or user.user_key}",
            f"Blocked task: {session.metadata.get('active_clickup_task_name') or 'unresolved'}",
            f"Blocked task id: {self._active_task_id(session) or 'unresolved'}",
            "",
            "What is blocked:",
            str(draft.get("description") or session.latest_blocker or "No blocker description provided."),
            "",
            "Exact help needed:",
            str(draft.get("help_needed") or "Not provided."),
        ]
        if session.latest_plan:
            lines.extend(["", "Today's plan:", session.latest_plan])
        if session.latest_status:
            lines.extend(["", "Most recent status update:", session.latest_status])
        return "\n".join(lines)

    def _build_unblocker_task_description(
        self,
        user: UserProfile,
        session: SessionState,
        draft: dict[str, Any],
    ) -> str:
        lines = [
            f"Requested by: {user.display_name}",
            f"Current blocked task: {session.metadata.get('active_clickup_task_name') or 'unresolved'}",
            f"Current blocked task id: {self._active_task_id(session) or 'unresolved'}",
            f"Requested owner: {draft.get('assignee_label') or 'unassigned'}",
        ]
        if draft.get("assignee_note"):
            lines.append(f"Owner resolution note: {draft['assignee_note']}")
        lines.extend(
            [
                "",
                "What should be done:",
                str(draft.get("description") or "No description provided."),
            ]
        )
        blocker_text = str(draft.get("blocker_text") or session.latest_blocker or "").strip()
        if blocker_text:
            lines.extend(["", "Blocking context:", blocker_text])
        if session.latest_plan:
            lines.extend(["", "Intern's current plan:", session.latest_plan])
        if session.latest_status:
            lines.extend(["", "Intern's latest status:", session.latest_status])
        return "\n".join(lines)

    def _format_unblocker_task_preview(self, draft: dict[str, Any]) -> str:
        due_text = str(draft.get("due_date_text") or "none")
        lines = [
            "Draft task preview:",
            f"- Title: {draft.get('title') or 'missing'}",
            f"- Requested owner: {draft.get('assignee_label') or 'unassigned'}",
            f"- Priority: {draft.get('priority') or 'normal'}",
            f"- Due date: {due_text}",
            "",
            "Description:",
            str(draft.get("description") or "No description provided."),
        ]
        if draft.get("assignee_note"):
            lines.extend(["", f"Owner note: {draft['assignee_note']}"])
        return "\n".join(lines)

    async def _safe_set_task_state(
        self,
        session: SessionState,
        task_id: str | None,
        state: str,
    ) -> str | None:
        if not self.clickup or not self.config or not self.config.clickup.auto_status_updates or not task_id:
            return None
        status_name = await self.clickup.set_task_state(task_id, state)
        if status_name:
            session.metadata["last_clickup_status"] = status_name
        return status_name

    async def _maybe_prompt_task_onboarding(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if not self.config:
            return False
        if not session.clocked_in_at or session.clocked_out_at:
            return False
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if (
            isinstance(prompt, dict)
            and str(prompt.get("type") or "") == "task_onboarding"
            and str(prompt.get("step") or "") == "photo"
            and session.stage == "awaiting_start_photo"
        ):
            last_prompt_at = self._metadata_datetime(
                session,
                "last_task_onboarding_prompt_at",
                reference=now,
                timezone_name=self.resolve_user_timezone_name(user),
            )
            if last_prompt_at and now - last_prompt_at < timedelta(minutes=self.config.schedule.task_onboarding_interval_minutes):
                return False
            session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
            await self._send_dm(
                client,
                user,
                session,
                self._task_onboarding_missing_text(prompt, "photo"),
                now,
            )
            return True
        if not self.clickup:
            return False
        if session.stage != "active":
            return False
        tracking = await self._get_task_tracking_state(user, session)
        self._reconcile_active_task_with_tracking(session, tracking)
        if tracking["active_task_id"] and tracking["timer_running"]:
            return False
        if isinstance(prompt, dict) and str(prompt.get("type") or "") == "task_onboarding":
            last_prompt_at = self._metadata_datetime(
                session,
                "last_task_onboarding_prompt_at",
                reference=now,
                timezone_name=self.resolve_user_timezone_name(user),
            )
            if last_prompt_at and now - last_prompt_at < timedelta(minutes=self.config.schedule.task_onboarding_interval_minutes):
                return False
            session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
            await self._send_dm(
                client,
                user,
                session,
                await self._task_onboarding_reminder(user, session, tracking, prompt),
                now,
            )
            return True
        last_prompt_at = self._metadata_datetime(
            session,
            "last_task_onboarding_prompt_at",
            reference=now,
            timezone_name=self.resolve_user_timezone_name(user),
        )
        if last_prompt_at and now - last_prompt_at < timedelta(minutes=self.config.schedule.task_onboarding_interval_minutes):
            return False
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_onboarding",
            "source": "missing_active_task",
            "step": "select_task",
            "reason": "No active ClickUp task with running tracking was confirmed while clocked in.",
            "draft": {},
        }
        session.metadata["last_task_onboarding_prompt_at"] = now.isoformat()
        await self._send_dm(client, user, session, await self._task_onboarding_intro(user, session, tracking), now)
        return True

    async def _task_onboarding_intro(
        self,
        user: UserProfile,
        session: SessionState,
        tracking: dict[str, Any],
    ) -> str:
        lines = []
        if tracking["active_task_name"] and not tracking["timer_running"]:
            lines.append(
                f"I need to re-confirm `{tracking['active_task_name']}` because I do not see an active timer tied to your current task flow."
            )
        else:
            lines.append("I need to confirm your active ClickUp task before you continue.")
        lines.append("Reply with the task name or task ID you are actively working on right now.")
        selection = await self._task_selection_prompt(user, session)
        return "\n\n".join(lines + [selection])

    async def _task_onboarding_reminder(
        self,
        user: UserProfile,
        session: SessionState,
        tracking: dict[str, Any],
        prompt: dict[str, Any],
    ) -> str:
        step = str(prompt.get("step") or "select_task")
        if step == "confirm_task":
            return self._task_onboarding_confirmation_prompt(prompt)
        task_name = str(prompt.get("task_name") or "")
        if step == "plan" and task_name:
            return self._task_onboarding_missing_text(prompt, step)
        if step == "tangible_result" and task_name:
            return self._task_onboarding_missing_text(prompt, step)
        if step == "necessity" and task_name:
            return self._task_onboarding_missing_text(prompt, step)
        if step == "effectiveness" and task_name:
            return self._task_onboarding_missing_text(prompt, step)
        if step == "estimated_duration" and task_name:
            return self._task_onboarding_missing_text(prompt, step)
        if step == "reconsider_threshold" and task_name:
            return self._task_onboarding_missing_text(prompt, step)
        if step == "fallback_plan" and task_name:
            return self._task_onboarding_missing_text(prompt, step)
        if step == "risk" and task_name:
            return f"I still need the predicted blockers or risks for `{task_name}`."
        if step == "photo" and task_name:
            return self._task_onboarding_missing_text(prompt, step)
        if tracking["active_task_name"] and not tracking["timer_running"]:
            prefix = f"I still need to reconnect task tracking for `{tracking['active_task_name']}`."
        else:
            prefix = "I still need your active task onboarding."
        return prefix + "\n\n" + await self._task_selection_prompt(
            user,
            session,
            recommended_task=(
                {
                    "id": prompt.get("recommended_task_id"),
                    "name": prompt.get("recommended_task_name"),
                }
                if prompt.get("recommended_task_id")
                else None
            ),
        )

    def _task_onboarding_draft(self, prompt: dict[str, Any]) -> dict[str, Any]:
        draft = prompt.get("draft")
        if not isinstance(draft, dict):
            draft = {}
            prompt["draft"] = draft
        return draft

    def _task_onboarding_question(self, prompt: dict[str, Any], step: str) -> str:
        task_name = str(prompt.get("task_name") or "this task")
        draft = self._task_onboarding_draft(prompt)
        admin_feedback = str(draft.get("admin_feedback") or "").strip()
        if step == "plan":
            question = f"What is your plan for `{task_name}`?"
            if admin_feedback:
                return f"Admin feedback to account for:\n{admin_feedback}\n\n{question}"
            return question
        if step == "tangible_result":
            return "What tangible result will show this task produced something real? Be specific about what you expect to point to."
        if step == "necessity":
            return f"Why is `{task_name}` necessary to do now?"
        if step == "effectiveness":
            return "Why do you believe this plan is the most effective way to get that result?"
        if step == "estimated_duration":
            return "How long do you expect this task to take? A rough answer like `45 minutes`, `2 hours`, or `half a day` is fine."
        if step == "reconsider_threshold":
            return "How long will you give this approach before you decide you are stuck or not producing good results?"
        if step == "fallback_plan":
            return "If you hit that point, what alternative plan, help request, or task switch would you consider next?"
        return "Tell me the next detail I still need for this task onboarding."

    def _task_onboarding_missing_text(self, prompt: dict[str, Any], step: str) -> str:
        task_name = str(prompt.get("task_name") or "this task")
        if step == "confirm_task":
            return self._task_onboarding_confirmation_prompt(prompt)
        if step == "plan":
            return f"I still need your plan for `{task_name}` before I can activate it."
        if step == "tangible_result":
            return f"I still need the tangible result you expect from `{task_name}`."
        if step == "necessity":
            return f"I still need why `{task_name}` is necessary to do now."
        if step == "effectiveness":
            return f"I still need why you think this plan will be effective for `{task_name}`."
        if step == "estimated_duration":
            return f"I still need your rough time estimate for `{task_name}`."
        if step == "reconsider_threshold":
            return "I still need to know how long you will give this approach before you reconsider."
        if step == "fallback_plan":
            return "I still need the alternative plan or next move you would consider if this approach stops working."
        if step == "photo":
            return (
                f"I still need the task-start picture for `{task_name}`. "
                "The task will not be officially tracked until you upload that starting image."
            )
        return "I still need your task onboarding reply."

    def _task_activation_notice(self, activation: TaskActivationResult, *, resumed: bool = False) -> str:
        label = activation.task_name or activation.task_id or "That task"
        tracking = activation.tracking_state if isinstance(activation.tracking_state, dict) else {}
        tracking_running = bool(tracking.get("timer_running")) and (
            not activation.task_id or str(tracking.get("timer_task_id") or "") == activation.task_id
        )
        timer_note = str(tracking.get("timer_note") or "").strip()
        if activation.clickup_status_name and tracking_running:
            if resumed:
                return (
                    f"Perfect. I confirmed `{label}` is back to `{activation.clickup_status_name}` "
                    "and task tracking is running again."
                )
            return f"Perfect. `{label}` is active in ClickUp and task tracking is running."
        if activation.clickup_status_name:
            if resumed:
                message = (
                    f"Perfect. I confirmed `{label}` is back to `{activation.clickup_status_name}`, "
                    "but I could not confirm that task tracking is running yet."
                )
            else:
                message = (
                    f"Perfect. `{label}` is active in ClickUp, but I could not confirm that task tracking is running yet."
                )
            if timer_note:
                message += f"\n\nNote: {timer_note}"
            return message
        if tracking_running:
            if resumed:
                message = (
                    f"Perfect. I resumed task tracking for `{label}`, but I could not confirm that ClickUp moved it back to `in progress`."
                )
            else:
                message = (
                    f"Perfect. I started task tracking for `{label}`, but I could not confirm that ClickUp moved it to `in progress`."
                )
            if timer_note:
                message += f"\n\nNote: {timer_note}"
            return message
        message = (
            f"Perfect. I saved `{label}` as the active task locally, but I could not confirm the ClickUp status or timer state yet."
        )
        if timer_note:
            message += f"\n\nNote: {timer_note}"
        return message

    def _task_onboarding_summary(self, prompt: dict[str, Any]) -> str:
        draft = self._task_onboarding_draft(prompt)
        fields = [
            ("Plan", draft.get("plan")),
            ("Tangible result", draft.get("tangible_result")),
            ("Necessary because", draft.get("necessity")),
            ("Why this should work", draft.get("effectiveness")),
            ("Expected duration", draft.get("estimated_duration")),
            ("Reconsider after", draft.get("reconsider_threshold")),
            ("Alternative if it is not working", draft.get("fallback_plan")),
        ]
        lines = [f"{label}: {str(value).strip()}" for label, value in fields if str(value or "").strip()]
        return "\n".join(lines)

    def _apply_task_onboarding_metadata(self, session: SessionState, prompt: dict[str, Any]) -> None:
        draft = self._task_onboarding_draft(prompt)
        for draft_key, metadata_key in _TASK_ONBOARDING_METADATA_FIELDS.items():
            value = str(draft.get(draft_key) or "").strip()
            if value:
                session.metadata[metadata_key] = value
            else:
                session.metadata.pop(metadata_key, None)
        summary = self._task_onboarding_summary(prompt)
        if summary:
            session.metadata["last_task_onboarding_summary"] = summary
            session.latest_plan = summary
        else:
            session.metadata.pop("last_task_onboarding_summary", None)

    async def _activate_clickup_task(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        task_id: str,
        task_name: str,
    ) -> TaskActivationResult:
        if not task_id:
            tracking_state = await self._get_task_tracking_state(user, session)
            return TaskActivationResult(
                task_id=None,
                task_name=task_name or None,
                clickup_status_name=None,
                tracking_state=tracking_state,
            )
        session.metadata["active_clickup_task_id"] = task_id
        if task_name:
            session.metadata["active_clickup_task_name"] = task_name
        status_name = await self._safe_set_task_state(session, task_id, "in_progress")
        await self._start_task_timer(user, session, now, task_id, task_name)
        tracking_state = await self._get_task_tracking_state(user, session)
        return TaskActivationResult(
            task_id=task_id,
            task_name=task_name or None,
            clickup_status_name=status_name,
            tracking_state=tracking_state,
        )

    async def _start_task_timer(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        task_id: str,
        task_name: str,
    ) -> None:
        tracking = session.metadata.get("clickup_time_tracking")
        if isinstance(tracking, dict) and tracking.get("task_id") == task_id and not tracking.get("closed_at"):
            return
        new_tracking: dict[str, Any] = {
            "task_id": task_id,
            "task_name": task_name,
            "started_at": now.isoformat(),
            "source": "local",
        }
        if self.clickup and self.config and self.config.clickup.create_time_entries:
            assignee_id = await self.clickup.resolve_clickup_user_id(user)
            if assignee_id:
                entry = await self.clickup.get_running_time_entry(assignee_id) if self.clickup.can_query_assignee_timers() else None
                entry_task_id = self._time_entry_task_id(entry) if entry else None
                if entry and entry_task_id == task_id:
                    new_tracking["source"] = "remote"
                    new_tracking["remote_entry_id"] = str(entry.get("id") or entry.get("timer_id") or "")
                    new_tracking["started_at"] = self._time_entry_start_iso(entry, now)
                elif self.clickup._assignee_timer_start_supported is not False:
                    started = await self.clickup.start_assignee_timer(
                        assignee_id=assignee_id,
                        task_id=task_id,
                        start_ms=int(now.timestamp() * 1000),
                        description=self._build_time_entry_description(session),
                    )
                    if started:
                        new_tracking["source"] = "remote"
                        new_tracking["remote_entry_id"] = str(started.get("id") or started.get("timer_id") or "")
                        new_tracking["started_at"] = self._time_entry_start_iso(started, now)
        session.metadata["clickup_time_tracking"] = new_tracking

    async def _pause_current_task_tracking(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        *,
        set_hold: bool,
        end_reason: str,
    ) -> str | None:
        active_task_id = self._active_task_id(session)
        active_task_name = str(session.metadata.get("active_clickup_task_name") or "the active task")
        if active_task_id and set_hold:
            await self._safe_set_task_state(session, active_task_id, "hold")
        tracking = session.metadata.get("clickup_time_tracking")
        if not isinstance(tracking, dict) or tracking.get("closed_at"):
            return None
        started_at = str(tracking.get("started_at") or "")
        tracked_task_id = str(tracking.get("task_id") or active_task_id or "")
        if not started_at:
            session.metadata.pop("clickup_time_tracking", None)
            return None
        description = self._build_time_entry_description(session)
        start_dt = datetime.fromisoformat(started_at)
        synced = False
        if self.clickup and self.config and self.config.clickup.create_time_entries:
            remote_entry_id = str(tracking.get("remote_entry_id") or "")
            if remote_entry_id:
                synced = bool(
                    await self.clickup.close_time_entry(
                        timer_id=remote_entry_id,
                        start_ms=int(start_dt.timestamp() * 1000),
                        stop_ms=int(now.timestamp() * 1000),
                        description=description,
                        task_id=tracked_task_id or None,
                    )
                )
            else:
                assignee_id = await self.clickup.resolve_clickup_user_id(user)
                if assignee_id:
                    synced = bool(
                        await self.clickup.create_time_entry(
                            assignee_id=assignee_id,
                            task_id=tracked_task_id or None,
                            start_ms=int(start_dt.timestamp() * 1000),
                            stop_ms=int(now.timestamp() * 1000),
                            description=description,
                        )
                    )
        tracking["closed_at"] = now.isoformat()
        tracking["duration_seconds"] = max(0, int((now - start_dt).total_seconds()))
        tracking["sync_result"] = "synced" if synced else "local_only"
        tracking["end_reason"] = end_reason
        history = session.metadata.get("clickup_time_tracking_history")
        if not isinstance(history, list):
            history = []
        history.append(tracking)
        session.metadata["clickup_time_tracking_history"] = history
        session.metadata.pop("clickup_time_tracking", None)
        if synced:
            return f"I paused time tracking for `{active_task_name}`."
        return f"I paused local task tracking for `{active_task_name}` and will keep the time window in the local log."

    async def _finalize_clickup_day(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
        *,
        allow_status_completion: bool = False,
        include_next_task_suggestion: bool = True,
        pause_reason: str = "clock_out",
        hold_active_task: bool = True,
    ) -> str | None:
        notes: list[str] = []
        task_id = self._active_task_id(session)
        summary_text = (session.latest_status or "").strip()
        if allow_status_completion and task_id and summary_text and self._looks_like_task_complete(summary_text):
            await self._safe_set_task_state(session, task_id, "complete")
            active_task_name = str(session.metadata.get("active_clickup_task_name") or "the active task")
            notes.append(f"I also marked `{active_task_name}` complete in ClickUp.")
        pause_note = await self._pause_current_task_tracking(
            user,
            session,
            now,
            set_hold=hold_active_task,
            end_reason=pause_reason,
        )
        if pause_note:
            notes.append(pause_note)
        if include_next_task_suggestion:
            suggestion = await self._build_next_task_suggestion(user, session)
            if suggestion:
                notes.append(suggestion)
        return "\n\n".join(notes) if notes else None

    async def _get_task_tracking_state(self, user: UserProfile, session: SessionState) -> dict[str, Any]:
        active_task_id = self._active_task_id(session)
        active_task_name = str(session.metadata.get("active_clickup_task_name") or "") or None
        timer_running = False
        timer_source = "none"
        timer_task_id: str | None = None
        timer_task_name: str | None = None
        timer_note: str | None = None
        timer_started_at: str | None = None
        local_tracking = session.metadata.get("clickup_time_tracking")
        if isinstance(local_tracking, dict) and not local_tracking.get("closed_at"):
            timer_task_id = str(local_tracking.get("task_id") or "") or None
            timer_task_name = str(local_tracking.get("task_name") or "") or None
            timer_running = bool(timer_task_id)
            timer_source = str(local_tracking.get("source") or "local")
            timer_started_at = str(local_tracking.get("started_at") or "") or None
        if self.clickup and self.config:
            assignee_id = await self.clickup.resolve_clickup_user_id(user)
            if assignee_id and self.clickup.can_query_assignee_timers():
                entry = await self.clickup.get_running_time_entry(assignee_id)
                if entry:
                    timer_running = True
                    timer_source = "clickup"
                    timer_task_id = self._time_entry_task_id(entry) or timer_task_id
                    timer_task_name = self._time_entry_task_name(entry) or timer_task_name
                    timer_started_at = self._time_entry_start_iso(
                        entry,
                        self.resolve_user_local_now(user),
                    )
                elif timer_running:
                    timer_note = "ClickUp does not expose a running assignee timer for this token, so the bot-managed timer is authoritative."
            elif assignee_id and not self.clickup.can_query_assignee_timers():
                if timer_running:
                    timer_note = "ClickUp live assignee timer queries are not permitted for the current token, so the bot-managed timer is authoritative."
                else:
                    timer_note = "ClickUp live assignee timer queries are not permitted for the current token."
            elif timer_running:
                timer_note = "ClickUp live assignee timer queries are not permitted for the current token, so the bot-managed timer is authoritative."
        if active_task_id and timer_task_id and active_task_id != timer_task_id:
            timer_note = f"timer appears tied to a different task ({timer_task_name or timer_task_id})"
            timer_running = False
        timer_elapsed_seconds = 0
        if timer_running and timer_started_at:
            timer_elapsed_seconds = self._elapsed_seconds_between(
                timer_started_at,
                self.resolve_user_local_now(user),
            )
        return {
            "active_task_id": active_task_id,
            "active_task_name": active_task_name,
            "timer_running": timer_running,
            "timer_source": timer_source,
            "timer_task_id": timer_task_id,
            "timer_task_name": timer_task_name,
            "timer_note": timer_note,
            "timer_started_at": timer_started_at,
            "timer_elapsed_seconds": timer_elapsed_seconds,
        }

    def _reconcile_active_task_with_tracking(
        self,
        session: SessionState,
        tracking: dict[str, Any],
    ) -> bool:
        timer_task_id = str(tracking.get("timer_task_id") or "").strip()
        if not timer_task_id:
            return False
        timer_task_name = str(tracking.get("timer_task_name") or "").strip() or None
        active_task_id = self._active_task_id(session)
        active_task_name = str(session.metadata.get("active_clickup_task_name") or "").strip() or None
        changed = False
        drifted = bool(active_task_id and active_task_id != timer_task_id)

        if active_task_id != timer_task_id:
            session.metadata["active_clickup_task_id"] = timer_task_id
            tracking["active_task_id"] = timer_task_id
            changed = True
        if timer_task_name:
            if active_task_name != timer_task_name:
                session.metadata["active_clickup_task_name"] = timer_task_name
                changed = True
            tracking["active_task_name"] = timer_task_name
        elif active_task_id != timer_task_id and active_task_name:
            session.metadata.pop("active_clickup_task_name", None)
            tracking["active_task_name"] = None
            changed = True
        if drifted:
            session.metadata["clickup_selection_reason"] = (
                "Recovered from the running task timer after active task metadata drifted."
            )
            changed = True
        if str(tracking.get("timer_task_id") or "").strip() == str(tracking.get("active_task_id") or "").strip():
            tracking["timer_running"] = bool(tracking.get("timer_task_id"))
            timer_note = str(tracking.get("timer_note") or "")
            if timer_note.startswith("timer appears tied to a different task"):
                tracking["timer_note"] = None
        return changed

    def _build_time_entry_description(self, session: SessionState) -> str:
        parts = ["Intern work session"]
        if session.latest_plan:
            parts.append(f"Plan: {session.latest_plan}")
        if session.latest_status:
            parts.append(f"Wrap-up: {session.latest_status}")
        if session.latest_blocker:
            parts.append(f"Blocker: {session.latest_blocker}")
        return " | ".join(parts)

    def _time_entry_task_id(self, entry: dict[str, Any] | None) -> str | None:
        if not isinstance(entry, dict):
            return None
        for key in ("tid", "task_id"):
            value = entry.get(key)
            if value:
                return str(value)
        task = entry.get("task")
        if isinstance(task, dict) and task.get("id"):
            return str(task.get("id"))
        return None

    def _time_entry_task_name(self, entry: dict[str, Any] | None) -> str | None:
        if not isinstance(entry, dict):
            return None
        task = entry.get("task")
        if isinstance(task, dict) and task.get("name"):
            return str(task.get("name"))
        return None

    def _time_entry_start_iso(self, entry: dict[str, Any], fallback_now: datetime) -> str:
        raw_start = entry.get("start")
        try:
            start_ms = int(raw_start)
        except (TypeError, ValueError):
            return fallback_now.isoformat()
        return datetime.fromtimestamp(start_ms / 1000, tz=fallback_now.tzinfo).isoformat()

    def _storage_root_path(self) -> Path | None:
        storage_root = getattr(self.bootstrap, "storage_root_path", None)
        if not storage_root:
            return None
        return Path(storage_root)

    def _time_tracking_report_path(self) -> Path | None:
        storage_root = self._storage_root_path()
        if storage_root is None:
            return None
        return storage_root / _TIME_TRACKING_REPORT_RELATIVE_PATH

    def _resolve_session_archive_path(
        self,
        user: UserProfile,
        session_date: str,
    ) -> Path | None:
        storage_root = self._storage_root_path()
        if storage_root is None:
            return None
        return (
            storage_root
            / "people"
            / _safe_storage_name(user.storage_folder_name or user.user_key)
            / session_date
            / "session.json"
        )

    def _manual_time_edits_path(
        self,
        user: UserProfile,
        session_date: str,
    ) -> Path | None:
        archive_path = self._resolve_session_archive_path(user, session_date)
        if archive_path is None:
            return None
        return archive_path.with_name(_MANUAL_TIME_EDITS_FILENAME)

    def _load_session_for_manual_time_edit(
        self,
        user: UserProfile,
        session_date: str,
    ) -> SessionState:
        session = self.state_store.get_session(user.user_key, session_date)
        self._normalize_session_state(session, user=user)
        if (
            session.session_date == session_date
            and (
                session.work_segments
                or session.clocked_in_at
                or session.clocked_out_at
                or session.metadata
                or session.stage != "awaiting_clock_in"
            )
        ):
            return session
        archive_path = self._resolve_session_archive_path(user, session_date)
        if archive_path is None or not archive_path.exists():
            return session
        try:
            archived = SessionState(**json.loads(archive_path.read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return session
        self._normalize_session_state(archived, user=user)
        return archived

    async def preview_manual_time_edit(
        self,
        user_key: str,
        session_date: str,
        segments: list[dict[str, Any]],
        *,
        edited_by: str,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        await self.refresh_configuration()
        user = self.resolve_user_profile(user_key)
        if user is None:
            raise ValueError(f"Unknown user_key {user_key!r}.")
        reference_now = now or self.resolve_user_local_now(user)
        session = self._load_session_for_manual_time_edit(user, session_date)
        _edited_session, preview_payload, _audit_entry = self._build_manual_time_edit_preview(
            user,
            session,
            segments,
            edited_by=edited_by,
            reason=reason,
            now=reference_now,
        )
        return preview_payload

    async def apply_manual_time_edit(
        self,
        user_key: str,
        session_date: str,
        segments: list[dict[str, Any]],
        *,
        edited_by: str,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        await self.refresh_configuration()
        user = self.resolve_user_profile(user_key)
        if user is None:
            raise ValueError(f"Unknown user_key {user_key!r}.")
        reference_now = now or self.resolve_user_local_now(user)
        current_session = self._load_session_for_manual_time_edit(user, session_date)
        previous_session = self._clone_session_state(current_session)
        edited_session, preview_payload, audit_entry = self._build_manual_time_edit_preview(
            user,
            current_session,
            segments,
            edited_by=edited_by,
            reason=reason,
            now=reference_now,
        )
        self._append_manual_time_edit_metadata(edited_session, audit_entry)
        await self._persist_session_state(
            user,
            edited_session,
            now=reference_now,
            previous_session=previous_session,
            trigger="manual_time_edit",
            details={
                "edited_by": str(audit_entry.get("edited_by") or ""),
                "reason": str(audit_entry.get("reason") or ""),
                "session_date": session_date,
                "old_segment_count": len(audit_entry.get("old_work_segments") or []),
                "new_segment_count": len(audit_entry.get("new_work_segments") or []),
                "clocked_in_delta_seconds": (
                    int(audit_entry.get("new_clocked_in_total_seconds") or 0)
                    - int(audit_entry.get("old_clocked_in_total_seconds") or 0)
                ),
                "task_tracked_delta_seconds": (
                    int(audit_entry.get("new_task_tracked_total_seconds") or 0)
                    - int(audit_entry.get("old_task_tracked_total_seconds") or 0)
                ),
            },
        )
        workspace = await self.store.ensure_user_workspace(user, session_date)
        await self.store.append_json_line(
            workspace.daily_dir / _MANUAL_TIME_EDITS_FILENAME,
            audit_entry,
        )
        try:
            await self.write_dashboard()
        except Exception:
            logger.exception(
                "Failed to rebuild dashboard after manual time edit for %s on %s.",
                user.user_key,
                session_date,
            )
        preview_payload["applied"] = True
        preview_payload["latest_manual_edit"] = audit_entry
        return preview_payload

    def _build_manual_time_edit_preview(
        self,
        user: UserProfile,
        session: SessionState,
        segments: list[dict[str, Any]],
        *,
        edited_by: str,
        reason: str,
        now: datetime,
    ) -> tuple[SessionState, dict[str, Any], dict[str, Any]]:
        edited_by_text, reason_text, normalized_segments = self._validate_manual_time_edit_request(
            user,
            session.session_date,
            segments,
            edited_by=edited_by,
            reason=reason,
            now=now,
        )
        timezone_name = self.resolve_user_timezone_name(user)
        original_session = self._clone_session_state(session)
        edited_session = self._clone_session_state(session)
        self._normalize_session_state(original_session, user=user)
        self._normalize_session_state(edited_session, user=user)

        before_now = self._session_time_summary_reference_now(original_session, user, now)
        self._refresh_session_time_summary(original_session, before_now)

        edited_session.work_segments = normalized_segments
        edited_session.clocked_in_at = normalized_segments[0]["clocked_in_at"]
        edited_session.clocked_out_at = normalized_segments[-1]["clocked_out_at"]
        edited_session.stage = "clocked_out"
        edited_session.awaiting_start_photo = False
        edited_session.awaiting_clock_out_photo = False
        edited_session.awaiting_clock_out_summary = False
        edited_session.pending_clickup_sync = False
        edited_session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
        edited_session.metadata.pop("session_reactivated_at", None)
        self._clear_follow_up_probe_tracking(edited_session)
        self._clear_pending_lunch_confirmation(edited_session)
        self._clear_auto_clock_out_metadata(edited_session)
        self._ensure_work_segments_consistency(edited_session)

        work_segment_bounds = self._build_work_segment_bounds(
            edited_session.work_segments,
            timezone_name=timezone_name,
        )
        clamped_tracking, tracking_stats = self._clamp_time_tracking_entries_to_work_segments(
            edited_session,
            work_segment_bounds,
            timezone_name=timezone_name,
        )
        if clamped_tracking:
            edited_session.metadata["clickup_time_tracking_history"] = clamped_tracking
        else:
            edited_session.metadata.pop("clickup_time_tracking_history", None)
        edited_session.metadata.pop("clickup_time_tracking", None)

        retro_stats = self._clamp_retro_backfill_task_windows_to_work_segments(
            edited_session,
            work_segment_bounds,
            timezone_name=timezone_name,
        )

        after_now = self._session_time_summary_reference_now(edited_session, user, now)
        self._refresh_session_time_summary(edited_session, after_now)

        warnings: list[str] = []
        if tracking_stats["dropped"] > 0:
            warnings.append(
                f"Dropped {tracking_stats['dropped']} ClickUp tracking window(s) that no longer overlap the edited work segments."
            )
        if tracking_stats["closed_open"] > 0:
            warnings.append(
                f"Closed {tracking_stats['closed_open']} open task-tracking window(s) at the edited work-segment boundary."
            )
        if tracking_stats["split"] > 0:
            warnings.append(
                f"Split {tracking_stats['split']} task-tracking overlap(s) across separate edited work segments."
            )
        if retro_stats["dropped"] > 0:
            warnings.append(
                f"Dropped {retro_stats['dropped']} retro-backfill task window(s) outside the edited work segments."
            )
        if retro_stats["split"] > 0:
            warnings.append(
                f"Split {retro_stats['split']} retro-backfill task-window overlap(s) across separate edited work segments."
            )

        before_payload = self._manual_time_edit_session_payload(
            user,
            original_session,
            reference_now=before_now,
        )
        after_payload = self._manual_time_edit_session_payload(
            user,
            edited_session,
            reference_now=after_now,
        )
        audit_entry = {
            "edited_at": now.isoformat(),
            "edited_by": edited_by_text,
            "reason": reason_text,
            "session_date": session.session_date,
            "user_key": user.user_key,
            "display_name": user.display_name,
            "timezone": timezone_name,
            "old_stage": original_session.stage,
            "new_stage": edited_session.stage,
            "old_work_segments": before_payload["work_segments"],
            "new_work_segments": after_payload["work_segments"],
            "old_clocked_in_total_seconds": before_payload["clocked_in_total_seconds"],
            "new_clocked_in_total_seconds": after_payload["clocked_in_total_seconds"],
            "old_task_tracked_total_seconds": before_payload["task_tracked_total_seconds"],
            "new_task_tracked_total_seconds": after_payload["task_tracked_total_seconds"],
            "warnings": warnings,
            "tracking_adjustments": tracking_stats,
            "retro_task_window_adjustments": retro_stats,
        }
        preview_payload = {
            "applied": False,
            "user_key": user.user_key,
            "display_name": user.display_name,
            "session_date": session.session_date,
            "timezone": timezone_name,
            "edited_by": edited_by_text,
            "reason": reason_text,
            "before": before_payload,
            "after": after_payload,
            "warnings": warnings,
        }
        return edited_session, preview_payload, audit_entry

    def _validate_manual_time_edit_request(
        self,
        user: UserProfile,
        session_date: str,
        segments: list[dict[str, Any]],
        *,
        edited_by: str,
        reason: str,
        now: datetime,
    ) -> tuple[str, str, list[dict[str, str | None]]]:
        edited_by_text = str(edited_by or "").strip()
        if not edited_by_text:
            raise ValueError("`edited_by` is required.")
        reason_text = str(reason or "").strip()
        if not reason_text:
            raise ValueError("`reason` is required.")
        current_workday = self.resolve_user_workday_date(user, now)
        if session_date >= current_workday:
            raise ValueError(
                f"Only past workdays can be edited. {session_date} is not earlier than the current effective workday {current_workday}."
            )
        if not isinstance(segments, list) or not segments:
            raise ValueError("At least one complete work segment is required.")
        timezone_name = self.resolve_user_timezone_name(user)
        normalized_segments: list[dict[str, str | None]] = []
        previous_end: datetime | None = None
        for index, raw_segment in enumerate(segments, start=1):
            if not isinstance(raw_segment, dict):
                raise ValueError(f"Segment {index} is not a valid object.")
            start_raw = str(
                raw_segment.get("start_local")
                or raw_segment.get("clocked_in_local")
                or raw_segment.get("clocked_in_at")
                or ""
            ).strip()
            end_raw = str(
                raw_segment.get("end_local")
                or raw_segment.get("clocked_out_local")
                or raw_segment.get("clocked_out_at")
                or ""
            ).strip()
            if not start_raw or not end_raw:
                raise ValueError(f"Segment {index} must include both a start and an end time.")
            start_dt = self._parse_manual_time_edit_local_datetime(start_raw, timezone_name=timezone_name)
            end_dt = self._parse_manual_time_edit_local_datetime(end_raw, timezone_name=timezone_name)
            if end_dt <= start_dt:
                raise ValueError(f"Segment {index} must end after it starts.")
            if previous_end is not None and start_dt < previous_end:
                raise ValueError(f"Segment {index} overlaps or is out of order.")
            normalized_segments.append(
                {
                    "clocked_in_at": start_dt.isoformat(),
                    "clocked_out_at": end_dt.isoformat(),
                }
            )
            previous_end = end_dt
        return edited_by_text, reason_text, normalized_segments

    def _parse_manual_time_edit_local_datetime(
        self,
        raw_value: str,
        *,
        timezone_name: str,
    ) -> datetime:
        text = str(raw_value or "").strip()
        if not text:
            raise ValueError("Missing datetime value.")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"Invalid datetime value {text!r}. Use a local datetime like 2026-06-10T08:30."
            ) from exc
        return localize_datetime(parsed, timezone_name)

    def _build_work_segment_bounds(
        self,
        work_segments: list[dict[str, Any]],
        *,
        timezone_name: str,
    ) -> list[tuple[datetime, datetime]]:
        bounds: list[tuple[datetime, datetime]] = []
        for segment in work_segments:
            if not isinstance(segment, dict):
                continue
            started_at = self._coerce_datetime(
                str(segment.get("clocked_in_at") or ""),
                timezone_name=timezone_name,
            )
            ended_at = self._coerce_datetime(
                str(segment.get("clocked_out_at") or ""),
                timezone_name=timezone_name,
            )
            if started_at is None or ended_at is None or ended_at <= started_at:
                continue
            bounds.append((started_at, ended_at))
        return bounds

    def _clip_time_range_to_work_segments(
        self,
        start_dt: datetime,
        end_dt: datetime,
        work_segment_bounds: list[tuple[datetime, datetime]],
    ) -> list[tuple[datetime, datetime]]:
        clipped: list[tuple[datetime, datetime]] = []
        for segment_start, segment_end in work_segment_bounds:
            overlap_start = max(start_dt, segment_start)
            overlap_end = min(end_dt, segment_end)
            if overlap_end > overlap_start:
                clipped.append((overlap_start, overlap_end))
        return clipped

    def _clamp_time_tracking_entries_to_work_segments(
        self,
        session: SessionState,
        work_segment_bounds: list[tuple[datetime, datetime]],
        *,
        timezone_name: str,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        entries: list[dict[str, Any]] = []
        history = session.metadata.get("clickup_time_tracking_history")
        if isinstance(history, list):
            entries.extend(item for item in history if isinstance(item, dict))
        current = session.metadata.get("clickup_time_tracking")
        if isinstance(current, dict):
            entries.append(current)
        fallback_end = work_segment_bounds[-1][1] if work_segment_bounds else None
        clamped: list[dict[str, Any]] = []
        stats = {"dropped": 0, "split": 0, "closed_open": 0}
        for entry in entries:
            started_at = self._coerce_datetime(
                str(entry.get("started_at") or ""),
                timezone_name=timezone_name,
            )
            if started_at is None:
                stats["dropped"] += 1
                continue
            raw_closed_at = str(entry.get("closed_at") or "").strip()
            closed_at = self._coerce_datetime(raw_closed_at, timezone_name=timezone_name)
            if closed_at is None:
                if fallback_end is None:
                    stats["dropped"] += 1
                    continue
                closed_at = fallback_end
                stats["closed_open"] += 1
            if closed_at <= started_at:
                stats["dropped"] += 1
                continue
            overlaps = self._clip_time_range_to_work_segments(started_at, closed_at, work_segment_bounds)
            if not overlaps:
                stats["dropped"] += 1
                continue
            if len(overlaps) > 1:
                stats["split"] += len(overlaps) - 1
            for overlap_start, overlap_end in overlaps:
                clone = dict(entry)
                clone["started_at"] = overlap_start.isoformat()
                clone["closed_at"] = overlap_end.isoformat()
                clone["duration_seconds"] = max(0, int((overlap_end - overlap_start).total_seconds()))
                if not raw_closed_at:
                    clone["end_reason"] = str(clone.get("end_reason") or "manual_time_edit")
                    clone["sync_result"] = str(clone.get("sync_result") or "local_only")
                clamped.append(clone)
        clamped.sort(key=lambda item: str(item.get("started_at") or ""))
        return clamped, stats

    def _clamp_retro_backfill_task_windows_to_work_segments(
        self,
        session: SessionState,
        work_segment_bounds: list[tuple[datetime, datetime]],
        *,
        timezone_name: str,
    ) -> dict[str, int]:
        retro = session.metadata.get(_RETRO_HOURS_BACKFILL_METADATA_KEY)
        if not isinstance(retro, dict):
            return {"dropped": 0, "split": 0}
        raw_windows = retro.get("task_windows")
        if not isinstance(raw_windows, list):
            return {"dropped": 0, "split": 0}
        fallback_end = work_segment_bounds[-1][1] if work_segment_bounds else None
        clamped: list[dict[str, Any]] = []
        stats = {"dropped": 0, "split": 0}
        for window in raw_windows:
            if not isinstance(window, dict):
                stats["dropped"] += 1
                continue
            started_at = self._coerce_datetime(
                str(window.get("started_at") or ""),
                timezone_name=timezone_name,
            )
            if started_at is None:
                stats["dropped"] += 1
                continue
            raw_ended_at = str(window.get("ended_at") or "").strip()
            ended_at = self._coerce_datetime(raw_ended_at, timezone_name=timezone_name)
            if ended_at is None:
                if fallback_end is None:
                    stats["dropped"] += 1
                    continue
                ended_at = fallback_end
            if ended_at <= started_at:
                stats["dropped"] += 1
                continue
            overlaps = self._clip_time_range_to_work_segments(started_at, ended_at, work_segment_bounds)
            if not overlaps:
                stats["dropped"] += 1
                continue
            if len(overlaps) > 1:
                stats["split"] += len(overlaps) - 1
            for overlap_start, overlap_end in overlaps:
                clone = dict(window)
                clone["started_at"] = overlap_start.isoformat()
                clone["ended_at"] = overlap_end.isoformat()
                clone["duration_seconds"] = max(0, int((overlap_end - overlap_start).total_seconds()))
                clamped.append(clone)
        retro["task_windows"] = clamped
        return stats

    def _serialize_work_segments_for_editor(
        self,
        work_segments: list[dict[str, Any]],
        *,
        timezone_name: str,
    ) -> list[dict[str, str]]:
        serialized: list[dict[str, str]] = []
        for segment in work_segments:
            if not isinstance(segment, dict):
                continue
            started_text = str(segment.get("clocked_in_at") or "").strip()
            ended_text = str(segment.get("clocked_out_at") or "").strip()
            if not started_text or not ended_text:
                continue
            started_at = self._coerce_datetime(started_text, timezone_name=timezone_name)
            ended_at = self._coerce_datetime(ended_text, timezone_name=timezone_name)
            if started_at is None or ended_at is None:
                continue
            serialized.append(
                {
                    "clocked_in_at": started_at.isoformat(),
                    "clocked_out_at": ended_at.isoformat(),
                    "start_local": localize_datetime(started_at, timezone_name).strftime("%Y-%m-%dT%H:%M"),
                    "end_local": localize_datetime(ended_at, timezone_name).strftime("%Y-%m-%dT%H:%M"),
                }
            )
        return serialized

    def _manual_time_edit_session_payload(
        self,
        user: UserProfile,
        session: SessionState,
        *,
        reference_now: datetime,
    ) -> dict[str, Any]:
        timezone_name = self.resolve_user_timezone_name(user)
        return {
            "stage": session.stage,
            "clocked_in_at": session.clocked_in_at,
            "clocked_out_at": session.clocked_out_at,
            "gross_clocked_in_total_seconds": int(session.time_summary.get("gross_clocked_in_total_seconds") or 0),
            "gross_clocked_in_total_human": str(session.time_summary.get("gross_clocked_in_total_human") or "0m"),
            "unpaid_lunch_deducted_seconds": int(session.time_summary.get("unpaid_lunch_deducted_seconds") or 0),
            "unpaid_lunch_deducted_human": str(session.time_summary.get("unpaid_lunch_deducted_human") or "0m"),
            "clocked_in_total_seconds": int(session.time_summary.get("clocked_in_total_seconds") or 0),
            "clocked_in_total_human": str(session.time_summary.get("clocked_in_total_human") or "0m"),
            "task_tracked_total_seconds": int(session.time_summary.get("task_tracked_total_seconds") or 0),
            "task_tracked_total_human": str(session.time_summary.get("task_tracked_total_human") or "0m"),
            "work_segments": self._serialize_work_segments_for_editor(
                session.work_segments,
                timezone_name=timezone_name,
            ),
            "time_by_task": list(session.time_summary.get("time_by_task") or []),
            "reference_now": reference_now.isoformat(),
        }

    def _append_manual_time_edit_metadata(
        self,
        session: SessionState,
        audit_entry: dict[str, Any],
    ) -> None:
        history = session.metadata.get("manual_time_edits")
        if not isinstance(history, list):
            history = []
        history.append(audit_entry)
        session.metadata["manual_time_edits"] = history[-20:]
        session.metadata["latest_manual_time_edit"] = audit_entry

    async def _write_time_tracking_csv(
        self,
        *,
        now: datetime,
        session_overrides: list[tuple[UserProfile, SessionState]] | None = None,
    ) -> Path | None:
        report_path, _rows = await asyncio.to_thread(
            self._write_time_tracking_csv_sync,
            now,
            session_overrides,
        )
        return report_path

    def _write_time_tracking_csv_sync(
        self,
        now: datetime,
        session_overrides: list[tuple[UserProfile, SessionState]] | None = None,
    ) -> tuple[Path | None, list[dict[str, Any]]]:
        rows = self._collect_time_tracking_report_rows_sync(
            now,
            session_overrides=session_overrides,
        )
        report_path = self._time_tracking_report_path()
        if report_path is None:
            return None, rows
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(_TIME_TRACKING_REPORT_FIELDS))
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)
        except OSError:
            logger.exception("Failed to write time tracking report to %s.", report_path)
            return None, rows
        write_time_tracking_dashboard(self._storage_root_path())
        return report_path.resolve(), rows

    def _collect_time_tracking_report_rows_sync(
        self,
        now: datetime,
        *,
        session_overrides: list[tuple[UserProfile, SessionState]] | None = None,
    ) -> list[dict[str, Any]]:
        sessions_by_key: dict[tuple[str, str], tuple[UserProfile, SessionState, Path | None]] = {}
        storage_root = self._storage_root_path()
        people_dir = storage_root / "people" if storage_root is not None else None
        if people_dir and people_dir.exists():
            for user_dir in sorted(people_dir.iterdir(), key=lambda path: path.name.lower()):
                if not user_dir.is_dir():
                    continue
                profile: UserProfile | None = None
                profile_path = user_dir / "profile.json"
                if profile_path.exists():
                    try:
                        profile = UserProfile(**json.loads(profile_path.read_text(encoding="utf-8")))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        profile = None
                for daily_dir in sorted(user_dir.iterdir(), key=lambda path: path.name):
                    if not daily_dir.is_dir() or not _SESSION_DATE_DIRECTORY_PATTERN.fullmatch(daily_dir.name):
                        continue
                    session_path = daily_dir / "session.json"
                    if not session_path.exists():
                        continue
                    try:
                        session = SessionState(**json.loads(session_path.read_text(encoding="utf-8")))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    row_user = profile or UserProfile(
                        user_key=session.user_key,
                        display_name=session.user_key,
                        discord_user_id=0,
                        discord_username=session.user_key,
                        storage_folder_name=user_dir.name,
                    )
                    sessions_by_key[(session.user_key, session.session_date)] = (
                        row_user,
                        session,
                        session_path.resolve(),
                    )
        for override_user, override_session in session_overrides or []:
            session_copy = self._clone_session_state(override_session)
            session_path = self._resolve_session_archive_path(override_user, session_copy.session_date)
            sessions_by_key[(session_copy.user_key, session_copy.session_date)] = (
                override_user,
                session_copy,
                session_path.resolve() if session_path is not None else None,
            )
        rows: list[dict[str, Any]] = []
        for _key, (row_user, row_session, session_path) in sorted(
            sessions_by_key.items(),
            key=lambda item: (item[0][0].lower(), item[0][1]),
        ):
            self._normalize_session_state(row_session, user=row_user)
            rows.append(
                self._build_time_tracking_report_row(
                    row_user,
                    row_session,
                    now=now,
                    session_path=session_path,
                )
            )
        return rows

    def _build_time_tracking_report_row(
        self,
        user: UserProfile,
        session: SessionState,
        *,
        now: datetime,
        session_path: Path | None,
    ) -> dict[str, Any]:
        summary_now = self._session_time_summary_reference_now(session, user, now)
        self._refresh_session_time_summary(session, summary_now)
        time_by_task = session.time_summary.get("time_by_task")
        if not isinstance(time_by_task, list):
            time_by_task = []
        manual_edits = session.metadata.get("manual_time_edits")
        manual_edit_count = len(manual_edits) if isinstance(manual_edits, list) else 0
        latest_manual_edit = session.metadata.get("latest_manual_time_edit")
        latest_manual_edit_json = (
            json.dumps(latest_manual_edit, sort_keys=True, separators=(",", ":"))
            if isinstance(latest_manual_edit, dict)
            else ""
        )
        retro_backfill = session.metadata.get(_RETRO_HOURS_BACKFILL_METADATA_KEY)
        retro_backfill = retro_backfill if isinstance(retro_backfill, dict) else {}
        retro_warnings = retro_backfill.get("warnings")
        review = self._build_time_tracking_review_snapshot(user, session, now=now)
        return {
            "user_key": session.user_key,
            "display_name": user.display_name,
            "timezone": self.resolve_user_timezone_name(user),
            "session_date": session.session_date,
            "gross_clocked_in_total_seconds": int(session.time_summary.get("gross_clocked_in_total_seconds") or 0),
            "gross_clocked_in_total_human": str(session.time_summary.get("gross_clocked_in_total_human") or "0m"),
            "unpaid_lunch_deducted_seconds": int(session.time_summary.get("unpaid_lunch_deducted_seconds") or 0),
            "unpaid_lunch_deducted_human": str(session.time_summary.get("unpaid_lunch_deducted_human") or "0m"),
            "clocked_in_total_seconds": int(session.time_summary.get("clocked_in_total_seconds") or 0),
            "clocked_in_total_human": str(session.time_summary.get("clocked_in_total_human") or "0m"),
            "task_tracked_total_seconds": int(session.time_summary.get("task_tracked_total_seconds") or 0),
            "task_tracked_total_human": str(session.time_summary.get("task_tracked_total_human") or "0m"),
            "work_segment_count": int(session.time_summary.get("work_segment_count") or 0),
            "has_open_work_segment": bool(session.time_summary.get("has_open_work_segment")),
            "active_task_timer_running": bool(session.time_summary.get("active_task_timer_running")),
            "manual_edit_count": manual_edit_count,
            "latest_manual_edit_json": latest_manual_edit_json,
            "retro_backfill_confidence": str(retro_backfill.get("confidence") or ""),
            "retro_backfill_warning_count": len(retro_warnings) if isinstance(retro_warnings, list) else 0,
            "review_status": str(review.get("status") or "likely_correct"),
            "review_summary": str(review.get("summary") or ""),
            "review_reasons_json": json.dumps(review.get("reasons") or [], sort_keys=True, separators=(",", ":")),
            "review_gap_seconds": int(review.get("gap_seconds") or 0),
            "review_gap_human": str(review.get("gap_human") or "0m"),
            "time_by_task_json": json.dumps(time_by_task, sort_keys=True, separators=(",", ":")),
            "session_path": str(session_path.resolve()) if session_path is not None else "",
        }

    def _build_time_tracking_review_snapshot(
        self,
        user: UserProfile,
        session: SessionState,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        clocked_in_total_seconds = int(session.time_summary.get("clocked_in_total_seconds") or 0)
        gross_clocked_in_total_seconds = int(session.time_summary.get("gross_clocked_in_total_seconds") or 0)
        recorded_lunch_seconds = int(session.time_summary.get("unpaid_lunch_deducted_seconds") or 0)
        task_tracked_total_seconds = int(session.time_summary.get("task_tracked_total_seconds") or 0)
        work_segment_count = int(session.time_summary.get("work_segment_count") or 0)
        has_open_work_segment = bool(session.time_summary.get("has_open_work_segment"))
        active_task_timer_running = bool(session.time_summary.get("active_task_timer_running"))
        gap_seconds = abs(clocked_in_total_seconds - task_tracked_total_seconds)
        current_workday = self.resolve_user_workday_date(user, now)
        is_current_workday = session.session_date == current_workday
        manual_edits = session.metadata.get("manual_time_edits")
        manual_edit_count = len(manual_edits) if isinstance(manual_edits, list) else 0
        retro_backfill = session.metadata.get(_RETRO_HOURS_BACKFILL_METADATA_KEY)
        retro_backfill = retro_backfill if isinstance(retro_backfill, dict) else {}
        retro_confidence = str(retro_backfill.get("confidence") or "")
        retro_warnings = retro_backfill.get("warnings")
        retro_warning_count = len(retro_warnings) if isinstance(retro_warnings, list) else 0

        if is_current_workday and (
            has_open_work_segment
            or active_task_timer_running
            or (clocked_in_total_seconds > 0 and not session.clocked_out_at and session.stage != "clocked_out")
        ):
            return {
                "status": "in_progress",
                "summary": "Current workday is still active, so the final totals can still change.",
                "reasons": [
                    "Current workday is still active, so the final totals can still change.",
                ],
                "gap_seconds": gap_seconds,
                "gap_human": self._format_duration(gap_seconds),
            }

        severity = 0
        issues: list[str] = []
        notes: list[str] = []

        def add_issue(level: int, message: str) -> None:
            nonlocal severity
            severity = max(severity, level)
            if message not in issues:
                issues.append(message)

        def add_note(message: str) -> None:
            if message not in notes:
                notes.append(message)

        if not is_current_workday and has_open_work_segment:
            add_issue(2, "Past workday still has an open work segment.")
        if not is_current_workday and active_task_timer_running:
            add_issue(2, "Past workday still shows an active task timer.")
        if clocked_in_total_seconds > 0 and work_segment_count <= 0:
            add_issue(2, "Clocked-in time exists, but no work segments were stored for the day.")
        if not is_current_workday and clocked_in_total_seconds > 0 and not session.clocked_out_at:
            add_issue(1, "Past workday never reached a stored clock-out timestamp.")
        if gross_clocked_in_total_seconds > 14 * 60 * 60:
            add_issue(
                2,
                f"Gross clocked-in time is unusually long at {self._format_duration(gross_clocked_in_total_seconds)}.",
            )
        lunch_started_at = str(session.metadata.get("lunch_started_at") or "").strip()
        lunch_ended_at = str(session.metadata.get("lunch_ended_at") or "").strip()
        if not is_current_workday and lunch_started_at and not lunch_ended_at:
            add_issue(2, "Past workday has a recorded lunch start but no recorded return.")
        elif recorded_lunch_seconds > 90 * 60:
            add_issue(
                1,
                f"Recorded lunch is unusually long at {self._format_duration(recorded_lunch_seconds)}; verify the return time.",
            )

        task_overrun_seconds = task_tracked_total_seconds - clocked_in_total_seconds
        if task_overrun_seconds > 5 * 60:
            add_issue(
                2,
                f"Task-tracked time exceeds clocked-in time by {self._format_duration(task_overrun_seconds)}.",
            )
        elif clocked_in_total_seconds >= 4 * 60 * 60 and task_tracked_total_seconds == 0:
            add_issue(
                1,
                f"No task-tracked time was recorded for a {self._format_duration(clocked_in_total_seconds)} day.",
            )
        else:
            uncovered_gap_seconds = clocked_in_total_seconds - task_tracked_total_seconds
            if uncovered_gap_seconds >= 4 * 60 * 60:
                add_issue(
                    2,
                    f"Clocked-in time exceeds task-tracked time by {self._format_duration(uncovered_gap_seconds)}.",
                )
            elif uncovered_gap_seconds >= 2 * 60 * 60 and task_tracked_total_seconds > 0:
                add_issue(
                    1,
                    f"Clocked-in time exceeds task-tracked time by {self._format_duration(uncovered_gap_seconds)}.",
                )

        if retro_confidence == "unresolved":
            add_issue(2, "Retro backfill confidence is unresolved for this day.")
        elif retro_confidence == "low":
            add_issue(1, "Retro backfill confidence is low for this day.")
        elif retro_confidence == "medium":
            add_note("Retro backfill confidence is medium for this day.")
        elif retro_confidence == "high":
            add_note("Retro backfill confidence is high for this day.")

        if retro_warning_count > 0:
            add_issue(1, f"Retro backfill recorded {retro_warning_count} warning(s) for this day.")

        if manual_edit_count > 1:
            add_issue(1, f"This day has been manually corrected {manual_edit_count} times.")
        elif manual_edit_count == 1:
            add_note("This day has one manual correction on record.")

        latest_manual_edit = session.metadata.get("latest_manual_time_edit")
        if isinstance(latest_manual_edit, dict):
            edited_by = str(latest_manual_edit.get("edited_by") or "").strip()
            if edited_by:
                add_note(f"Latest manual correction was recorded by {edited_by}.")
        latest_lunch_edit = session.metadata.get("latest_manual_lunch_edit")
        if isinstance(latest_lunch_edit, dict):
            add_note("This day has a transcript-backed manual lunch correction.")

        status = "likely_correct"
        if severity >= 2:
            status = "likely_wrong"
        elif severity == 1:
            status = "needs_review"

        reasons = issues + notes
        summary = (
            issues[0]
            if issues
            else notes[0]
            if notes
            else "No suspicious timing issues were detected for this day."
        )
        return {
            "status": status,
            "summary": summary,
            "reasons": reasons,
            "gap_seconds": gap_seconds,
            "gap_human": self._format_duration(gap_seconds),
        }

    def _session_time_summary_reference_now(
        self,
        session: SessionState,
        user: UserProfile,
        now: datetime,
    ) -> datetime:
        current_workday = self.resolve_user_workday_date(user, now)
        if session.session_date == current_workday:
            return now
        timezone_name = self.resolve_user_timezone_name(user)
        latest_known = self._latest_known_session_timestamp(session, timezone_name=timezone_name)
        if latest_known is None:
            return now
        comparable_latest = self._coerce_datetime_for_reference(
            latest_known.isoformat(),
            reference=now,
            timezone_name=timezone_name,
        )
        if comparable_latest is None:
            return now
        return now if comparable_latest > now else comparable_latest

    def _latest_known_session_timestamp(
        self,
        session: SessionState,
        *,
        timezone_name: str,
    ) -> datetime | None:
        latest: datetime | None = None
        for field_name in _SESSION_STATE_TIMESTAMP_FIELDS:
            candidate = self._coerce_datetime(
                getattr(session, field_name),
                timezone_name=timezone_name,
            )
            latest = self._later_datetime(latest, candidate)
        latest = self._later_datetime(
            latest,
            self._latest_known_timestamp_from_container(session.work_segments, timezone_name=timezone_name),
        )
        latest = self._later_datetime(
            latest,
            self._latest_known_timestamp_from_container(session.metadata, timezone_name=timezone_name),
        )
        return latest

    def _latest_known_timestamp_from_container(
        self,
        value: Any,
        *,
        timezone_name: str,
    ) -> datetime | None:
        latest: datetime | None = None
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and self._should_scan_timestamp_key(key):
                    candidate = self._coerce_datetime(
                        item if isinstance(item, str) else None,
                        timezone_name=timezone_name,
                    )
                    latest = self._later_datetime(latest, candidate)
                latest = self._later_datetime(
                    latest,
                    self._latest_known_timestamp_from_container(item, timezone_name=timezone_name),
                )
            return latest
        if isinstance(value, list):
            for item in value:
                latest = self._later_datetime(
                    latest,
                    self._latest_known_timestamp_from_container(item, timezone_name=timezone_name),
                )
        return latest

    def _later_datetime(
        self,
        current: datetime | None,
        candidate: datetime | None,
    ) -> datetime | None:
        if candidate is None:
            return current
        if current is None or candidate > current:
            return candidate
        return current

    def _build_weekly_hours_reply_from_rows(
        self,
        user: UserProfile,
        session: SessionState,
        rows: list[dict[str, Any]],
        now: datetime,
    ) -> str:
        return self._build_self_hours_reply_for_range_from_rows(
            user,
            session,
            rows,
            now,
            "this_week",
        )

    def _build_self_hours_reply_for_range_from_rows(
        self,
        user: UserProfile,
        session: SessionState,
        rows: list[dict[str, Any]],
        now: datetime,
        range_key: str,
    ) -> str:
        effective_date_text = self.resolve_user_workday_date(user, now)
        effective_date = datetime.fromisoformat(effective_date_text).date()
        range_label = _SELF_LOOKUP_RANGE_LABELS.get(range_key, "This Week")
        start_date = effective_date
        end_date = effective_date
        if range_key == "this_week":
            start_date = effective_date - timedelta(days=effective_date.weekday())
        elif range_key == "last_week":
            this_week_start = effective_date - timedelta(days=effective_date.weekday())
            start_date = this_week_start - timedelta(days=7)
            end_date = this_week_start - timedelta(days=1)
        elif range_key == "whole_summer":
            archived_dates: list[date] = []
            for row in rows:
                if str(row.get("user_key") or "") != user.user_key:
                    continue
                session_date_text = str(row.get("session_date") or "").strip()
                try:
                    archived_dates.append(datetime.fromisoformat(session_date_text).date())
                except ValueError:
                    continue
            if archived_dates:
                start_date = min(archived_dates)
        elif range_key != "today":
            start_date = effective_date - timedelta(days=effective_date.weekday())
        by_day: dict[str, dict[str, int]] = {}
        for row in rows:
            if str(row.get("user_key") or "") != user.user_key:
                continue
            session_date_text = str(row.get("session_date") or "").strip()
            try:
                session_date = datetime.fromisoformat(session_date_text).date()
            except ValueError:
                continue
            if session_date < start_date or session_date > end_date:
                continue
            bucket = by_day.setdefault(
                session_date_text,
                {
                    "clocked_in_total_seconds": 0,
                    "task_tracked_total_seconds": 0,
                },
            )
            bucket["clocked_in_total_seconds"] += int(row.get("clocked_in_total_seconds") or 0)
            bucket["task_tracked_total_seconds"] += int(row.get("task_tracked_total_seconds") or 0)
        total_clocked_in = sum(item["clocked_in_total_seconds"] for item in by_day.values())
        total_task_tracked = sum(item["task_tracked_total_seconds"] for item in by_day.values())
        if range_key == "today":
            heading = f"{range_label} ({effective_date_text}):"
            empty_text = "I do not have any logged time for you yet today."
        elif range_key == "this_week":
            heading = f"{range_label} so far ({start_date.isoformat()} to {effective_date_text}):"
            empty_text = "I do not have any logged time for you yet this week."
        else:
            heading = f"{range_label} ({start_date.isoformat()} to {end_date.isoformat()}):"
            if range_key == "last_week":
                empty_text = "I do not have any logged time for you from last week."
            else:
                empty_text = "I do not have any logged time for you yet."
        lines = [
            heading,
            f"- Clocked-in time: {self._format_duration(total_clocked_in)}",
            f"- Task-tracked time: {self._format_duration(total_task_tracked)}",
        ]
        nonzero_dates = [
            session_date
            for session_date in sorted(by_day)
            if by_day[session_date]["clocked_in_total_seconds"] > 0
            or by_day[session_date]["task_tracked_total_seconds"] > 0
        ]
        if nonzero_dates and range_key == "whole_summer":
            lines.append(f"- Days with logged time: {len(nonzero_dates)}")
        elif nonzero_dates:
            lines.append("")
            lines.append("Daily breakdown:")
            for session_date_text in nonzero_dates:
                day_totals = by_day[session_date_text]
                day_label = datetime.fromisoformat(session_date_text).strftime("%a %Y-%m-%d")
                lines.append(
                    f"- {day_label}: "
                    f"clocked in {self._format_duration(day_totals['clocked_in_total_seconds'])}; "
                    f"task tracked {self._format_duration(day_totals['task_tracked_total_seconds'])}"
                )
        else:
            lines.append("")
            lines.append(empty_text)
        reminder = self._clock_out_artifacts_reminder_text(session)
        if reminder:
            lines.append("")
            lines.append(reminder)
        return "\n".join(lines)

    def _clock_out_artifacts_reminder_text(self, session: SessionState) -> str | None:
        if session.stage != "awaiting_clock_out_artifacts":
            return None
        reminders: list[str] = []
        if session.awaiting_clock_out_photo:
            reminders.append("the picture")
        if session.awaiting_clock_out_summary:
            reminders.append("the written wrap-up")
        if not reminders:
            return None
        return f"I still need {' and '.join(reminders)} before I close out today."

    def _tracked_time_totals(
        self,
        session: SessionState,
        now: datetime,
    ) -> tuple[int, list[tuple[str | None, str | None, int]]]:
        retro_windows = self._retro_backfill_task_windows(session)
        if retro_windows is not None:
            totals: dict[str, dict[str, Any]] = {}
            total_seconds = 0
            for window in retro_windows:
                if not isinstance(window, dict):
                    continue
                seconds = self._retro_task_window_seconds(window)
                if seconds <= 0:
                    continue
                task_id = str(window.get("task_id") or "") or None
                task_name = str(window.get("task_name") or "") or None
                bucket_key = task_id or task_name or "unknown-task"
                bucket = totals.setdefault(
                    bucket_key,
                    {
                        "task_id": task_id,
                        "task_name": task_name,
                        "seconds": 0,
                    },
                )
                bucket["seconds"] += seconds
                total_seconds += seconds
            ordered = sorted(
                (
                    (
                        item.get("task_id"),
                        item.get("task_name"),
                        int(item.get("seconds") or 0),
                    )
                    for item in totals.values()
                ),
                key=lambda item: item[2],
                reverse=True,
            )
            return total_seconds, ordered
        totals: dict[str, dict[str, Any]] = {}
        total_seconds = 0
        entries: list[dict[str, Any]] = []
        history = session.metadata.get("clickup_time_tracking_history")
        if isinstance(history, list):
            entries.extend(item for item in history if isinstance(item, dict))
        current = session.metadata.get("clickup_time_tracking")
        if isinstance(current, dict):
            entries.append(current)
        for entry in entries:
            seconds = self._tracking_entry_seconds(entry, now)
            if seconds <= 0:
                continue
            task_id = str(entry.get("task_id") or "") or None
            task_name = str(entry.get("task_name") or "") or None
            bucket_key = task_id or task_name or "unknown-task"
            bucket = totals.setdefault(
                bucket_key,
                {
                    "task_id": task_id,
                    "task_name": task_name,
                    "seconds": 0,
                },
            )
            bucket["seconds"] += seconds
            total_seconds += seconds
        ordered = sorted(
            (
                (
                    item.get("task_id"),
                    item.get("task_name"),
                    int(item.get("seconds") or 0),
                )
                for item in totals.values()
            ),
            key=lambda item: item[2],
            reverse=True,
        )
        return total_seconds, ordered

    def _work_segment_total_seconds(
        self,
        session: SessionState,
        now: datetime,
    ) -> int:
        total_seconds = 0
        for start_dt, end_dt in self._work_segment_bounds(session, now):
            total_seconds += max(0, int((end_dt - start_dt).total_seconds()))
        return total_seconds

    def _work_segment_bounds(
        self,
        session: SessionState,
        now: datetime,
    ) -> list[tuple[datetime, datetime]]:
        bounds: list[tuple[datetime, datetime]] = []
        for segment in session.work_segments:
            if not isinstance(segment, dict):
                continue
            started_at = str(segment.get("clocked_in_at") or "").strip()
            if not started_at:
                continue
            closed_at = str(segment.get("clocked_out_at") or "").strip() or None
            start_dt = self._coerce_datetime(started_at)
            end_dt = self._coerce_datetime(closed_at) or now
            if start_dt is None:
                continue
            if start_dt.tzinfo is None and end_dt.tzinfo is not None:
                start_dt = start_dt.replace(tzinfo=end_dt.tzinfo)
            elif start_dt.tzinfo is not None and end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=start_dt.tzinfo)
            if end_dt > start_dt:
                bounds.append((start_dt, end_dt))
        return sorted(bounds, key=lambda item: item[0])

    def _unpaid_lunch_deduction_seconds(
        self,
        session: SessionState,
        now: datetime,
        gross_seconds: int,
    ) -> int:
        if gross_seconds <= 0:
            return 0
        work_bounds = self._work_segment_bounds(session, now)
        if not work_bounds:
            return 0
        overlaps: list[tuple[datetime, datetime]] = []
        for lunch_start, lunch_end in self._recorded_lunch_window_bounds(session, now):
            for work_start, work_end in work_bounds:
                comparable_lunch_start, comparable_lunch_end = self._align_datetime_pair(
                    lunch_start,
                    lunch_end,
                    reference=work_start,
                )
                comparable_work_start, comparable_work_end = self._align_datetime_pair(
                    work_start,
                    work_end,
                    reference=comparable_lunch_start,
                )
                overlap_start = max(comparable_lunch_start, comparable_work_start)
                overlap_end = min(comparable_lunch_end, comparable_work_end)
                if overlap_end > overlap_start:
                    overlaps.append((overlap_start, overlap_end))
        if not overlaps:
            return 0
        merged: list[tuple[datetime, datetime]] = []
        for start_dt, end_dt in sorted(overlaps, key=lambda item: item[0]):
            if not merged or start_dt > merged[-1][1]:
                merged.append((start_dt, end_dt))
                continue
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end_dt))
        deducted_seconds = sum(
            max(0, int((end_dt - start_dt).total_seconds()))
            for start_dt, end_dt in merged
        )
        return min(gross_seconds, deducted_seconds)

    def _recorded_lunch_window_bounds(
        self,
        session: SessionState,
        now: datetime,
    ) -> list[tuple[datetime, datetime]]:
        raw_windows: list[dict[str, Any]] = []
        stored_windows = session.metadata.get(_LUNCH_WINDOWS_KEY)
        if isinstance(stored_windows, list):
            raw_windows.extend(window for window in stored_windows if isinstance(window, dict))
        retro = session.metadata.get(_RETRO_HOURS_BACKFILL_METADATA_KEY)
        retro_windows = retro.get("lunch_windows") if isinstance(retro, dict) else None
        if isinstance(retro_windows, list):
            raw_windows.extend(window for window in retro_windows if isinstance(window, dict))
        legacy_start = str(session.metadata.get("lunch_started_at") or "").strip()
        legacy_end = str(session.metadata.get("lunch_ended_at") or "").strip()
        if legacy_start:
            raw_windows.append(
                {
                    "started_at": legacy_start,
                    "ended_at": legacy_end or None,
                }
            )

        bounds: list[tuple[datetime, datetime]] = []
        seen: set[tuple[str, str]] = set()
        for window in raw_windows:
            started_at = str(window.get("started_at") or window.get("start_at") or "").strip()
            ended_at = str(
                window.get("ended_at")
                or window.get("end_at")
                or window.get("closed_at")
                or ""
            ).strip()
            start_dt = self._coerce_datetime(started_at)
            end_dt = self._coerce_datetime(ended_at) if ended_at else now
            if start_dt is None or end_dt is None:
                continue
            start_dt, end_dt = self._align_datetime_pair(start_dt, end_dt, reference=now)
            if end_dt <= start_dt:
                continue
            key = (start_dt.isoformat(), end_dt.isoformat())
            if key in seen:
                continue
            seen.add(key)
            bounds.append((start_dt, end_dt))
        return sorted(bounds, key=lambda item: item[0])

    def _align_datetime_pair(
        self,
        start_dt: datetime,
        end_dt: datetime,
        *,
        reference: datetime,
    ) -> tuple[datetime, datetime]:
        if start_dt.tzinfo is None and reference.tzinfo is not None:
            start_dt = start_dt.replace(tzinfo=reference.tzinfo)
        elif start_dt.tzinfo is not None and reference.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=None)
        if end_dt.tzinfo is None and start_dt.tzinfo is not None:
            end_dt = end_dt.replace(tzinfo=start_dt.tzinfo)
        elif end_dt.tzinfo is not None and start_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=None)
        return start_dt, end_dt

    def _record_lunch_window_start(self, session: SessionState, now: datetime) -> None:
        windows = session.metadata.get(_LUNCH_WINDOWS_KEY)
        if not isinstance(windows, list):
            windows = []
        previous_start = str(session.metadata.get("lunch_started_at") or "").strip()
        previous_end = str(session.metadata.get("lunch_ended_at") or "").strip()
        if previous_start and previous_end and not any(
            isinstance(window, dict)
            and str(window.get("started_at") or "") == previous_start
            for window in windows
        ):
            windows.append(
                {
                    "started_at": previous_start,
                    "ended_at": previous_end,
                    "source": "legacy_lunch_record",
                }
            )
        started_at = now.isoformat()
        if not any(
            isinstance(window, dict)
            and str(window.get("started_at") or "") == started_at
            for window in windows
        ):
            windows.append(
                {
                    "started_at": started_at,
                    "ended_at": None,
                    "source": "runtime_lunch_break",
                }
            )
        session.metadata[_LUNCH_WINDOWS_KEY] = windows

    def _record_lunch_window_end(self, session: SessionState, now: datetime) -> None:
        windows = session.metadata.get(_LUNCH_WINDOWS_KEY)
        if not isinstance(windows, list):
            windows = []
        started_at = str(session.metadata.get("lunch_started_at") or "").strip()
        for window in reversed(windows):
            if not isinstance(window, dict) or str(window.get("ended_at") or "").strip():
                continue
            if started_at and str(window.get("started_at") or "").strip() != started_at:
                continue
            window["ended_at"] = now.isoformat()
            session.metadata[_LUNCH_WINDOWS_KEY] = windows
            return
        if started_at:
            windows.append(
                {
                    "started_at": started_at,
                    "ended_at": now.isoformat(),
                    "source": "runtime_lunch_break",
                }
            )
            session.metadata[_LUNCH_WINDOWS_KEY] = windows

    def _refresh_session_time_summary(
        self,
        session: SessionState,
        now: datetime,
    ) -> None:
        self._ensure_work_segments_consistency(session)
        gross_clocked_in_total_seconds = self._work_segment_total_seconds(session, now)
        unpaid_lunch_deducted_seconds = self._unpaid_lunch_deduction_seconds(
            session,
            now,
            gross_clocked_in_total_seconds,
        )
        clocked_in_total_seconds = max(0, gross_clocked_in_total_seconds - unpaid_lunch_deducted_seconds)
        tracked_total_seconds, tracked_tasks = self._tracked_time_totals(session, now)
        retro_windows = self._retro_backfill_task_windows(session)
        if retro_windows is not None:
            timer_running = any(
                isinstance(window, dict)
                and bool(window.get("task_id") or window.get("task_name"))
                and not str(window.get("ended_at") or "").strip()
                for window in retro_windows
            )
        else:
            current_tracking = session.metadata.get("clickup_time_tracking")
            current_tracking = current_tracking if isinstance(current_tracking, dict) else {}
            timer_running = bool(current_tracking) and not current_tracking.get("closed_at") and bool(
                current_tracking.get("task_id")
            )
        session.time_summary = {
            "gross_clocked_in_total_seconds": gross_clocked_in_total_seconds,
            "gross_clocked_in_total_human": self._format_duration(gross_clocked_in_total_seconds),
            "unpaid_lunch_deducted_seconds": unpaid_lunch_deducted_seconds,
            "unpaid_lunch_deducted_human": self._format_duration(unpaid_lunch_deducted_seconds),
            "clocked_in_total_seconds": clocked_in_total_seconds,
            "clocked_in_total_human": self._format_duration(clocked_in_total_seconds),
            "task_tracked_total_seconds": tracked_total_seconds,
            "task_tracked_total_human": self._format_duration(tracked_total_seconds),
            "work_segment_count": len(session.work_segments),
            "has_open_work_segment": any(
                isinstance(segment, dict) and segment.get("clocked_in_at") and not segment.get("clocked_out_at")
                for segment in session.work_segments
            ),
            "active_task_timer_running": timer_running,
            "time_by_task": [
                {
                    "task_id": task_id,
                    "task_name": task_name,
                    "seconds": seconds,
                    "human_duration": self._format_duration(seconds),
                }
                for task_id, task_name, seconds in tracked_tasks
            ],
        }

    def _retro_backfill_task_windows(self, session: SessionState) -> list[dict[str, Any]] | None:
        retro = session.metadata.get(_RETRO_HOURS_BACKFILL_METADATA_KEY)
        if not isinstance(retro, dict):
            return None
        task_windows = retro.get("task_windows")
        if task_windows is None:
            return None
        if not isinstance(task_windows, list):
            return []
        return [window for window in task_windows if isinstance(window, dict)]

    def _retro_task_window_seconds(self, window: dict[str, Any]) -> int:
        duration_seconds = window.get("duration_seconds")
        if isinstance(duration_seconds, int) and duration_seconds > 0:
            return duration_seconds
        started_at = str(window.get("started_at") or "").strip()
        ended_at = str(window.get("ended_at") or "").strip()
        if not started_at or not ended_at:
            return 0
        end_dt = self._coerce_datetime(ended_at)
        if end_dt is None:
            return 0
        return self._elapsed_seconds_between(started_at, end_dt)

    def _tracking_entry_seconds(self, entry: dict[str, Any], now: datetime) -> int:
        if not isinstance(entry, dict):
            return 0
        duration_seconds = entry.get("duration_seconds")
        if isinstance(duration_seconds, int) and duration_seconds > 0:
            return duration_seconds
        started_at = str(entry.get("started_at") or "")
        if not started_at:
            return 0
        end_dt = self._coerce_datetime(str(entry.get("closed_at") or "")) or now
        return self._elapsed_seconds_between(started_at, end_dt)

    def _elapsed_seconds_between(self, started_at: str, end_dt: datetime) -> int:
        start_dt = self._coerce_datetime(started_at)
        if not start_dt:
            return 0
        if start_dt.tzinfo is None and end_dt.tzinfo is not None:
            start_dt = start_dt.replace(tzinfo=end_dt.tzinfo)
        elif start_dt.tzinfo is not None and end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=start_dt.tzinfo)
        return max(0, int((end_dt - start_dt).total_seconds()))

    def _coerce_datetime(
        self,
        raw_value: str | None,
        *,
        timezone_name: str | None = None,
    ) -> datetime | None:
        if not raw_value:
            return None
        try:
            parsed = datetime.fromisoformat(raw_value)
        except ValueError:
            return None
        if parsed.tzinfo is None and timezone_name:
            return localize_datetime(parsed, timezone_name)
        return parsed

    def _coerce_datetime_for_reference(
        self,
        raw_value: str | None,
        *,
        reference: datetime,
        timezone_name: str | None = None,
    ) -> datetime | None:
        parsed = self._coerce_datetime(raw_value, timezone_name=timezone_name)
        if not parsed:
            return None
        if reference.tzinfo is None and parsed.tzinfo is not None:
            return parsed.replace(tzinfo=None)
        if reference.tzinfo is not None and parsed.tzinfo is None:
            if timezone_name:
                return localize_datetime(parsed, timezone_name)
            return parsed.replace(tzinfo=reference.tzinfo)
        return parsed

    def _format_duration(self, total_seconds: int) -> str:
        if total_seconds <= 0:
            return "0m"
        minutes = total_seconds // 60
        hours, minutes = divmod(minutes, 60)
        if hours and minutes:
            return f"{hours}h {minutes}m"
        if hours:
            return f"{hours}h"
        return f"{minutes}m"

    def _clone_session_state(self, session: SessionState) -> SessionState:
        return deepcopy(session)

    def _build_state_machine_snapshot(self, session: SessionState, now: datetime) -> dict[str, Any]:
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        prompt = prompt if isinstance(prompt, dict) else {}
        prompt_draft = prompt.get("draft")
        prompt_draft = prompt_draft if isinstance(prompt_draft, dict) else {}
        reviews = self._pending_admin_reviews(session)
        review = reviews[0] if reviews else {}
        unblocker = self._pending_admin_unblocker_task(session) or {}
        unblocker_draft = unblocker.get("draft") if isinstance(unblocker, dict) else {}
        unblocker_draft = unblocker_draft if isinstance(unblocker_draft, dict) else {}
        tracking = session.metadata.get("clickup_time_tracking")
        tracking = tracking if isinstance(tracking, dict) else {}
        tracking_history = session.metadata.get("clickup_time_tracking_history")
        tracking_history = tracking_history if isinstance(tracking_history, list) else []
        timer_running = bool(tracking) and not tracking.get("closed_at") and bool(tracking.get("task_id"))
        timer_started_at = str(tracking.get("started_at") or "") or None
        timer_elapsed_seconds = self._elapsed_seconds_between(timer_started_at, now) if timer_running and timer_started_at else 0
        tracked_total_seconds, tracked_tasks = self._tracked_time_totals(session, now)
        recently_closed = session.metadata.get("recently_closed_clickup_tasks")
        recently_closed = recently_closed if isinstance(recently_closed, list) else []
        created_blocker_task_ids = session.metadata.get("created_blocker_task_ids")
        created_blocker_task_ids = created_blocker_task_ids if isinstance(created_blocker_task_ids, list) else []
        next_task_suggestions = session.metadata.get("next_task_suggestion_ids")
        next_task_suggestions = next_task_suggestions if isinstance(next_task_suggestions, list) else []
        candidate_task_ids = session.metadata.get("clickup_candidate_task_ids")
        candidate_task_ids = candidate_task_ids if isinstance(candidate_task_ids, list) else []
        task_onboarding = {
            "plan": str(session.metadata.get("task_onboarding_plan") or "") or None,
            "tangible_result": str(session.metadata.get("task_onboarding_tangible_result") or "") or None,
            "necessity": str(session.metadata.get("task_onboarding_necessity") or "") or None,
            "effectiveness": str(session.metadata.get("task_onboarding_effectiveness") or "") or None,
            "estimated_duration": str(session.metadata.get("task_onboarding_estimated_duration") or "") or None,
            "reconsider_threshold": str(session.metadata.get("task_onboarding_reconsider_threshold") or "") or None,
            "fallback_plan": str(session.metadata.get("task_onboarding_fallback_plan") or "") or None,
            "summary": str(session.metadata.get("last_task_onboarding_summary") or "") or None,
        }
        last_review_resolution = session.metadata.get("last_admin_review_resolution")
        last_review_resolution = last_review_resolution if isinstance(last_review_resolution, dict) else {}
        last_unblocker_resolution = session.metadata.get("last_admin_unblocker_resolution")
        last_unblocker_resolution = last_unblocker_resolution if isinstance(last_unblocker_resolution, dict) else {}
        direct_help_targets = session.metadata.get("last_direct_admin_help_targets")
        direct_help_targets = direct_help_targets if isinstance(direct_help_targets, list) else []
        work_segments = [
            {
                "clocked_in_at": str(segment.get("clocked_in_at") or "") or None,
                "clocked_out_at": str(segment.get("clocked_out_at") or "") or None,
            }
            for segment in session.work_segments
            if isinstance(segment, dict)
        ]
        return {
            "stage": session.stage,
            "workday": {
                "session_date": session.session_date,
                "work_segment_count": len(work_segments),
                "work_segments": work_segments,
            },
            "timestamps": {
                "first_sign_of_life_at": session.first_sign_of_life_at,
                "clocked_in_at": session.clocked_in_at,
                "intake_completed_at": session.intake_completed_at,
                "last_contact_at": session.last_contact_at,
                "last_user_message_at": session.last_user_message_at,
                "last_outbound_at": session.last_outbound_at,
                "last_clock_in_prompt_at": session.last_clock_in_prompt_at,
                "last_follow_up_at": session.last_follow_up_at,
                "last_clickup_sync_at": session.last_clickup_sync_at,
                "stuck_since": session.stuck_since,
                "stuck_alerted_at": session.stuck_alerted_at,
                "clocked_out_at": session.clocked_out_at,
            },
            "flags": {
                "awaiting_start_photo": session.awaiting_start_photo,
                "awaiting_clock_out_photo": session.awaiting_clock_out_photo,
                "awaiting_clock_out_summary": session.awaiting_clock_out_summary,
                "pending_clickup_sync": session.pending_clickup_sync,
            },
            "latest": {
                "plan": session.latest_plan,
                "status": session.latest_status,
                "blocker": session.latest_blocker,
                "blocker_state": self._blocker_state(session),
                "feedback": session.latest_feedback,
            },
            "task_onboarding": task_onboarding,
            "active_task": {
                "task_id": self._active_task_id(session),
                "task_name": str(session.metadata.get("active_clickup_task_name") or "") or None,
                "selection_reason": str(session.metadata.get("clickup_selection_reason") or "") or None,
                "candidate_task_ids": candidate_task_ids,
                "recently_closed_task_ids": [
                    str(item.get("task_id") or "")
                    for item in recently_closed
                    if isinstance(item, dict) and str(item.get("task_id") or "")
                ],
            },
            "prompt": {
                "type": str(prompt.get("type") or "") or None,
                "step": str(prompt.get("step") or "") or None,
                "source": str(prompt.get("source") or "") or None,
                "reason": str(prompt.get("reason") or "") or None,
                "task_id": str(prompt.get("task_id") or "") or None,
                "task_name": str(prompt.get("task_name") or "") or None,
                "probe_id": str(prompt.get("probe_id") or "") or None,
                "question_text": str(prompt.get("question_text") or "") or None,
                "probe_round": int(prompt.get("probe_round") or 0) if "probe_round" in prompt else None,
                "original_reply_message_count": len(prompt.get("original_reply_message_ids") or []),
                "probe_exchange_count": len(prompt.get("probe_exchange") or []),
                "subscribed_admin_count": len(prompt.get("subscribed_admin_ids") or []),
                "last_activity_at": str(prompt.get("last_activity_at") or "") or None,
                "closure_reason": str(prompt.get("closure_reason") or "") or None,
                "blocker_text": str(prompt.get("blocker_text") or "") or None,
                "help_decision": str(prompt.get("help_decision") or "") or None,
                "blocked_state_after_decline": str(prompt.get("blocked_state_after_decline") or "") or None,
                "needs_summary": bool(prompt.get("needs_summary")) if "needs_summary" in prompt else None,
                "needs_photo": bool(prompt.get("needs_photo")) if "needs_photo" in prompt else None,
                "requested_at": str(prompt.get("requested_at") or "") or None,
                "draft_title": str(prompt_draft.get("title") or "") or None,
                "draft_priority": str(prompt_draft.get("priority") or "") or None,
                "draft_assignee_label": str(prompt_draft.get("assignee_label") or "") or None,
                "draft_plan": str(prompt_draft.get("plan") or "") or None,
                "draft_tangible_result": str(prompt_draft.get("tangible_result") or "") or None,
                "draft_necessity": str(prompt_draft.get("necessity") or "") or None,
                "draft_effectiveness": str(prompt_draft.get("effectiveness") or "") or None,
                "draft_estimated_duration": str(prompt_draft.get("estimated_duration") or "") or None,
                "draft_reconsider_threshold": str(prompt_draft.get("reconsider_threshold") or "") or None,
                "draft_fallback_plan": str(prompt_draft.get("fallback_plan") or "") or None,
                "draft_admin_feedback": str(prompt_draft.get("admin_feedback") or "") or None,
            },
            "review": {
                "pending": bool(review),
                "pending_count": len(reviews),
                "pending_reviews": [
                    {
                        "task_id": str(item.get("task_id") or "") or None,
                        "task_name": str(item.get("task_name") or "") or None,
                        "submitted_at": str(item.get("submitted_at") or "") or None,
                        "completion_photo_count": len(self._coerce_path_list(item.get("completion_photo_paths"))),
                        "completion_summary": str(item.get("completion_summary") or "") or None,
                    }
                    for item in reviews
                    if isinstance(item, dict)
                ],
                "task_id": str(review.get("task_id") or "") or None,
                "task_name": str(review.get("task_name") or "") or None,
                "submitted_at": str(review.get("submitted_at") or "") or None,
                "completion_photo_count": len(self._coerce_path_list(review.get("completion_photo_paths"))),
                "completion_summary": str(review.get("completion_summary") or "") or None,
                "last_decision": str(last_review_resolution.get("decision") or "") or None,
                "last_decision_at": str(last_review_resolution.get("at") or "") or None,
            },
            "unblocker": {
                "pending": bool(unblocker),
                "title": str(unblocker_draft.get("title") or "") or None,
                "assignee_label": str(unblocker_draft.get("assignee_label") or "") or None,
                "assignee_id": str(unblocker_draft.get("assignee_id") or "") or None,
                "priority": str(unblocker_draft.get("priority") or "") or None,
                "due_date_text": str(unblocker_draft.get("due_date_text") or "") or None,
                "submitted_at": str(unblocker.get("submitted_at") or "") or None,
                "last_decision": str(last_unblocker_resolution.get("decision") or "") or None,
                "last_decision_at": str(last_unblocker_resolution.get("at") or "") or None,
                "created_task_ids": [str(item) for item in created_blocker_task_ids if isinstance(item, str)],
            },
            "timer": {
                "running": timer_running,
                "source": str(tracking.get("source") or "") or None,
                "task_id": str(tracking.get("task_id") or "") or None,
                "task_name": str(tracking.get("task_name") or "") or None,
                "started_at": timer_started_at,
                "elapsed_seconds": timer_elapsed_seconds,
                "history_count": len(tracking_history),
                "tracked_total_seconds": tracked_total_seconds,
                "tracked_task_count": len(tracked_tasks),
            },
            "artifacts": {
                "before_start_photo_count": len(self._coerce_path_list(session.metadata.get("before_start_photo_paths"))),
                "progress_photo_count": len(self._coerce_path_list(session.metadata.get("progress_photo_paths"))),
                "clock_out_photo_count": len(self._coerce_path_list(session.metadata.get("clock_out_photo_paths"))),
                "clock_out_summary_message_id": str(session.metadata.get("clock_out_summary_message_id") or "") or None,
            },
            "automation": {
                "follow_up_index": int(session.metadata.get("follow_up_index") or 0),
                "pending_follow_up_message_id": str((self._pending_follow_up(session) or {}).get("message_id") or "") or None,
                "pending_follow_up_question": str((self._pending_follow_up(session) or {}).get("question_text") or "") or None,
                "follow_up_aggregation_reply_count": len((self._pending_follow_up_aggregation(session) or {}).get("reply_message_ids") or []),
                "follow_up_aggregation_last_reply_at": str((self._pending_follow_up_aggregation(session) or {}).get("last_reply_at") or "") or None,
                "progress_probe_history_count": len(self._progress_probe_history(session)),
                "last_task_onboarding_prompt_at": str(session.metadata.get("last_task_onboarding_prompt_at") or "") or None,
                "last_task_onboarding_completed_at": str(session.metadata.get("last_task_onboarding_completed_at") or "") or None,
                "next_task_suggestion_ids": [str(item) for item in next_task_suggestions if isinstance(item, str)],
                "lunch_started_at": str(session.metadata.get("lunch_started_at") or "") or None,
                "lunch_ended_at": str(session.metadata.get("lunch_ended_at") or "") or None,
                "lunch_last_prompt_at": str(session.metadata.get("lunch_last_prompt_at") or "") or None,
                "lunch_confirmation_requested_at": str(session.metadata.get(_LUNCH_CONFIRMATION_REQUESTED_AT_KEY) or "") or None,
                "lunch_resume_requested_at": str(session.metadata.get("lunch_resume_requested_at") or "") or None,
                "lunch_resume_stage": str(session.metadata.get("lunch_resume_stage") or "") or None,
                "lunch_resume_task_id": str(session.metadata.get("lunch_resume_task_id") or "") or None,
                "lunch_resume_task_name": str(session.metadata.get("lunch_resume_task_name") or "") or None,
                "auto_clock_out_at": str(session.metadata.get("auto_clock_out_at") or "") or None,
                "auto_clock_out_reference_at": str(session.metadata.get("auto_clock_out_reference_at") or "") or None,
                "auto_clock_out_reason": str(session.metadata.get("auto_clock_out_reason") or "") or None,
                "auto_clock_out_note": str(session.metadata.get("auto_clock_out_note") or "") or None,
            },
            "audit": {
                "last_clickup_status": str(session.metadata.get("last_clickup_status") or "") or None,
                "last_direct_admin_help_request_at": str(session.metadata.get("last_direct_admin_help_request_at") or "") or None,
                "last_direct_admin_help_targets": [str(item) for item in direct_help_targets if isinstance(item, str)],
                "blocker_state": self._blocker_state(session),
                "blocker_help_decision_at": str(session.metadata.get(_BLOCKER_HELP_DECISION_AT_KEY) or "") or None,
                "last_resolved_blocker": str(session.metadata.get("last_resolved_blocker") or "") or None,
                "last_unblocked_at": str(session.metadata.get("last_unblocked_at") or "") or None,
                "last_unblocked_source": str(session.metadata.get("last_unblocked_source") or "") or None,
                "last_unblocked_note": str(session.metadata.get("last_unblocked_note") or "") or None,
                "cancelled_unblocker_task_after_recovery_at": str(session.metadata.get("cancelled_unblocker_task_after_recovery_at") or "") or None,
                "session_reactivated_at": str(session.metadata.get("session_reactivated_at") or "") or None,
                "metadata_keys": sorted(session.metadata.keys()),
            },
        }

    def _diff_state_machine_snapshots(
        self,
        previous_snapshot: dict[str, Any],
        current_snapshot: dict[str, Any],
    ) -> list[str]:
        previous_flat = self._flatten_state_machine_snapshot(previous_snapshot)
        current_flat = self._flatten_state_machine_snapshot(current_snapshot)
        changed_fields: list[str] = []
        for key in sorted(set(previous_flat) | set(current_flat)):
            if previous_flat.get(key) != current_flat.get(key):
                changed_fields.append(key)
        return changed_fields

    def _flatten_state_machine_snapshot(
        self,
        snapshot: dict[str, Any],
        prefix: str = "",
    ) -> dict[str, Any]:
        flattened: dict[str, Any] = {}
        for key, value in snapshot.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                flattened.update(self._flatten_state_machine_snapshot(value, path))
            else:
                flattened[path] = value
        return flattened

    def _coerce_path_list(self, raw_value: Any) -> list[str]:
        if not isinstance(raw_value, list):
            return []
        return [str(item) for item in raw_value if isinstance(item, str) and item]

    def _blocker_state(self, session: SessionState) -> str | None:
        value = session.metadata.get(_BLOCKER_STATE_KEY)
        if isinstance(value, str) and value:
            return value
        return None

    def _set_blocker_state(self, session: SessionState, state: str, now: datetime) -> None:
        session.metadata[_BLOCKER_STATE_KEY] = state
        session.metadata[_BLOCKER_HELP_DECISION_AT_KEY] = now.isoformat()

    def _clear_blocker_state(self, session: SessionState, *, mark_not_blocked: bool) -> None:
        session.stuck_since = None
        session.stuck_alerted_at = None
        session.latest_blocker = None
        if mark_not_blocked:
            session.metadata[_BLOCKER_STATE_KEY] = "not_blocked"
        else:
            session.metadata.pop(_BLOCKER_STATE_KEY, None)

    def _signals_blocked_status(self, signals: Any) -> bool:
        return bool(getattr(signals, "blocked_status", False) or getattr(signals, "stuck", False))

    def _signals_help_requested(self, signals: Any) -> bool:
        return bool(getattr(signals, "help_requested", False))

    def _signals_help_declined(self, signals: Any) -> bool:
        return bool(getattr(signals, "help_declined", False))

    def _update_blocker_tracking_from_inbound(
        self,
        session: SessionState,
        inbound: MessageRecord,
        signals: Any,
        now: datetime,
    ) -> None:
        if not self._should_track_stuck_signal(session):
            return
        if getattr(signals, "recovered", False):
            self._set_blocker_state(session, "not_blocked", now)
            session.stuck_since = None
            session.stuck_alerted_at = None
            return
        if not self._signals_blocked_status(signals):
            return
        blocker_text = inbound.content.strip()
        previous_blocker = (session.latest_blocker or "").strip()
        if blocker_text and blocker_text != previous_blocker:
            session.latest_blocker = blocker_text
            session.metadata.pop(_BLOCKER_STATE_KEY, None)
        elif blocker_text and not session.latest_blocker:
            session.latest_blocker = blocker_text
        if not session.stuck_since:
            session.stuck_since = now.isoformat()
            session.stuck_alerted_at = None

    def _should_offer_blocker_resolution(self, session: SessionState, text: str, signals: Any) -> bool:
        if session.stage != "active":
            return False
        if session.metadata.get(_CLICKUP_PROMPT_KEY):
            return False
        blocked_status = self._signals_blocked_status(signals)
        help_requested = self._signals_help_requested(signals)
        if not blocked_status and not help_requested:
            return False
        blocker_state = self._blocker_state(session)
        if blocker_state == "blocked_no_help":
            return help_requested or bool(self._resolve_requested_admins(text)) or self._looks_like_unblocker_task_request(text)
        if blocker_state == "blocked_help_requested":
            return bool(self._resolve_requested_admins(text)) or self._looks_like_unblocker_task_request(text)
        return True

    def _looks_like_help_request(self, text: str) -> bool:
        lowered = " ".join(text.strip().lower().split())
        return any(
            phrase in lowered
            for phrase in (
                "need help",
                "help me",
                "can someone help",
                "could someone help",
                "admin help",
                "ask admin",
            )
        )

    def _looks_like_help_declined(self, text: str) -> bool:
        return any(
            phrase in text
            for phrase in (
                "no help",
                "don't need help",
                "dont need help",
                "do not need help",
                "don't need any help",
                "dont need any help",
                "do not need any help",
                "nah i'm good",
                "nah im good",
                "i'm good",
                "im good",
                "i am good",
            )
        )

    def _looks_like_not_blocked_reply(self, text: str) -> bool:
        return any(
            phrase in text
            for phrase in (
                "not blocked",
                "not actually blocked",
                "never mind",
                "nevermind",
                "i'm fine",
                "im fine",
                "i am fine",
                "all good",
            )
        )

    def _looks_like_keep_blocker_logged(self, text: str) -> bool:
        return any(
            phrase in text
            for phrase in (
                "keep logged",
                "keep it logged",
                "keep blocker logged",
                "keep the blocker logged",
                "keep blocker",
                "log it",
            )
        )

    def _looks_like_clear_blocker(self, text: str) -> bool:
        return any(
            phrase in text
            for phrase in (
                "clear it",
                "clear blocker",
                "clear the blocker",
                "remove it",
                "remove blocker",
                "don't log it",
                "dont log it",
            )
        )

    def _signal_details(self, signals: Any) -> dict[str, bool]:
        return {
            "clocked_in": bool(getattr(signals, "clocked_in", False)),
            "clocking_out": bool(getattr(signals, "clocking_out", False)),
            "blocked_status": self._signals_blocked_status(signals),
            "help_requested": self._signals_help_requested(signals),
            "help_declined": self._signals_help_declined(signals),
            "stuck": bool(getattr(signals, "stuck", False)),
            "recovered": bool(getattr(signals, "recovered", False)),
            "starting_lunch": bool(getattr(signals, "starting_lunch", False)),
            "ending_lunch": bool(getattr(signals, "ending_lunch", False)),
            "starting_short_rest": bool(
                getattr(signals, "starting_short_rest", False)
            ),
            "ending_short_rest": bool(
                getattr(signals, "ending_short_rest", False)
            ),
        }

    def _excerpt_text(self, text: str, limit: int = 160) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3].rstrip() + "..."

    def _last_inbound_check_in_at(
        self,
        user: UserProfile,
        session: SessionState,
        *,
        reference: datetime | None = None,
    ) -> datetime | None:
        timezone_name = self.resolve_user_timezone_name(user)
        for value in (session.last_user_message_at, session.last_contact_at, session.clocked_in_at):
            if reference is not None:
                parsed = self._coerce_datetime_for_reference(
                    value,
                    reference=reference,
                    timezone_name=timezone_name,
                )
            else:
                parsed = self._coerce_datetime(value, timezone_name=timezone_name)
            if parsed:
                return parsed
        return None

    def _inactivity_auto_clock_out_reference_at(
        self,
        user: UserProfile,
        session: SessionState,
        *,
        reference: datetime,
    ) -> datetime | None:
        if not self.config or not session.clocked_in_at or session.clocked_out_at:
            return None
        if session.stage in {"on_lunch_break", "awaiting_clock_out_artifacts"}:
            return None
        return self._last_inbound_check_in_at(user, session, reference=reference)

    def _auto_clock_out_warning_state(self, session: SessionState) -> dict[str, Any] | None:
        raw = session.metadata.get(_AUTO_CLOCK_OUT_WARNING_KEY)
        if not isinstance(raw, dict):
            return None
        reference_at = str(raw.get("reference_at") or "").strip()
        warning_sent_at = str(raw.get("warning_sent_at") or "").strip()
        if not reference_at or not warning_sent_at:
            return None
        return raw

    def _message_attachment_paths(self, message: MessageRecord) -> list[str]:
        paths: list[str] = []
        for attachment in message.attachments:
            if attachment.local_path and attachment.local_path not in paths:
                paths.append(attachment.local_path)
        return paths

    def _resolve_existing_paths(self, raw_paths: Any) -> list[Path]:
        if not isinstance(raw_paths, list):
            return []
        paths: list[Path] = []
        for item in raw_paths:
            if isinstance(item, str):
                path = Path(item)
                if path.exists():
                    paths.append(path)
        return paths

    async def _build_next_task_suggestion(
        self,
        user: UserProfile,
        session: SessionState,
    ) -> str | None:
        if not self.clickup or not self.config:
            return None
        messages = self.list_session_messages(user.user_key, session)
        suggestions = await self.clickup.suggest_next_tasks(
            user,
            session,
            messages,
            exclude_task_ids={task_id for task_id in [self._active_task_id(session)] if task_id},
            limit=2,
        )
        if not suggestions:
            return None
        session.metadata["next_task_suggestion_ids"] = [
            str(task.get("id"))
            for task in suggestions
            if task.get("id")
        ]
        best = suggestions[0]
        other = suggestions[1:]
        lines = [
            f"Tomorrow's best unassigned ClickUp candidate looks like `{best.get('name')}`.",
        ]
        if other:
            lines.append(
                "Other likely follow-ups: "
                + ", ".join(f"`{task.get('name')}`" for task in other)
            )
        lines.append("If that feels off, tell me in the morning and I will re-rank it against the latest ClickUp context.")
        return "\n".join(lines)

    def _active_task_id(self, session: SessionState) -> str | None:
        return SessionMetadata(session).active_task_id

    def _clear_active_task_metadata(self, session: SessionState) -> None:
        SessionMetadata(session).clear_active_task()

    def _pending_admin_unblocker_task(self, session: SessionState) -> dict[str, Any] | None:
        value = session.metadata.get("pending_admin_unblocker_task")
        if isinstance(value, dict):
            return value
        return None

    def _pending_admin_task_proposal(self, session: SessionState) -> dict[str, Any] | None:
        value = session.metadata.get("pending_admin_task_proposal")
        return value if isinstance(value, dict) else None

    def _remember_recently_closed_task(
        self,
        session: SessionState,
        task_id: str,
        task_name: str,
        now: datetime,
    ) -> None:
        if not task_id:
            return
        tasks = session.metadata.get("recently_closed_clickup_tasks")
        normalized: list[dict[str, str]] = []
        if isinstance(tasks, list):
            for item in tasks:
                if isinstance(item, dict):
                    existing_id = str(item.get("task_id") or "").strip()
                    if existing_id and existing_id != task_id:
                        normalized.append(
                            {
                                "task_id": existing_id,
                                "task_name": str(item.get("task_name") or "").strip(),
                                "closed_at": str(item.get("closed_at") or "").strip(),
                            }
                        )
        normalized.insert(
            0,
            {
                "task_id": task_id,
                "task_name": task_name,
                "closed_at": now.isoformat(),
            },
        )
        session.metadata["recently_closed_clickup_tasks"] = normalized[:10]

    def _recently_closed_task_ids(self, session: SessionState) -> set[str]:
        tasks = session.metadata.get("recently_closed_clickup_tasks")
        if not isinstance(tasks, list):
            return set()
        task_ids: set[str] = set()
        for item in tasks:
            if isinstance(item, dict):
                task_id = str(item.get("task_id") or "").strip()
                if task_id:
                    task_ids.add(task_id)
        return task_ids

    def _pending_admin_reviews(self, session: SessionState) -> list[dict[str, Any]]:
        return SessionMetadata(session).pending_admin_reviews()

    def _set_pending_admin_reviews(self, session: SessionState, reviews: list[dict[str, Any]]) -> None:
        SessionMetadata(session).set_pending_admin_reviews(reviews)

    def _pending_admin_review(self, session: SessionState) -> dict[str, Any] | None:
        reviews = self._pending_admin_reviews(session)
        return reviews[0] if reviews else None

    def _pending_admin_review_count(self, session: SessionState) -> int:
        return len(self._pending_admin_reviews(session))

    def _match_pending_admin_review(
        self,
        session: SessionState,
        *,
        task_hint: str | None = None,
        task_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        reviews = self._pending_admin_reviews(session)
        if not reviews:
            return None, None
        if task_id:
            for review in reviews:
                if str(review.get("task_id") or "").strip() == task_id.strip():
                    return review, None
            return None, f"I could not find a pending review with task id `{task_id}`."
        if task_hint:
            normalized_hint = self._normalize_identifier_value(task_hint)
            lowered_hint = task_hint.strip().lower()
            for review in reviews:
                review_task_id = str(review.get("task_id") or "")
                review_task_name = str(review.get("task_name") or "")
                if review_task_id and review_task_id.lower() == lowered_hint:
                    return review, None
                normalized_name = self._normalize_identifier_value(review_task_name)
                if normalized_hint and (normalized_name == normalized_hint or normalized_hint in normalized_name):
                    return review, None
            return None, f"I could not match `{task_hint}` to a pending review for this intern."
        if len(reviews) == 1:
            return reviews[0], None
        lines = ["That intern has multiple pending reviews. Add `task=` or `task_id=` to disambiguate:"]
        for review in reviews:
            lines.append(
                f"- {review.get('task_name') or 'unnamed task'}"
                + (f" | id={review.get('task_id')}" if review.get("task_id") else "")
            )
        return None, "\n".join(lines)

    def _has_pending_admin_review(self, session: SessionState) -> bool:
        return self._pending_admin_review_count(session) > 0

    def _normalize_identifier_value(self, value: str) -> str:
        return "".join(ch for ch in value.lower() if ch.isalnum())

    def _normalize_freeform_lookup_text(self, text: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", text.lower()))

    def _looks_like_flexible_self_hours_request(
        self,
        text: str,
        *,
        normalized: str | None = None,
    ) -> bool:
        normalized_text = normalized or self._normalize_freeform_lookup_text(text)
        if not normalized_text:
            return False
        token_set = set(normalized_text.split())
        has_request_signal = "?" in text or normalized_text.startswith(_SELF_LOOKUP_HOURS_REQUEST_PREFIXES)
        if not has_request_signal:
            return False
        has_time_tracking_topic = any(
            phrase in normalized_text
            for phrase in _SELF_LOOKUP_TIME_TRACKING_PHRASES
        ) or ("time" in token_set and bool({"tracked", "tracking", "logged"} & token_set))
        if has_time_tracking_topic:
            return True
        has_clocked_duration_topic = (
            "clocked in" in normalized_text
            and (
                "hours" in token_set
                or normalized_text.startswith(("how many ", "how much ", "how long "))
            )
        )
        if has_clocked_duration_topic:
            return True
        has_work_duration_topic = normalized_text.startswith(("how many ", "how much ", "how long ")) and bool(
            {"worked", "working"} & token_set
        )
        if has_work_duration_topic:
            return True
        if normalized_text.startswith(("how many hours am i at", "what hours am i at")):
            return True
        if "hours" not in token_set:
            return False
        return bool(token_set & _SELF_LOOKUP_HOURS_REPORT_TOKENS)

    def _self_lookup_request_kind(self, text: str) -> str | None:
        normalized = self._normalize_freeform_lookup_text(text)
        if (
            normalized in _SELF_LOOKUP_HOURS_REQUEST_PATTERNS
            or self._looks_like_flexible_self_hours_request(text, normalized=normalized)
        ):
            return "hours"
        if normalized in _SELF_LOOKUP_STATUS_REQUEST_PATTERNS:
            return "status"
        return None

    def _looks_like_self_hours_request(self, text: str) -> bool:
        return self._self_lookup_request_kind(text) == "hours"

    def _looks_like_not_working_today_request(self, text: str) -> bool:
        normalized = self._normalize_freeform_lookup_text(text)
        if not normalized or "today" not in normalized:
            return False
        if "not working on" in normalized:
            return False
        explicit_phrases = (
            "not working today",
            "off today",
            "out today",
            "taking today off",
            "not coming in today",
            "wont be in today",
            "won't be in today",
            "cant work today",
            "can't work today",
            "not able to work today",
            "sick today",
            "home sick today",
        )
        return any(phrase in normalized for phrase in explicit_phrases)

    async def _maybe_start_day_suppression_prompt(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
    ) -> bool:
        if self._day_suppression_active_for_session(session):
            return False
        action = await self._resolve_daily_availability_intent(
            inbound.content,
            stage=session.stage,
        )
        if action != "not_working_today":
            return False
        await self._start_day_suppression_prompt(client, user, session, inbound, now)
        return True

    def _metadata_datetime(
        self,
        session: SessionState,
        key: str,
        *,
        reference: datetime | None = None,
        timezone_name: str | None = None,
    ) -> datetime | None:
        value = session.metadata.get(key)
        if not isinstance(value, str) or not value:
            return None
        if reference is not None:
            return self._coerce_datetime_for_reference(
                value,
                reference=reference,
                timezone_name=timezone_name,
            )
        return self._coerce_datetime(value, timezone_name=timezone_name)

    def _stuck_acknowledgement(self) -> str:
        if self.config and self.config.clickup.mission_board_list_id:
            return (
                "Thanks for flagging it. I can also open a Mission Board blocker task if you want, "
                "so keep an eye out for my next message."
            )
        return "Thanks for flagging it. Tell me the exact blocker when you can, and I will keep an eye on it."

    def _looks_like_task_complete(self, text: str) -> bool:
        lowered = text.lower()
        if not any(hint in lowered for hint in _COMPLETE_HINTS):
            return False
        return not any(hint in lowered for hint in _INCOMPLETE_HINTS)

    def _looks_like_review_cancellation(self, text: str) -> bool:
        lowered = " ".join(text.strip().lower().split())
        return any(hint in lowered for hint in _TASK_REVIEW_CANCEL_HINTS)

    def _looks_like_unblocker_task_request(self, text: str) -> bool:
        lowered = text.strip().lower()
        return any(
            phrase in lowered
            for phrase in (
                "create task",
                "make task",
                "open task",
                "draft a task",
                "unblocker task",
                "someone else should do",
                "have someone else do",
            )
        )

    def _completion_requires_summary(self, text: str) -> bool:
        lowered = " ".join(text.strip().lower().split())
        if not lowered:
            return True
        if lowered in {
            "done",
            "im done",
            "i'm done",
            "finished",
            "im finished",
            "i'm finished",
            "complete",
            "completed",
            "all set",
            "wrapped up",
        }:
            return True
        if len(lowered.split()) <= 4 and any(
            token in lowered for token in ("done", "finished", "complete", "completed", "all set", "wrapped up")
        ):
            return True
        return False

    def _looks_like_explicit_clock_out_text(self, text: str) -> bool:
        lowered = text.strip().lower()
        return any(
            phrase in lowered
            for phrase in (
                "clocking out",
                "clocked out",
                "logging off",
                "done for the day",
                "end of day",
            )
        )

    def _should_treat_as_task_completion(self, session: SessionState, text: str) -> bool:
        if session.stage != "active" or not self._active_task_id(session):
            return False
        if not text.strip() or self._looks_like_explicit_clock_out_text(text):
            return False
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") == "task_finish_confirmation":
            return False
        return self._looks_like_task_complete(text)

    def _is_affirmative_reply(self, text: str) -> bool:
        lowered = text.strip().lower()
        return lowered in {"yes", "y", "yeah", "yep", "please do", "do it", "go ahead", "sure"} or lowered.startswith("yes ")

    def _is_negative_reply(self, text: str) -> bool:
        lowered = text.strip().lower()
        return lowered in {"no", "n", "nope", "nah", "dont", "don't"} or lowered.startswith("no ")

    def _looks_like_lunch_resume_reply(self, text: str) -> bool:
        normalized = " ".join(text.strip().lower().split())
        if normalized in {
            "done",
            "im done",
            "i'm done",
            "finished",
            "im finished",
            "i'm finished",
            "all done",
            "back",
            "im back",
            "i'm back",
        }:
            return True
        if re.search(r"\bclock(?:ed|\s+me)?(?:\s+back)?\s+in\b", normalized):
            return True
        if re.search(r"\b(?:got|came|am|i'?m|been)\s+back\b", normalized):
            return True
        if re.search(
            r"\b(?:off|finished|done with|no (?:more|longer))\s+(?:my\s+)?lunch(?:\s+break)?\b",
            normalized,
        ):
            return True
        return bool(
            re.search(r"\b(?:back|returned)\b", normalized)
            and re.search(r"\b(?:lunch|break)\b", normalized)
        )

    def _reported_lunch_return_at(
        self,
        user: UserProfile,
        session: SessionState,
        text: str,
        now: datetime,
    ) -> datetime | None:
        match = _LUNCH_RETURN_TIME_PATTERN.search(text)
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour > 23 or minute > 59:
            return None
        meridiem = re.sub(r"[^apm]", "", (match.group(3) or "").lower())
        if meridiem and (hour < 1 or hour > 12):
            return None
        timezone_name = self.resolve_user_timezone_name(user)
        local_now = localize_datetime(now, timezone_name)
        lunch_started_at = self._coerce_datetime(
            str(session.metadata.get("lunch_started_at") or ""),
            timezone_name=timezone_name,
        )
        if lunch_started_at is None:
            return None
        lunch_started_at = localize_datetime(lunch_started_at, timezone_name)
        if meridiem:
            candidate_hour = hour % 12
            if meridiem.startswith("p"):
                candidate_hour += 12
            candidate_hours = [candidate_hour]
        else:
            candidate_hours = [hour]
            if 1 <= hour <= 11:
                candidate_hours.append(hour + 12)
        candidates = [
            local_now.replace(hour=candidate_hour, minute=minute, second=0, microsecond=0)
            for candidate_hour in candidate_hours
            if 0 <= candidate_hour <= 23
        ]
        valid = [
            candidate
            for candidate in candidates
            if lunch_started_at <= candidate <= local_now
        ]
        return max(valid) if valid else None

    def _normalize_priority(self, text: str) -> str | None:
        lowered = text.strip().lower()
        if lowered in {"urgent", "high", "normal", "low"}:
            return lowered
        return None

    def _parse_due_date_ms(self, text: str) -> int | None | bool:
        lowered = text.strip().lower()
        if not lowered or lowered in {"none", "no", "n/a", "na"}:
            return None
        try:
            due_date = datetime.fromisoformat(lowered).date()
        except ValueError:
            return False
        return int(datetime.combine(due_date, datetime.min.time()).timestamp() * 1000)

    def _touch_inbound_session(self, session: SessionState, now: datetime) -> None:
        iso_value = now.isoformat()
        session.last_contact_at = iso_value
        session.last_user_message_at = iso_value
        session.pending_clickup_sync = True
        session.metadata.pop(_AUTO_CLOCK_OUT_WARNING_KEY, None)

    def _normalize_session_state(
        self,
        session: SessionState,
        *,
        user: UserProfile | None = None,
    ) -> bool:
        changed = self._ensure_work_segments_consistency(session)
        timezone_name = self.resolve_user_timezone_name(user) if user is not None else None
        if timezone_name and self._normalize_session_timestamps(session, timezone_name=timezone_name):
            changed = True
        normalized_reviews = self._pending_admin_reviews(session)
        if normalized_reviews != session.metadata.get(_PENDING_ADMIN_REVIEWS_KEY):
            self._set_pending_admin_reviews(session, normalized_reviews)
            changed = True
        if not session.intake_completed_at:
            last_task_onboarding_completed_at = str(
                session.metadata.get("last_task_onboarding_completed_at") or ""
            ).strip()
            if last_task_onboarding_completed_at:
                session.intake_completed_at = last_task_onboarding_completed_at
                changed = True
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        tracking = session.metadata.get("clickup_time_tracking")
        has_open_tracking = isinstance(tracking, dict) and not tracking.get("closed_at")
        has_open_task_onboarding = isinstance(prompt, dict) and str(prompt.get("type") or "") == "task_onboarding"
        has_open_task_creation = isinstance(prompt, dict) and str(prompt.get("type") or "") == "task_creation"
        has_open_progress_probe = isinstance(prompt, dict) and str(prompt.get("type") or "") == "progress_probe"
        if session.stage in {"awaiting_clock_out_artifacts", "clocked_out"} and has_open_task_onboarding:
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            changed = True
        if session.stage in {"awaiting_clock_out_artifacts", "clocked_out"} and has_open_task_creation:
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            changed = True
        if session.stage in {"awaiting_clock_out_artifacts", "clocked_out"} and has_open_progress_probe:
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            changed = True
        if session.stage in {"awaiting_clock_out_artifacts", "clocked_out"} and (
            self._pending_follow_up(session) or self._pending_follow_up_aggregation(session)
        ):
            self._clear_follow_up_probe_tracking(session)
            changed = True
        if session.stage == "clocked_out" and has_open_tracking:
            session.stage = "active"
            session.clocked_out_at = None
            session.awaiting_clock_out_photo = False
            session.awaiting_clock_out_summary = False
            session.metadata["session_reactivated_at"] = datetime.now(
                tz=resolve_timezone(
                    self.runtime_timezone_name()
                )
            ).isoformat()
            changed = True
        return changed

    def _normalize_session_timestamps(
        self,
        session: SessionState,
        *,
        timezone_name: str,
    ) -> bool:
        changed = False
        for field_name in _SESSION_STATE_TIMESTAMP_FIELDS:
            raw_value = getattr(session, field_name)
            normalized_value, field_changed = self._normalize_timestamp_value(
                raw_value,
                timezone_name=timezone_name,
            )
            if field_changed:
                setattr(session, field_name, normalized_value)
                changed = True
        for segment in session.work_segments:
            if not isinstance(segment, dict):
                continue
            for key in ("clocked_in_at", "clocked_out_at"):
                normalized_value, field_changed = self._normalize_timestamp_value(
                    segment.get(key),
                    timezone_name=timezone_name,
                )
                if field_changed:
                    segment[key] = normalized_value
                    changed = True
        if self._normalize_timestamp_container(session.metadata, timezone_name=timezone_name):
            changed = True
        return changed

    def _normalize_timestamp_container(
        self,
        value: Any,
        *,
        timezone_name: str,
    ) -> bool:
        changed = False
        if isinstance(value, dict):
            for key, item in list(value.items()):
                if isinstance(key, str) and self._should_normalize_timestamp_key(key):
                    normalized_item, item_changed = self._normalize_timestamp_value(
                        item,
                        timezone_name=timezone_name,
                    )
                    if item_changed:
                        value[key] = normalized_item
                        item = normalized_item
                        changed = True
                if isinstance(item, (dict, list)) and self._normalize_timestamp_container(
                    item,
                    timezone_name=timezone_name,
                ):
                    changed = True
            return changed
        if isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)) and self._normalize_timestamp_container(
                    item,
                    timezone_name=timezone_name,
                ):
                    changed = True
        return changed

    def _normalize_timestamp_value(
        self,
        raw_value: Any,
        *,
        timezone_name: str,
    ) -> tuple[Any, bool]:
        if not isinstance(raw_value, str):
            return raw_value, False
        text = raw_value.strip()
        if not text:
            return raw_value, False
        parsed = self._coerce_datetime(text, timezone_name=timezone_name)
        if not parsed:
            return raw_value, False
        normalized = parsed.isoformat()
        return normalized, normalized != raw_value

    def _should_normalize_timestamp_key(self, key: str) -> bool:
        return key == "at" or key.endswith("_at")

    def _should_scan_timestamp_key(self, key: str) -> bool:
        return self._should_normalize_timestamp_key(key) or key.endswith("_since")

    def _ensure_work_segments_consistency(self, session: SessionState) -> bool:
        normalized_segments: list[dict[str, str | None]] = []
        raw_segments = session.work_segments if isinstance(session.work_segments, list) else []
        for raw_segment in raw_segments:
            if not isinstance(raw_segment, dict):
                continue
            clocked_in_at = str(raw_segment.get("clocked_in_at") or "").strip() or None
            clocked_out_at = str(raw_segment.get("clocked_out_at") or "").strip() or None
            if not clocked_in_at and not clocked_out_at:
                continue
            normalized_segments.append(
                {
                    "clocked_in_at": clocked_in_at,
                    "clocked_out_at": clocked_out_at,
                }
            )
        changed = normalized_segments != session.work_segments
        session.work_segments = normalized_segments
        if not session.work_segments and (session.clocked_in_at or session.clocked_out_at):
            session.work_segments = [
                {
                    "clocked_in_at": session.clocked_in_at,
                    "clocked_out_at": session.clocked_out_at,
                }
            ]
            changed = True
        return changed

    def _start_new_work_segment(self, session: SessionState, now: datetime) -> None:
        self._ensure_work_segments_consistency(session)
        if session.work_segments and not session.work_segments[-1].get("clocked_out_at"):
            if not session.work_segments[-1].get("clocked_in_at"):
                session.work_segments[-1]["clocked_in_at"] = now.isoformat()
            return
        session.work_segments.append(
            {
                "clocked_in_at": now.isoformat(),
                "clocked_out_at": None,
            }
        )

    def _close_current_work_segment(self, session: SessionState, now: datetime) -> None:
        self._ensure_work_segments_consistency(session)
        for segment in reversed(session.work_segments):
            if not segment.get("clocked_out_at"):
                segment["clocked_out_at"] = now.isoformat()
                return
        if session.clocked_in_at:
            session.work_segments.append(
                {
                    "clocked_in_at": session.clocked_in_at,
                    "clocked_out_at": now.isoformat(),
                }
            )

    def _clear_clock_out_state(self, session: SessionState) -> None:
        session.clocked_out_at = None
        session.awaiting_clock_out_photo = False
        session.awaiting_clock_out_summary = False
        session.metadata.pop(_CLOCK_OUT_RETURN_STATE_KEY, None)

    def _clear_auto_clock_out_metadata(self, session: SessionState) -> None:
        for key in (
            "auto_clock_out_at",
            "auto_clock_out_reference_at",
            "auto_clock_out_reason",
            "auto_clock_out_note",
        ):
            session.metadata.pop(key, None)
        session.metadata.pop(_AUTO_CLOCK_OUT_WARNING_KEY, None)

    def _should_track_stuck_signal(self, session: SessionState) -> bool:
        return session.stage in {"active", "awaiting_clock_out_artifacts"}

    def _can_offer_lunch_confirmation(self, session: SessionState) -> bool:
        if not session.clocked_in_at or session.clocked_out_at:
            return False
        return session.stage in {"active", "awaiting_task_selection", "awaiting_plan", "awaiting_start_photo", "awaiting_risk"}

    def _message_mentions_lunch(self, text: str) -> bool:
        return bool(re.search(r"\blunch\b", text.lower()))

    def _clear_pending_lunch_confirmation(self, session: SessionState) -> None:
        session.metadata.pop(_LUNCH_CONFIRMATION_REQUESTED_AT_KEY, None)

    def _clear_task_onboarding_prompt(self, session: SessionState) -> None:
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") == "task_onboarding":
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)

    async def _start_clock_out(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
    ) -> None:
        if _CLOCK_OUT_RETURN_STATE_KEY not in session.metadata:
            prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
            session.metadata[_CLOCK_OUT_RETURN_STATE_KEY] = {
                "stage": session.stage,
                "awaiting_clock_out_photo": session.awaiting_clock_out_photo,
                "awaiting_clock_out_summary": session.awaiting_clock_out_summary,
                "clickup_prompt": deepcopy(prompt) if isinstance(prompt, dict) else None,
                "had_clock_out_photo_paths": "clock_out_photo_paths" in session.metadata,
                "clock_out_photo_paths": deepcopy(session.metadata.get("clock_out_photo_paths")),
                "had_clock_out_summary_message_id": "clock_out_summary_message_id" in session.metadata,
                "clock_out_summary_message_id": session.metadata.get("clock_out_summary_message_id"),
                "pending_lunch_confirmation": session.metadata.get(_LUNCH_CONFIRMATION_REQUESTED_AT_KEY),
            }
        self._clear_pending_lunch_confirmation(session)
        self._clear_task_onboarding_prompt(session)
        if self._progress_probe_prompt(session):
            await self._close_progress_probe(
                client,
                user,
                session,
                now,
                reason="converted_to_clock_out",
            )
        else:
            self._clear_follow_up_probe_tracking(session)
        session.stage = "awaiting_clock_out_artifacts"
        session.awaiting_clock_out_photo = not bool(inbound.attachments)
        session.awaiting_clock_out_summary = True
        if inbound.attachments:
            self._record_attachment_paths(session, "clock_out_photo_paths", inbound)
        await self._send_dm(client, user, session, self.config.prompts.clock_out_prompt, now)

    async def _cancel_clock_out(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> None:
        return_state = session.metadata.pop(_CLOCK_OUT_RETURN_STATE_KEY, None)
        return_state = return_state if isinstance(return_state, dict) else {}
        previous_stage = str(return_state.get("stage") or "")
        if previous_stage and previous_stage not in {"awaiting_clock_out_artifacts", "clocked_out"}:
            session.stage = previous_stage
        else:
            session.stage = "active" if session.clocked_in_at else "awaiting_clock_in"
        session.clocked_out_at = None
        session.awaiting_clock_out_photo = bool(return_state.get("awaiting_clock_out_photo", False))
        session.awaiting_clock_out_summary = bool(return_state.get("awaiting_clock_out_summary", False))

        restored_prompt = return_state.get("clickup_prompt")
        if isinstance(restored_prompt, dict):
            session.metadata[_CLICKUP_PROMPT_KEY] = deepcopy(restored_prompt)
        else:
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)

        for key in ("clock_out_photo_paths", "clock_out_summary_message_id"):
            had_key = bool(return_state.get(f"had_{key}", False))
            if had_key:
                session.metadata[key] = deepcopy(return_state.get(key))
            else:
                session.metadata.pop(key, None)
        pending_lunch_confirmation = return_state.get("pending_lunch_confirmation")
        if pending_lunch_confirmation:
            session.metadata[_LUNCH_CONFIRMATION_REQUESTED_AT_KEY] = pending_lunch_confirmation

        message = "Okay, I canceled the clock-out process. You are still clocked in."
        if isinstance(restored_prompt, dict) and str(restored_prompt.get("type") or "") == "task_onboarding":
            step = str(restored_prompt.get("step") or "select_task")
            if step == "select_task":
                continuation = "Let's continue onboarding. Reply with the task name or task ID you are working on."
            else:
                continuation = self._task_onboarding_question(restored_prompt, step)
            message = f"{message}\n\n{continuation}"
        await self._send_dm(client, user, session, message, now)

    async def _handle_clock_out_artifacts(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
    ) -> None:
        if is_clock_out_cancellation(inbound.content):
            await self._cancel_clock_out(client, user, session, now)
            return
        self._clear_task_onboarding_prompt(session)
        if inbound.attachments:
            self._record_attachment_paths(session, "clock_out_photo_paths", inbound)
            session.awaiting_clock_out_photo = False
        if inbound.content.strip():
            session.metadata["clock_out_summary_message_id"] = inbound.message_id
            session.awaiting_clock_out_summary = False
            session.latest_status = inbound.content.strip()
        if session.awaiting_clock_out_photo or session.awaiting_clock_out_summary:
            reminders: list[str] = []
            if session.awaiting_clock_out_photo:
                reminders.append("the picture")
            if session.awaiting_clock_out_summary:
                reminders.append("the written wrap-up")
            await self._send_dm(
                client,
                user,
                session,
                f"I still need {' and '.join(reminders)} before I close out today.",
                now,
            )
            return
        if session.stage == "on_lunch_break" or (
            session.metadata.get("lunch_started_at")
            and not session.metadata.get("lunch_ended_at")
        ):
            session.metadata["lunch_ended_at"] = now.isoformat()
            self._record_lunch_window_end(session, now)
        session.clocked_out_at = now.isoformat()
        self._close_current_work_segment(session, now)
        session.stage = "clocked_out"
        session.metadata.pop(_CLOCK_OUT_RETURN_STATE_KEY, None)
        note = await self._finalize_clickup_day(user, session, now)
        message = "Perfect. I saved everything and will update the project trail."
        if note:
            message = f"{message}\n\n{note}"
        await self._send_dm(client, user, session, message, now)

    def _record_attachment_paths(self, session: SessionState, key: str, inbound: MessageRecord) -> None:
        paths = [attachment.local_path for attachment in inbound.attachments if attachment.local_path]
        if not paths:
            return
        existing = [path for path in session.metadata.get(key, []) if isinstance(path, str)]
        for path in paths:
            if path not in existing:
                existing.append(path)
        session.metadata[key] = existing

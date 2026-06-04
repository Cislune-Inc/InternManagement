from __future__ import annotations

import os
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import discord

from .admin_commands import AdminCommandRouter
from .advisor import Advisor, build_advisor
from .clickup_client import ClickUpClient
from .config import load_bootstrap
from .formatting import build_clickup_update, build_transcript_markdown
from .image_intelligence import ImageIntelligence, build_image_manifest
from .interface_intelligence import InterfaceIntelligence
from .local_store import LocalStore
from .models import AdminProfile, AgentConfig, AttachmentRecord, ClickUpContextBundle, MessageRecord, SessionState, UserProfile
from .signals import detect_signals
from .state_store import StateStore
from .time_utils import effective_workday_date, localize_datetime, resolve_timezone


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
    "blocked",
    "stuck",
    "need help",
    "tomorrow",
    "next day",
)

_CLICKUP_PROMPT_KEY = "clickup_prompt"
_STATE_MACHINE_CHANGES_FILENAME = "state_machine_changes.jsonl"
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
        self.roster_by_key: dict[str, UserProfile] = {}
        self.clickup: ClickUpClient | None = None
        self.advisor: Advisor = build_advisor(
            os.environ.get("OPENAI_API_KEY"),
            os.environ.get("OPENAI_MODEL"),
            os.environ.get("BACKUP_OPENAI_MODEL"),
        )
        self.image_intelligence = ImageIntelligence()
        self.interface_intelligence = InterfaceIntelligence()
        self.admin_router = AdminCommandRouter(self)
        self._config_loaded_at: datetime | None = None

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
        self.roster_by_discord_id = {user.discord_user_id: user for user in roster if user.active}
        self.roster_by_key = {user.user_key: user for user in roster if user.active}
        clickup_token = os.environ.get("CLICKUP_API_TOKEN")
        self.clickup = ClickUpClient(clickup_token, self.config) if clickup_token else None
        self._config_loaded_at = now

    def is_admin_user(self, discord_user_id: int) -> bool:
        if not self.config:
            return False
        return discord_user_id in {admin.discord_user_id for admin in self.config.admins}

    def admin_profiles(self) -> list[AdminProfile]:
        if not self.config:
            return []
        return list(self.config.admins)

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
        return self.state_store.get_session(user.user_key, session_date), local_now

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
            lines.append(f"- Lunch break: on break since {lunch_started_at or 'unknown'}")
        if self._has_pending_admin_review(session):
            review = self._pending_admin_review(session) or {}
            lines.append(
                f"- Admin review: pending for {review.get('task_name') or tracking['active_task_name'] or 'current task'}"
            )
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
        now: datetime | None = None,
    ) -> str:
        await self.refresh_configuration()
        if not self.config or not self.clickup:
            return "ClickUp is not configured."
        review = self._pending_admin_review(session)
        if not review:
            return f"{user.display_name} does not have a task waiting on admin review."
        previous_session = self._clone_session_state(session)
        now = now or self.resolve_user_local_now(user)
        task_id = str(review.get("task_id") or self._active_task_id(session) or "")
        task_name = str(review.get("task_name") or session.metadata.get("active_clickup_task_name") or "the task")
        if approve_close:
            if task_id:
                await self._safe_set_task_state(session, task_id, "complete")
                if self.clickup:
                    await self.clickup.comment_on_task(
                        task_id,
                        f"Admin review approved closure. Admin note: {admin_message}",
                    )
                self._remember_recently_closed_task(session, task_id, task_name, now)
            session.metadata["last_admin_review_resolution"] = {
                "decision": "close",
                "at": now.isoformat(),
                "message": admin_message,
            }
            session.metadata.pop("pending_admin_review", None)
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
                    "task_id": task_id,
                    "task_name": task_name,
                    "admin_message_excerpt": self._excerpt_text(admin_message),
                },
            )
            await self.write_dashboard()
            return f"Closed `{task_name}` for {user.display_name} and started next-task onboarding."
        if task_id:
            await self._safe_set_task_state(session, task_id, "in_progress")
            if self.clickup:
                await self.clickup.comment_on_task(
                    task_id,
                    f"Admin requested more work before closure: {admin_message}",
                )
        session.metadata["last_admin_review_resolution"] = {
            "decision": "rework",
            "at": now.isoformat(),
            "message": admin_message,
        }
        session.metadata.pop("pending_admin_review", None)
        session.stage = "active"
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "task_onboarding",
            "source": "review_rework",
            "step": "plan",
            "task_id": task_id,
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
                "task_id": task_id,
                "task_name": task_name,
                "admin_message_excerpt": self._excerpt_text(admin_message),
            },
        )
        await self.write_dashboard()
        return f"Sent review feedback back to {user.display_name} and reactivated `{task_name}`."

    async def handle_incoming_message(self, client: discord.Client, message: discord.Message) -> None:
        await self.refresh_configuration()
        if not self.config:
            return
        user = self.roster_by_discord_id.get(message.author.id)
        if not user:
            return
        session, now = self.get_user_session_for_moment(user, message.created_at)
        inbound = await self._build_inbound_record(message, session, user)
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
        normalized_changed = self._normalize_session_state(session)
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
        signals = detect_signals(inbound.content)
        signals = await self.interface_intelligence.enrich_intern_signals(
            inbound.content,
            session.stage,
            signals,
        )
        if not session.first_sign_of_life_at:
            session.first_sign_of_life_at = now.isoformat()
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
        if self._should_track_stuck_signal(session) and signals.stuck and not session.stuck_since:
            session.stuck_since = now.isoformat()
            session.latest_blocker = inbound.content
        if self._should_track_stuck_signal(session) and signals.recovered:
            session.stuck_since = None
        if not signals.clocking_out and await self._handle_clickup_prompt(client, user, session, inbound, signals, now):
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
        await self._route_message(client, user, session, inbound, signals, now)
        await self._apply_post_route_clickup_automation(client, user, session, inbound, signals, now)
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
        if self._has_pending_admin_review(session):
            await self._start_task_selection_resume(client, user, session, now, reason="Your previous task is waiting on admin review, so I need you to confirm what you are picking up next.")
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
            lines.extend(
                [
                    "",
                    f"My recommendation is `{recommendation_name}` ({recommendation_id}).",
                    "It is the highest-priority assigned task I can see right now, using earliest-created as the tie-breaker.",
                    f"Priority: {recommendation_priority}",
                    "If that is what you are starting with, reply `yes` or `recommended`. Otherwise reply with a different assigned task name or task ID.",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "I could not find an assigned ClickUp task to recommend yet.",
                    "Reply with the exact assigned task name or task ID you are starting with, or ask admin to assign one.",
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
        await self.refresh_configuration()
        if not self.config:
            return
        base_now = datetime.now(tz=resolve_timezone(self.runtime_timezone_name()))
        for user in self.roster_by_key.values():
            session, now = self.get_user_session_for_moment(user, base_now)
            is_workday = now.weekday() in self.config.schedule.workdays
            previous_session = self._clone_session_state(session)
            normalized_changed = self._normalize_session_state(session)
            changed = False
            reasons: list[str] = []
            if normalized_changed:
                changed = True
                reasons.append("normalized_session_state")
            if is_workday and await self._maybe_send_clock_in(client, user, session, now):
                changed = True
                reasons.append("sent_clock_in_prompt")
            if await self._maybe_auto_clock_out_inactive(user, session, now):
                changed = True
                reasons.append("auto_clock_out_inactive")
            if is_workday and await self._maybe_prompt_task_onboarding(client, user, session, now):
                changed = True
                reasons.append("prompted_task_onboarding")
            if is_workday and await self._maybe_send_lunch_break_check_in(client, user, session, now):
                changed = True
                reasons.append("sent_lunch_check_in")
            if is_workday and await self._maybe_send_follow_up(client, user, session, now):
                changed = True
                reasons.append("sent_follow_up")
            if await self._maybe_alert_admin(client, user, session, now):
                changed = True
                reasons.append("alerted_admin")
            if await self._maybe_flush_clickup(user, session, now):
                changed = True
                reasons.append("flushed_clickup")
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
        await self.write_dashboard()

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
                lines.append(f"- Clocked in: `{session.clocked_in_at}`")
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
                lines.append(f"- On lunch break since: `{lunch_started_at or 'unknown'}`")
            auto_clock_out_at = session.metadata.get("auto_clock_out_at")
            if isinstance(auto_clock_out_at, str) and auto_clock_out_at:
                lines.append(f"- Auto clocked out after inactivity: `{auto_clock_out_at}`")
            if session.clocked_out_at:
                lines.append(f"- Clocked out: `{session.clocked_out_at}`")
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
                if signals.stuck:
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
        should_send = False
        if not session.last_clock_in_prompt_at:
            should_send = True
            prompt = self.config.prompts.clock_in
        else:
            last_prompt = datetime.fromisoformat(session.last_clock_in_prompt_at)
            if now - last_prompt >= timedelta(hours=1):
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
        if self._has_pending_admin_review(session):
            return False
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") == "task_onboarding":
            return False
        if not session.intake_completed_at:
            return False
        intake_completed = datetime.fromisoformat(session.intake_completed_at)
        if now - intake_completed < timedelta(minutes=self.config.schedule.follow_up_interval_minutes):
            return False
        if session.last_follow_up_at:
            last_follow_up = datetime.fromisoformat(session.last_follow_up_at)
            if now - last_follow_up < timedelta(minutes=self.config.schedule.follow_up_interval_minutes):
                return False
        questions = self.config.prompts.follow_up_questions
        index = int(session.metadata.get("follow_up_index", 0)) % len(questions)
        await self._send_dm(client, user, session, questions[index], now)
        session.metadata["follow_up_index"] = index + 1
        session.last_follow_up_at = now.isoformat()
        return True

    async def _maybe_send_lunch_break_check_in(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if session.stage != "on_lunch_break" or session.clocked_out_at:
            return False
        last_prompt_at = self._metadata_datetime(session, "lunch_last_prompt_at")
        if last_prompt_at and now - last_prompt_at < timedelta(minutes=self.config.schedule.follow_up_interval_minutes):
            return False
        await self._send_dm(client, user, session, self.config.prompts.lunch_break_check_in, now)
        session.metadata["lunch_last_prompt_at"] = now.isoformat()
        session.metadata["lunch_resume_requested_at"] = now.isoformat()
        return True

    async def _maybe_auto_clock_out_inactive(
        self,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if not self.config or not session.clocked_in_at or session.clocked_out_at:
            return False
        if session.stage == "on_lunch_break":
            return False
        reference_at = self._last_inbound_check_in_at(session)
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
        stuck_since = datetime.fromisoformat(session.stuck_since)
        if now - stuck_since < timedelta(hours=self.config.schedule.stuck_alert_after_hours):
            return False
        sent_to = await self._send_admin_notice(
            client,
            f"{user.display_name} has appeared stuck since {session.stuck_since}. "
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
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") == "task_onboarding":
            return False
        if not session.pending_clickup_sync or not session.last_user_message_at:
            return False
        if now - datetime.fromisoformat(session.last_user_message_at) < timedelta(
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
            cutoff = datetime.fromisoformat(session.last_clickup_sync_at)
            messages = [message for message in messages if message.created_at > cutoff]
        if not messages and not force:
            return False
        clickup_bundle = await self._get_clickup_context(user, session, messages)
        self._remember_clickup_context(session, clickup_bundle)
        summary = await self.advisor.summarize_updates(user, session, messages, clickup_bundle.context)
        workspace = await self.store.ensure_user_workspace(user, session.session_date)
        comment_text = build_clickup_update(user, session, summary, str(workspace.daily_dir))
        if self.clickup:
            task_id = await self.clickup.post_update(
                user,
                clickup_bundle.active_task_id,
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

    async def _send_dm(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        content: str,
        now: datetime,
    ) -> None:
        discord_user = await client.fetch_user(user.discord_user_id)
        dm = await discord_user.create_dm()
        sent = await dm.send(content)
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

    async def _send_admin_notice(
        self,
        client: discord.Client,
        content: str,
        *,
        target_admins: list[AdminProfile] | None = None,
        files: list[Path] | None = None,
        user: UserProfile | None = None,
        session: SessionState | None = None,
    ) -> list[str]:
        admins = target_admins or self.admin_profiles()
        if not admins:
            return []
        sent_to: list[str] = []
        for admin in admins:
            discord_user = await client.fetch_user(admin.discord_user_id)
            dm = await discord_user.create_dm()
            if files:
                sent = await dm.send(content=content[:1900], files=[discord.File(str(path)) for path in files[:10]])
            else:
                sent = await dm.send(content)
            sent_to.append(admin.name)
            if user and session:
                self.state_store.append_message(
                    user.user_key,
                    session.session_date,
                    MessageRecord(
                        message_id=str(sent.id),
                        direction="outbound",
                        author_id=admin.discord_user_id,
                        created_at=sent.created_at,
                        content=sent.content,
                        attachments=[],
                    ),
                )
        return sent_to

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
    ) -> ClickUpContextBundle:
        if not self.clickup:
            return ClickUpContextBundle()
        return await self.clickup.get_context_bundle(user, session, messages)

    def _remember_clickup_context(self, session: SessionState, bundle: ClickUpContextBundle) -> None:
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
            await self._start_task_review_submission(client, user, session, inbound, now)
            return
        if signals.stuck and session.stage in {"active", "awaiting_clock_out_artifacts"}:
            await self._maybe_prompt_stuck_assistance(client, user, session, inbound, now)

    async def _start_task_review_submission(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
    ) -> None:
        summary_text = inbound.content.strip()
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
        if signals.recovered and prompt_type in {"stuck_assistance", "unblocker_task_draft"}:
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
        if prompt_type == "task_review_submission":
            return await self._handle_task_review_submission_prompt(client, user, session, inbound, now, prompt)
        if prompt_type == "task_onboarding":
            return await self._handle_task_onboarding_prompt(client, user, session, inbound, now, prompt)
        if prompt_type == "stuck_assistance":
            return await self._handle_stuck_assistance_prompt(client, user, session, inbound, now, prompt)
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
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(client, user, session, "Okay, I cancelled the task-onboarding flow.", now)
            return True
        step = str(prompt.get("step") or "select_task")
        if step == "select_task":
            if not self.clickup:
                session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
                await self._send_dm(client, user, session, "I cannot resolve tasks right now because ClickUp is unavailable.", now)
                return True
            recommended_task_id = str(prompt.get("recommended_task_id") or "")
            recommended_task_name = str(prompt.get("recommended_task_name") or "")
            task: dict[str, Any] | None
            if recommended_task_id and (lowered == "recommended" or self._is_affirmative_reply(text)):
                try:
                    task = await self.clickup.get_task(recommended_task_id)
                except Exception:
                    task = None
            else:
                task = await self.clickup.resolve_task_for_user(user, text, include_mission_board=False)
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
            prompt["task_id"] = str(task.get("id") or "")
            prompt["task_name"] = str(task.get("name") or "Unnamed task")
            prompt["step"] = "plan"
            session.stage = "awaiting_plan"
            session.latest_plan = None
            session.latest_feedback = None
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
                await self._send_dm(client, user, session, "I still need the task-start picture for this task.", now)
                return True
            self._record_attachment_paths(session, "progress_photo_paths", inbound)
            session.awaiting_start_photo = False
            await self._finish_task_onboarding(user, session, prompt, now)
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(
                client,
                user,
                session,
                f"Perfect. `{prompt.get('task_name') or 'That task'}` is now active and I updated the task tracking.",
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
        if lowered in {"cancel", "never mind", "nevermind", "stop"}:
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(client, user, session, "Okay, I cancelled the admin-review submission for now.", now)
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

    async def _handle_stuck_assistance_prompt(
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
        if lowered in {"cancel", "never mind", "nevermind", "stop", "no"}:
            session.metadata.pop(_CLICKUP_PROMPT_KEY, None)
            await self._send_dm(
                client,
                user,
                session,
                "Okay. I logged the blocker locally. If you want admin help or an unblocker task later, just say so.",
                now,
            )
            return True
        if self._looks_like_unblocker_task_request(text):
            await self._begin_unblocker_task_draft(client, user, session, prompt, now)
            return True
        requested_admins = self._resolve_requested_admins(text)
        if requested_admins:
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
        await self._send_dm(
            client,
            user,
            session,
            self._stuck_assistance_prompt(),
            now,
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
                    "Who should own this task? Reply with a roster/admin name, or say `unassigned` if you want admin to decide.\n\n"
                    f"Admins I know: {self.admin_name_list_text()}."
                ),
                now,
            )
            return True
        if step == "assignee":
            if not text:
                await self._send_dm(client, user, session, "Tell me who should own this task, or say `unassigned`.", now)
                return True
            assignee_label, assignee_id, assignee_note = await self._resolve_unblocker_assignee(text)
            draft["assignee_label"] = assignee_label
            draft["assignee_id"] = assignee_id
            if assignee_note:
                draft["assignee_note"] = assignee_note
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

    async def _task_selection_prompt(
        self,
        user: UserProfile,
        session: SessionState | None = None,
        *,
        tasks: list[dict[str, Any]] | None = None,
        recommended_task: dict[str, Any] | None = None,
    ) -> str:
        if not self.clickup:
            return "Tell me which ClickUp task you are working on right now."
        visible_tasks = list(tasks) if tasks is not None else await self.clickup.list_assigned_tasks(user, limit=8)
        hidden_count = 0
        if session is not None:
            recently_closed_ids = self._recently_closed_task_ids(session)
            if recently_closed_ids:
                original_count = len(visible_tasks)
                visible_tasks = [
                    task for task in visible_tasks
                    if str(task.get("id") or "") not in recently_closed_ids
                ]
                hidden_count = original_count - len(visible_tasks)
        if not visible_tasks:
            message = "I cannot see any assigned ClickUp tasks for you yet. Tell me the exact task name or ask admin to assign one."
            if hidden_count:
                message = "I hid tasks that were already closed today. " + message
            return message
        recommended_id = str((recommended_task or {}).get("id") or "")
        lines = [
            "I need to confirm your active ClickUp task before you continue. Reply with the task name or ID.",
            "",
            "Assigned tasks I can see:",
        ]
        for task in visible_tasks:
            task_id = str(task.get("id") or "")
            marker = " [recommended]" if recommended_id and task_id == recommended_id else ""
            lines.append(
                f"- {task.get('name')} | id={task_id} | status={(task.get('status') or {}).get('status') or 'unknown'} | priority={(task.get('priority') or {}).get('priority') or 'none'}{marker}"
            )
        if hidden_count:
            lines.extend(
                [
                    "",
                    "I hid tasks that were already closed today.",
                ]
            )
        return "\n".join(lines)

    async def _finish_task_onboarding(
        self,
        user: UserProfile,
        session: SessionState,
        prompt: dict[str, Any],
        now: datetime,
    ) -> None:
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
        elif source in {"daily_clock_in", "clock_in_recovery"}:
            selection_reason = "Confirmed by intern during daily clock-in onboarding."
        elif source == "same_day_reclockin":
            selection_reason = "Confirmed by intern after same-day re-clock-in."
        else:
            selection_reason = "Confirmed by intern during task onboarding."
        session.metadata["clickup_selection_reason"] = selection_reason
        session.stage = "active"
        session.clocked_out_at = None
        session.awaiting_clock_out_photo = False
        session.awaiting_clock_out_summary = False
        if not session.intake_completed_at:
            session.intake_completed_at = now.isoformat()
        session.last_follow_up_at = now.isoformat()
        await self._activate_clickup_task(user, session, now, task_id, task_name)
        session.metadata["last_task_onboarding_completed_at"] = now.isoformat()

    async def _maybe_start_lunch_break(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        self._clear_pending_lunch_confirmation(session)
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
        if session.stage in {"awaiting_admin_review", "awaiting_clock_out_artifacts", "clocked_out"}:
            await self._send_dm(
                client,
                user,
                session,
                "I cannot start a lunch break from this part of the workflow right now.",
                now,
            )
            return True
        if session.stage not in {"active", "awaiting_task_selection", "awaiting_plan", "awaiting_start_photo", "awaiting_risk"}:
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
        session.metadata["lunch_started_at"] = now.isoformat()
        session.metadata["lunch_last_prompt_at"] = now.isoformat()
        session.metadata["lunch_resume_stage"] = previous_stage
        session.metadata.pop("lunch_ended_at", None)
        session.metadata.pop("lunch_resume_requested_at", None)
        if active_task_id:
            session.metadata["lunch_resume_task_id"] = active_task_id
        if active_task_name:
            session.metadata["lunch_resume_task_name"] = active_task_name
        session.stage = "on_lunch_break"
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
        if getattr(signals, "ending_lunch", False) or self._is_affirmative_reply(text):
            await self._end_lunch_break(client, user, session, now)
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
    ) -> None:
        resume_stage = str(session.metadata.get("lunch_resume_stage") or "active")
        task_id = str(session.metadata.get("lunch_resume_task_id") or self._active_task_id(session) or "")
        task_name = str(
            session.metadata.get("lunch_resume_task_name")
            or session.metadata.get("active_clickup_task_name")
            or ""
        )
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        session.metadata["lunch_ended_at"] = now.isoformat()
        session.last_follow_up_at = now.isoformat()
        session.metadata.pop("lunch_last_prompt_at", None)
        session.metadata.pop("lunch_resume_requested_at", None)
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
                f"Welcome back. Let's pick up where we left off.\n\n{reminder}",
                now,
            )
            session.metadata.pop("lunch_resume_stage", None)
            session.metadata.pop("lunch_resume_task_id", None)
            session.metadata.pop("lunch_resume_task_name", None)
            return
        if task_id:
            session.stage = "active"
            session.awaiting_start_photo = False
            await self._start_task_timer(user, session, now, task_id, task_name)
            label = task_name or task_id
            await self._send_dm(
                client,
                user,
                session,
                f"Welcome back. I resumed task tracking for `{label}`.",
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
            f"Welcome back.\n\n{await self._task_onboarding_intro(user, session, tracking)}",
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
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "stuck_assistance",
            "step": "offer",
            "draft": {
                "origin_message_id": inbound.message_id,
                "blocker_text": inbound.content.strip() or session.latest_blocker or "",
            },
        }
        await self._send_dm(
            client,
            user,
            session,
            self._stuck_assistance_prompt(),
            now,
        )

    def _stuck_assistance_prompt(self) -> str:
        admin_names = self.admin_name_list_text()
        return (
            f"Got it. Do you need specific admin help from {admin_names}, or do you want me to draft an unblocker task that someone else should do?\n\n"
            "You can reply with an admin name if you want direct help, or say that you want me to create an unblocker task."
        )

    async def _begin_unblocker_task_draft(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        stuck_prompt: dict[str, Any],
        now: datetime,
    ) -> None:
        draft = stuck_prompt.get("draft")
        blocker_text = ""
        if isinstance(draft, dict):
            blocker_text = str(draft.get("blocker_text") or "")
        session.metadata[_CLICKUP_PROMPT_KEY] = {
            "type": "unblocker_task_draft",
            "step": "title",
            "draft": {
                "blocker_text": blocker_text or session.latest_blocker or "",
                "origin_message_id": draft.get("origin_message_id") if isinstance(draft, dict) else None,
            },
        }
        await self._send_dm(
            client,
            user,
            session,
            "Okay. I will draft the unblocker task, show it to admin, and only create it in ClickUp after approval.\n\nGive me a short title for the task.",
            now,
        )

    def _resolve_requested_admins(self, text: str) -> list[AdminProfile]:
        direct_matches = self.find_admin_profiles(text)
        if direct_matches:
            return direct_matches
        lowered = text.strip().lower()
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

    async def _resolve_unblocker_assignee(self, text: str) -> tuple[str, str | None, str | None]:
        lowered = text.strip().lower()
        if lowered in {"unassigned", "none", "no one", "admin decides"}:
            return "unassigned", None, None
        normalized = self._normalize_identifier_value(text)
        for roster_user in self.roster_by_key.values():
            candidates = {
                self._normalize_identifier_value(roster_user.user_key),
                self._normalize_identifier_value(roster_user.display_name),
                self._normalize_identifier_value(roster_user.discord_username),
            }
            if normalized in candidates:
                assignee_id = await self.clickup.resolve_clickup_user_id(roster_user) if self.clickup else None
                note = None if assignee_id else f"I could not resolve {roster_user.display_name} to a ClickUp assignee, so the task may stay unassigned until admin adjusts it."
                return roster_user.display_name, assignee_id, note
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
            return admin.name, resolved_admin_id, note
        return text.strip(), None, "I could not map that person to a ClickUp assignee, so I will show the requested owner to admin and leave the task unassigned unless they change it."

    async def _submit_unblocker_task_for_admin_review(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        draft: dict[str, Any],
        now: datetime,
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
            "Got it. I sent the unblocker-task draft to admin for review. I will only create it in ClickUp after approval.",
            now,
        )
        await self._send_admin_unblocker_task_request(client, user, session, now)

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
            "I can open a Mission Board blocker task for this. Reply `yes` if you want that, or `no` if you only want me to log the blocker locally.",
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
        created_ids = [
            task_id
            for task_id in session.metadata.get("created_blocker_task_ids", [])
            if isinstance(task_id, str)
        ]
        created_id = str(created.get("id") or "")
        if created_id and created_id not in created_ids:
            created_ids.append(created_id)
        session.metadata["created_blocker_task_ids"] = created_ids
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
        session.metadata["pending_admin_review"] = {
            "task_id": task_id,
            "task_name": task_name,
            "submitted_at": now.isoformat(),
            "completion_message_id": inbound.message_id,
            "completion_summary": summary,
            "completion_photo_paths": photo_paths,
            "pause_note": pause_note,
        }
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
        await self._send_admin_review_request(client, user, session, now)

    async def _send_admin_review_request(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> None:
        if not self.config:
            return
        review = self._pending_admin_review(session) or {}
        task_name = str(review.get("task_name") or session.metadata.get("active_clickup_task_name") or "the task")
        task_id = str(review.get("task_id") or self._active_task_id(session) or "")
        summary = str(review.get("completion_summary") or session.latest_status or "No completion summary captured.")
        photo_paths = self._resolve_existing_paths(review.get("completion_photo_paths"))
        message = (
            f"{user.display_name} says they finished `{task_name}`"
            + (f" ({task_id})" if task_id else "")
            + ".\n\n"
            f"Intern summary:\n{summary}\n\n"
            "Use `run review.close user="
            f"{user.user_key}` to close it.\n"
            "Or use `run review.rework user="
            f"{user.user_key} comments=\"...\"` if they need to keep working on it."
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
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") in {"stuck_assistance", "unblocker_task_draft"}:
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
            await self._activate_clickup_task(user, session, now, task_id, task_name)
            session.last_follow_up_at = now.isoformat()
            if notify_user:
                notice = (
                    f"Perfect. I marked `{task_name or task_id}` back to `in progress` and resumed task tracking."
                )
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
            return f"Resumed `{task_name or task_id}` and moved it back to in progress."
        await self._maybe_begin_clickup_work(user, session, now)
        task_id = self._active_task_id(session)
        task_name = str(session.metadata.get("active_clickup_task_name") or "").strip()
        if task_id:
            session.last_follow_up_at = now.isoformat()
            if notify_user:
                notice = (
                    f"Perfect. I marked `{task_name or task_id}` back to `in progress` and resumed task tracking."
                )
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
            return f"Resumed `{task_name or task_id}` and moved it back to in progress."
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
            created_ids = [
                task_id
                for task_id in session.metadata.get("created_blocker_task_ids", [])
                if isinstance(task_id, str)
            ]
            if created_id and created_id not in created_ids:
                created_ids.append(created_id)
            session.metadata["created_blocker_task_ids"] = created_ids
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
    ) -> None:
        if not self.clickup or not self.config or not self.config.clickup.auto_status_updates or not task_id:
            return
        status_name = await self.clickup.set_task_state(task_id, state)
        if status_name:
            session.metadata["last_clickup_status"] = status_name

    async def _maybe_prompt_task_onboarding(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        now: datetime,
    ) -> bool:
        if not self.config or not self.clickup:
            return False
        if not session.clocked_in_at or session.clocked_out_at:
            return False
        if self._has_pending_admin_review(session) or session.stage == "awaiting_admin_review":
            return False
        if session.stage not in {"active", "awaiting_clock_out_artifacts"}:
            return False
        tracking = await self._get_task_tracking_state(user, session)
        if tracking["active_task_id"] and tracking["timer_running"]:
            return False
        prompt = session.metadata.get(_CLICKUP_PROMPT_KEY)
        if isinstance(prompt, dict) and str(prompt.get("type") or "") == "task_onboarding":
            last_prompt_at = self._metadata_datetime(session, "last_task_onboarding_prompt_at")
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
        last_prompt_at = self._metadata_datetime(session, "last_task_onboarding_prompt_at")
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
            return f"I still need the task-start picture for `{task_name}`."
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
        return "I still need your task onboarding reply."

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
    ) -> None:
        if not task_id:
            return
        session.metadata["active_clickup_task_id"] = task_id
        if task_name:
            session.metadata["active_clickup_task_name"] = task_name
        await self._safe_set_task_state(session, task_id, "in_progress")
        await self._start_task_timer(user, session, now, task_id, task_name)

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

    def _tracked_time_totals(
        self,
        session: SessionState,
        now: datetime,
    ) -> tuple[int, list[tuple[str | None, str | None, int]]]:
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
        for segment in session.work_segments:
            if not isinstance(segment, dict):
                continue
            started_at = str(segment.get("clocked_in_at") or "").strip()
            if not started_at:
                continue
            closed_at = str(segment.get("clocked_out_at") or "").strip() or None
            end_dt = self._coerce_datetime(closed_at) or now
            total_seconds += self._elapsed_seconds_between(started_at, end_dt)
        return total_seconds

    def _refresh_session_time_summary(
        self,
        session: SessionState,
        now: datetime,
    ) -> None:
        self._ensure_work_segments_consistency(session)
        clocked_in_total_seconds = self._work_segment_total_seconds(session, now)
        tracked_total_seconds, tracked_tasks = self._tracked_time_totals(session, now)
        current_tracking = session.metadata.get("clickup_time_tracking")
        current_tracking = current_tracking if isinstance(current_tracking, dict) else {}
        timer_running = bool(current_tracking) and not current_tracking.get("closed_at") and bool(
            current_tracking.get("task_id")
        )
        session.time_summary = {
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
        return max(0, int((end_dt - start_dt).total_seconds()))

    def _coerce_datetime(self, raw_value: str | None) -> datetime | None:
        if not raw_value:
            return None
        try:
            return datetime.fromisoformat(raw_value)
        except ValueError:
            return None

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
        review = self._pending_admin_review(session) or {}
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

    def _signal_details(self, signals: Any) -> dict[str, bool]:
        return {
            "clocked_in": bool(getattr(signals, "clocked_in", False)),
            "clocking_out": bool(getattr(signals, "clocking_out", False)),
            "stuck": bool(getattr(signals, "stuck", False)),
            "recovered": bool(getattr(signals, "recovered", False)),
            "starting_lunch": bool(getattr(signals, "starting_lunch", False)),
            "ending_lunch": bool(getattr(signals, "ending_lunch", False)),
        }

    def _excerpt_text(self, text: str, limit: int = 160) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3].rstrip() + "..."

    def _last_inbound_check_in_at(self, session: SessionState) -> datetime | None:
        for value in (session.last_user_message_at, session.last_contact_at, session.clocked_in_at):
            parsed = self._coerce_datetime(value)
            if parsed:
                return parsed
        return None

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
        value = session.metadata.get("active_clickup_task_id")
        if isinstance(value, str) and value.strip():
            return value
        return None

    def _pending_admin_unblocker_task(self, session: SessionState) -> dict[str, Any] | None:
        value = session.metadata.get("pending_admin_unblocker_task")
        if isinstance(value, dict):
            return value
        return None

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

    def _pending_admin_review(self, session: SessionState) -> dict[str, Any] | None:
        value = session.metadata.get("pending_admin_review")
        if isinstance(value, dict):
            return value
        return None

    def _has_pending_admin_review(self, session: SessionState) -> bool:
        return self._pending_admin_review(session) is not None

    def _normalize_identifier_value(self, value: str) -> str:
        return "".join(ch for ch in value.lower() if ch.isalnum())

    def _metadata_datetime(self, session: SessionState, key: str) -> datetime | None:
        value = session.metadata.get(key)
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

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
        if session.stage != "active" or self._has_pending_admin_review(session) or not self._active_task_id(session):
            return False
        if not text.strip() or self._looks_like_explicit_clock_out_text(text):
            return False
        return self._looks_like_task_complete(text)

    def _is_affirmative_reply(self, text: str) -> bool:
        lowered = text.strip().lower()
        return lowered in {"yes", "y", "yeah", "yep", "please do", "do it", "go ahead", "sure"} or lowered.startswith("yes ")

    def _is_negative_reply(self, text: str) -> bool:
        lowered = text.strip().lower()
        return lowered in {"no", "n", "nope", "nah", "dont", "don't"} or lowered.startswith("no ")

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

    def _normalize_session_state(self, session: SessionState) -> bool:
        changed = self._ensure_work_segments_consistency(session)
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
        if session.stage == "clocked_out" and (has_open_tracking or has_open_task_onboarding):
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

    def _clear_auto_clock_out_metadata(self, session: SessionState) -> None:
        for key in (
            "auto_clock_out_at",
            "auto_clock_out_reference_at",
            "auto_clock_out_reason",
            "auto_clock_out_note",
        ):
            session.metadata.pop(key, None)

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

    async def _start_clock_out(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
    ) -> None:
        self._clear_pending_lunch_confirmation(session)
        session.stage = "awaiting_clock_out_artifacts"
        session.awaiting_clock_out_photo = not bool(inbound.attachments)
        session.awaiting_clock_out_summary = not bool(inbound.content.strip())
        if inbound.attachments:
            self._record_attachment_paths(session, "clock_out_photo_paths", inbound)
        if inbound.content.strip():
            session.metadata["clock_out_summary_message_id"] = inbound.message_id
        if not session.awaiting_clock_out_photo and not session.awaiting_clock_out_summary:
            session.clocked_out_at = now.isoformat()
            self._close_current_work_segment(session, now)
            session.stage = "clocked_out"
            note = await self._finalize_clickup_day(user, session, now)
            message = "Got it. I saved your end-of-day update."
            if note:
                message = f"{message}\n\n{note}"
            await self._send_dm(client, user, session, message, now)
            return
        await self._send_dm(client, user, session, self.config.prompts.clock_out_prompt, now)

    async def _handle_clock_out_artifacts(
        self,
        client: discord.Client,
        user: UserProfile,
        session: SessionState,
        inbound: MessageRecord,
        now: datetime,
    ) -> None:
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
        session.clocked_out_at = now.isoformat()
        self._close_current_work_segment(session, now)
        session.stage = "clocked_out"
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

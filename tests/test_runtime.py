import csv
import json
import logging
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncio
import requests

from agent.formatting import build_transcript_markdown
from agent.models import AdminProfile, AgentConfig, AttachmentRecord, ClickUpConfig, ClickUpContextBundle, MessageRecord, PromptConfig, ScheduleConfig, SessionState, UserProfile
from agent.advisor import CheckInAssessment
from agent.runtime import InternManagementRuntime, TaskActivationResult
from agent.signals import detect_signals
from agent.state_store import StateStore


def _write_archived_session(storage_root: Path, user: UserProfile, session: SessionState) -> Path:
    user_dir = storage_root / "people" / user.storage_folder_name
    daily_dir = user_dir / session.session_date
    daily_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "profile.json").write_text(
        json.dumps(asdict(user), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    session_path = daily_dir / "session.json"
    session_path.write_text(
        json.dumps(asdict(session), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return session_path


class _FakeClickUp:
    def can_query_assignee_timers(self) -> bool:
        return False

    async def list_assigned_tasks(self, user, limit=8):
        return [
            {
                "id": "868jun6qg",
                "name": "formalize project tree",
                "status": {"status": "to do"},
                "priority": {"priority": "high"},
                "date_created": "100",
                "list": {"id": "list-1"},
            },
            {
                "id": "868jun6qh",
                "name": "secondary cleanup",
                "status": {"status": "to do"},
                "priority": {"priority": "normal"},
                "date_created": "200",
                "list": {"id": "list-1"},
            }
        ]

    async def build_assigned_task_hierarchy(self, user, *, tasks=None, limit=25):
        del user, limit
        assigned_tasks = list(tasks) if tasks is not None else await self.list_assigned_tasks(None)
        tasks_by_id = {
            str(task.get("id") or ""): task
            for task in assigned_tasks
            if str(task.get("id") or "")
        }
        root_ids = [task_id for task_id in tasks_by_id]
        return {
            "tasks_by_id": tasks_by_id,
            "assigned_task_ids": root_ids,
            "root_ids": root_ids,
            "children_by_parent_id": {},
        }

    def render_assigned_task_hierarchy(self, hierarchy, *, recommended_task_id=None):
        tasks_by_id = hierarchy.get("tasks_by_id") or {}
        root_ids = hierarchy.get("root_ids") or []
        lines = []
        for task_id in root_ids:
            task = tasks_by_id.get(task_id) or {}
            marker = " [recommended]" if recommended_task_id and task_id == recommended_task_id else ""
            lines.append(f"\\- {task.get('name')} | id={task_id} [assigned]{marker}")
        return "\n".join(lines)

    def match_task_hint(self, tasks, task_hint: str):
        hint = task_hint.strip().lower()
        for task in tasks:
            if hint in {str(task.get("id") or "").lower(), str(task.get("name") or "").lower()}:
                return task
        for task in tasks:
            if hint and hint in str(task.get("name") or "").lower():
                return task
        return None

    async def resolve_task_for_user(self, user, task_hint: str, *, include_mission_board=False, include_workspace=False):
        del user, include_mission_board, include_workspace
        hint = task_hint.strip().lower()
        for task in await self.list_assigned_tasks(None):
            if hint in {str(task["id"]).lower(), str(task["name"]).lower()}:
                return task
        for task in await self.list_assigned_tasks(None):
            if hint and hint in str(task["name"]).lower():
                return task
        return None

    def pick_highest_priority_task(self, tasks):
        return tasks[0] if tasks else None

    async def resolve_clickup_user_id(self, user):
        return "87438366"

    async def create_task(
        self,
        list_id: str,
        *,
        name: str,
        description: str,
        assignee_ids=None,
        priority=None,
        due_date=None,
        status=None,
        tags=None,
        parent_task_id=None,
    ):
        del priority, due_date, status, tags
        return {
            "id": "created-task-1",
            "name": name,
            "description": description,
            "assignees": [{"id": int((assignee_ids or ["87438366"])[0])}] if assignee_ids else [],
            "parent": parent_task_id,
            "list": {"id": list_id},
        }

    async def comment_on_task(self, task_id: str, comment_text: str):
        return None

    async def get_task(self, task_id: str):
        for task in await self.list_assigned_tasks(None):
            if task["id"] == task_id:
                return {
                    **task,
                    "description": "Break the project into clear deliverables and align the folder structure.",
                    "list": {"id": "list-1"},
                }
        return {
            "id": task_id,
            "name": "unknown",
            "status": {"status": "to do"},
            "description": "",
            "list": {"id": "list-1"},
        }


def _build_runtime(admins: list[AdminProfile] | None = None) -> InternManagementRuntime:
    runtime = InternManagementRuntime.__new__(InternManagementRuntime)
    configured_admins = admins or [AdminProfile(name="George", discord_user_id=999)]
    runtime.bootstrap = SimpleNamespace(default_timezone="America/Los_Angeles")
    runtime.config = AgentConfig(
        timezone="America/Los_Angeles",
        admin_discord_user_id=configured_admins[0].discord_user_id,
        roster_file_name="roster.csv",
        dashboard_file_name="dashboard.md",
        schedule=ScheduleConfig(task_onboarding_interval_minutes=5, auto_clock_out_after_hours=6),
        clickup=ClickUpConfig(workspace_id="9011286053"),
        prompts=PromptConfig(
            clock_in="clock in",
            clock_in_reminder="reminder",
            plan_question="plan",
            start_photo_question="photo",
            risk_question="risk",
            follow_up_questions=["follow up"],
            clock_out_prompt="clock out",
        ),
        admins=configured_admins,
    )
    runtime.clickup = _FakeClickUp()
    runtime.roster_by_discord_id = {}
    runtime.roster_by_key = {}
    runtime._config_loaded_at = None
    runtime._user_session_locks = {}
    saved_sessions: dict[tuple[str, str], SessionState] = {}
    saved_messages: dict[tuple[str, str], list[MessageRecord]] = {}

    def save_session(_session: SessionState) -> None:
        saved_sessions[(_session.user_key, _session.session_date)] = _session

    def get_session(user_key: str, session_date: str) -> SessionState:
        existing = saved_sessions.get((user_key, session_date))
        if existing is not None:
            return existing
        return SessionState(user_key=user_key, session_date=session_date)

    def list_messages(user_key: str, session_date: str) -> list[MessageRecord]:
        return list(saved_messages.get((user_key, session_date), []))

    def append_message(user_key: str, session_date: str, message: MessageRecord) -> bool:
        bucket = saved_messages.setdefault((user_key, session_date), [])
        if any(existing.message_id == message.message_id and existing.direction == message.direction for existing in bucket):
            return False
        bucket.append(message)
        return True

    runtime.state_store = SimpleNamespace(
        save_session=save_session,
        get_session=get_session,
        list_messages=list_messages,
        append_message=append_message,
    )

    async def fake_ensure_workspace(_user, _session_date: str):
        return SimpleNamespace(daily_dir=Path("tests"))

    async def fake_append_json_line(path: Path, _payload: dict[str, object]) -> Path:
        return path

    runtime.store = SimpleNamespace(
        ensure_user_workspace=fake_ensure_workspace,
        append_json_line=fake_append_json_line,
    )

    async def fake_archive(_user, _session) -> None:
        return None

    async def fake_dashboard() -> None:
        return None

    async def fake_plan_feedback(_user, _plan_text: str, _context: str) -> str:
        return "Use the tangible result to keep the scope tight."

    async def fake_assess_check_in_reply(
        _user,
        _session,
        _question_text: str,
        reply_text: str,
        _recent_messages,
        *,
        previous_status: str | None = None,
        attachment_count: int = 0,
    ) -> CheckInAssessment:
        del previous_status
        if attachment_count > 0 or "motor" in reply_text.lower() or "wired" in reply_text.lower():
            return CheckInAssessment(
                meaningful_progress=True,
                needs_probe=False,
                reason="looks concrete",
                probe_questions=[],
            )
        return CheckInAssessment(
            meaningful_progress=False,
            needs_probe=True,
            reason="too vague",
            probe_questions=[
                "What specifically changed since the last check-in?",
                "What exact part did you work on?",
            ],
        )

    runtime._archive_session = fake_archive  # type: ignore[method-assign]
    runtime.write_dashboard = fake_dashboard  # type: ignore[method-assign]
    runtime.refresh_configuration = fake_dashboard  # type: ignore[method-assign]
    runtime.advisor = SimpleNamespace(
        plan_feedback=fake_plan_feedback,
        assess_check_in_reply=fake_assess_check_in_reply,
    )
    runtime.interface_intelligence = SimpleNamespace(
        enrich_intern_signals=lambda _text, _stage, signals: asyncio.sleep(0, result=signals),
        resolve_task_draft_intent=lambda _text, **_kwargs: asyncio.sleep(0, result=None),
        resolve_daily_availability_intent=lambda _text, **_kwargs: asyncio.sleep(0, result=None),
    )
    return runtime


def test_adaptive_follow_up_interval_uses_task_estimate_and_user_override() -> None:
    runtime = _build_runtime()
    runtime.config.schedule.follow_up_interval_minutes = 90
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-07-28",
        metadata={"task_onboarding_estimated_duration": "45 minutes"},
    )

    assert runtime._adaptive_follow_up_interval_minutes(user, session) == 45
    session.metadata["task_onboarding_estimated_duration"] = "3 hours"
    assert runtime._adaptive_follow_up_interval_minutes(user, session) == 75
    session.metadata["task_onboarding_estimated_duration"] = "1 day"
    assert runtime._adaptive_follow_up_interval_minutes(user, session) == 90
    user.check_in_interval_minutes = 110
    assert runtime._adaptive_follow_up_interval_minutes(user, session) == 110


def test_slack_feedback_reaction_flags_update_for_manager_review() -> None:
    runtime = _build_runtime()
    runtime.config.slack.enabled = True
    runtime.config.slack.feedback_poll_interval_minutes = 360
    runtime.config.slack.feedback_reactions = {
        "white_check_mark": "useful",
        "x": "wrong_task_or_channel",
    }
    user = UserProfile(
        user_key="alex",
        display_name="Alex Example",
        discord_user_id=1,
    )
    runtime.roster_by_key = {"alex": user}
    now = datetime.fromisoformat("2026-07-30T14:00:00-07:00")
    state = {
        "daily_updates": {
            "alex": {
                "2026-07-30": {
                    "posted_at": "2026-07-30T13:00:00-07:00",
                    "channel_id": "CROVER",
                    "message_ts": "123.456",
                    "active_task_id": "task-1",
                    "active_task_name": "Solar diagnostics",
                }
            }
        }
    }
    written = []
    reported = []

    class FakeSlack:
        async def get_reactions(self, channel_id, message_ts):
            assert (channel_id, message_ts) == ("CROVER", "123.456")
            return [{"name": "x", "count": 1}]

    async def report_issue(**kwargs):
        reported.append(kwargs)
        return {}

    runtime.slack = FakeSlack()
    runtime._load_slack_update_state = lambda: state  # type: ignore[method-assign]
    runtime._write_slack_update_state = lambda payload: written.append(payload)  # type: ignore[method-assign]
    runtime._report_operational_issue = report_issue  # type: ignore[method-assign]

    reviewed = asyncio.run(runtime._maybe_collect_slack_update_feedback(now))

    assert reviewed == 1
    assert reported[0]["category"] == "slack_update_feedback"
    assert reported[0]["details"]["feedback_action"] == "wrong_task_or_channel"
    assert state["daily_updates"]["alex"]["2026-07-30"]["operator_feedback"] == {
        "wrong_task_or_channel": 1
    }
    assert written[-1]["feedback_checked_at"] == now.isoformat()
    assert asyncio.run(
        runtime._maybe_collect_slack_update_feedback(now + timedelta(minutes=30))
    ) == 0


def test_slack_only_worker_receives_runtime_dm_through_slack() -> None:
    runtime = _build_runtime()
    posted: list[tuple[str, str]] = []

    async def post_message(channel_id: str, message: str) -> dict[str, str]:
        posted.append((channel_id, message))
        return {"channel": "D123", "ts": "1.234"}

    runtime.slack = SimpleNamespace(post_message=post_message)
    user = UserProfile(
        user_key="sam",
        display_name="Sam",
        slack_user_id="U123",
        preferred_transport="slack",
        worker_type="employee",
    )
    session = SessionState(user_key="sam", session_date="2026-07-28")

    outbound = asyncio.run(
        runtime._send_dm(
            SimpleNamespace(),
            user,
            session,
            "How is the task progressing?",
            datetime.fromisoformat("2026-07-28T10:00:00-07:00"),
        )
    )

    assert posted == [("U123", "How is the task progressing?")]
    assert outbound.message_id == "slack:D123:1.234"
    assert session.last_outbound_at == "2026-07-28T10:00:00-07:00"


def test_slack_admin_dm_uses_admin_console_without_worker_roster_entry() -> None:
    runtime = _build_runtime(
        admins=[
            AdminProfile(
                name="Erik",
                discord_user_id=999,
                slack_user_id="U01SWQKDTBM",
            )
        ]
    )
    posted: list[tuple[str, str]] = []
    routed: list[tuple[int, str]] = []

    async def refresh_configuration(*_args, **_kwargs):
        return None

    async def handle_plain_text(_client, admin_user_id: int, text: str) -> str:
        routed.append((admin_user_id, text))
        return "Admin beta console ready."

    async def post_message(channel_id: str, message: str) -> dict[str, str]:
        posted.append((channel_id, message))
        return {"channel": "DADMIN", "ts": "1.234"}

    runtime.refresh_configuration = refresh_configuration  # type: ignore[method-assign]
    runtime.admin_router = SimpleNamespace(handle_plain_text=handle_plain_text)
    runtime.slack = SimpleNamespace(post_message=post_message)
    runtime.roster_by_slack_id = {
        "U01SWQKDTBM": UserProfile(
            user_key="should-not-be-used",
            display_name="Worker Collision",
            slack_user_id="U01SWQKDTBM",
        )
    }

    for trailing_metadata in (
        "*Sent using* <@U0BATRYF16C>",
        "*Sent using* <@U0BATRYF16C|ChatGPT>",
        "Connector attribution represented outside the plain-text footer.",
        "*Sent using* <@U0BATRYF16C|ChatGPT> on the same rendered line.",
    ):
        separator = " " if trailing_metadata.endswith("rendered line.") else "\n"
        asyncio.run(
            runtime.handle_slack_direct_message(
                SimpleNamespace(),
                {
                    "user": "U01SWQKDTBM",
                    "text": (
                        f"run presence.attention{separator}"
                        f"{trailing_metadata}"
                    ),
                    "ts": "1785859200.0",
                },
            )
        )

    assert routed == [
        (999, "run presence.attention"),
        (999, "run presence.attention"),
        (999, "run presence.attention"),
        (999, "run presence.attention"),
    ]
    assert posted == [
        ("U01SWQKDTBM", "Admin beta console ready."),
        ("U01SWQKDTBM", "Admin beta console ready."),
        ("U01SWQKDTBM", "Admin beta console ready."),
        ("U01SWQKDTBM", "Admin beta console ready."),
    ]


def test_slack_admin_portal_command_returns_live_link(tmp_path: Path) -> None:
    runtime = _build_runtime(
        admins=[
            AdminProfile(
                name="Erik",
                discord_user_id=999,
                slack_user_id="U01SWQKDTBM",
            )
        ]
    )
    posted: list[tuple[str, str]] = []
    routed: list[str] = []

    async def refresh_configuration(*_args, **_kwargs):
        return None

    async def handle_plain_text(_client, _admin_user_id: int, text: str) -> str:
        routed.append(text)
        return "unexpected"

    async def post_message(channel_id: str, message: str) -> dict[str, str]:
        posted.append((channel_id, message))
        return {"channel": "DADMIN", "ts": "1.234"}

    runtime.config.slack.manager_queue_url = "http://192.168.4.87:8765/exceptions"
    runtime.config.slack.worker_portal_beta_slack_user_ids = ["U01SWQKDTBM"]
    runtime.state_store = StateStore(tmp_path / "state.sqlite3")
    runtime.refresh_configuration = refresh_configuration  # type: ignore[method-assign]
    runtime.admin_router = SimpleNamespace(handle_plain_text=handle_plain_text)
    runtime.slack = SimpleNamespace(post_message=post_message)
    runtime.roster_by_slack_id = {}

    asyncio.run(
        runtime.handle_slack_direct_message(
            SimpleNamespace(),
            {
                "user": "U01SWQKDTBM",
                "text": "portal *Sent using* <@U0BATRYF16C|ChatGPT>",
                "ts": "1785859200.0",
            },
        )
    )

    assert routed == []
    assert posted[0][0] == "U01SWQKDTBM"
    assert "http://192.168.4.87:8765/portal?token=" in posted[0][1]
    assert "same durable work session" in posted[0][1]
    assert "VPN" not in posted[0][1]


def test_slack_worker_portal_command_returns_own_live_link(tmp_path: Path) -> None:
    runtime = _build_runtime()
    aj = UserProfile(
        user_key="AJ",
        display_name="AJ Torres",
        slack_user_id="U095NMY2U4R",
        clickup_user_id="456",
    )
    posted: list[tuple[str, str]] = []

    async def refresh_configuration(*_args, **_kwargs):
        return None

    async def post_message(channel_id: str, message: str) -> dict[str, str]:
        posted.append((channel_id, message))
        return {"channel": "DAJ", "ts": "1.234"}

    runtime.config.slack.manager_queue_url = "http://192.168.4.87:8765/exceptions"
    runtime.config.slack.worker_portal_beta_slack_user_ids = ["U01SWQKDTBM", "U095NMY2U4R"]
    runtime.state_store = StateStore(tmp_path / "state.sqlite3")
    runtime.refresh_configuration = refresh_configuration  # type: ignore[method-assign]
    runtime.slack = SimpleNamespace(post_message=post_message)
    runtime.roster_by_slack_id = {aj.slack_user_id: aj}

    asyncio.run(
        runtime.handle_slack_direct_message(
            SimpleNamespace(),
            {
                "user": "U095NMY2U4R",
                "text": "portal *Sent using* <@U0BATRYF16C|ChatGPT>",
                "ts": "1785859200.0",
            },
        )
    )

    assert posted[0][0] == "U095NMY2U4R"
    assert "http://192.168.4.87:8765/portal?token=" in posted[0][1]
    assert "same durable work session" in posted[0][1]
    assert "Slack DM as the fallback" in posted[0][1]
    assert "VPN" not in posted[0][1]


def test_short_rest_stays_paid_and_requires_return_check_in() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def send_dm(_client, _user, _session, content: str, _now, **_kwargs):
        sent.append(content)
        return None

    runtime._send_dm = send_dm  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
    )
    running_timer = {
        "task_id": "task-1",
        "task_name": "Build fixture",
        "started_at": "2026-07-28T09:00:00-07:00",
    }
    session = SessionState(
        user_key="alex",
        session_date="2026-07-28",
        stage="active",
        clocked_in_at="2026-07-28T09:00:00-07:00",
        work_segments=[
            {
                "clocked_in_at": "2026-07-28T09:00:00-07:00",
                "clocked_out_at": None,
            }
        ],
        metadata={
            "active_clickup_task_id": "task-1",
            "active_clickup_task_name": "Build fixture",
            "clickup_time_tracking": running_timer,
        },
    )

    started = asyncio.run(
        runtime._maybe_start_short_rest_break(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-07-28T12:00:00-07:00"),
        )
    )

    assert started is True
    assert session.metadata["short_rest_break_active"]["deadline_at"] == (
        "2026-07-28T12:10:00-07:00"
    )
    assert "closed_at" not in running_timer
    assert session.work_segments[-1]["clocked_out_at"] is None
    assert "Reply `back from break`" in sent[-1]

    returned_at = datetime.fromisoformat("2026-07-28T12:08:00-07:00")
    inbound = MessageRecord(
        message_id="rest-return",
        direction="inbound",
        author_id=1,
        created_at=returned_at,
        content="I'm back from my short break.",
        attachments=[],
    )
    asyncio.run(
        runtime._handle_short_rest_break_message(
            SimpleNamespace(),
            user,
            session,
            inbound,
            detect_signals(inbound.content),
            returned_at,
        )
    )

    assert "short_rest_break_active" not in session.metadata
    assert session.metadata["short_rest_breaks"][0]["outcome"] == "returned"
    assert session.stage == "active"
    assert session.clocked_out_at is None
    assert "8m" in sent[-1]


def test_short_rest_over_ten_minutes_clocks_out_at_exact_cutoff_without_admin_dm() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def send_dm(_client, _user, _session, content: str, _now, **_kwargs):
        sent.append(content)
        return None

    async def finalize_day(*_args, **_kwargs):
        return "Task timer stopped."

    async def fail_admin_notice(*_args, **_kwargs):
        raise AssertionError("resolved auto clock-out must not DM admins")

    runtime._send_dm = send_dm  # type: ignore[method-assign]
    runtime._finalize_clickup_day = finalize_day  # type: ignore[method-assign]
    runtime._safe_send_compliance_admin_notice = fail_admin_notice  # type: ignore[method-assign]
    user = UserProfile(user_key="alex", display_name="Alex", discord_user_id=1)
    rest_record = {
        "started_at": "2026-07-28T12:00:00-07:00",
        "deadline_at": "2026-07-28T12:10:00-07:00",
        "limit_minutes": 10,
        "task_id": "task-1",
        "task_name": "Build fixture",
    }
    session = SessionState(
        user_key="alex",
        session_date="2026-07-28",
        stage="active",
        clocked_in_at="2026-07-28T09:00:00-07:00",
        work_segments=[
            {
                "clocked_in_at": "2026-07-28T09:00:00-07:00",
                "clocked_out_at": None,
            }
        ],
        metadata={
            "active_clickup_task_id": "task-1",
            "active_clickup_task_name": "Build fixture",
            "short_rest_break_active": dict(rest_record),
            "short_rest_breaks": [dict(rest_record)],
        },
    )

    at_limit = asyncio.run(
        runtime._maybe_check_short_rest_break(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-07-28T12:10:00-07:00"),
        )
    )
    assert at_limit is False
    assert session.stage == "active"

    changed = asyncio.run(
        runtime._maybe_check_short_rest_break(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-07-28T12:11:00-07:00"),
        )
    )

    assert changed is True
    assert session.stage == "clocked_out"
    assert session.clocked_out_at == "2026-07-28T12:10:00-07:00"
    assert session.work_segments[-1]["clocked_out_at"] == "2026-07-28T12:10:00-07:00"
    assert session.metadata["short_rest_breaks"][0]["outcome"] == "auto_clocked_out"
    assert session.metadata["compliance_events"][0]["event_type"] == (
        "short_rest_auto_clocked_out"
    )
    assert "clocked you out effective 12:10 PM" in sent[-1]


def test_meal_compliance_auto_pauses_worker_without_admin_interruption_at_five_hours() -> None:
    runtime = _build_runtime()
    user_messages: list[str] = []
    admin_messages: list[str] = []

    async def send_dm(_client, _user, _session, content: str, _now, **_kwargs):
        user_messages.append(content)
        return None

    async def send_admin(_client, content: str, **_kwargs):
        admin_messages.append(content)
        return ["George"]

    runtime._send_dm = send_dm
    runtime._send_admin_notice = send_admin
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-07-28",
        stage="active",
        clocked_in_at="2026-07-28T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-07-28T09:00:00-07:00", "clocked_out_at": None}],
    )

    changed = asyncio.run(
        runtime._maybe_check_meal_compliance(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-07-28T14:01:00-07:00"),
        )
    )

    assert changed is True
    assert any("automatically paused your work time" in message for message in user_messages)
    assert session.stage == "on_lunch_break"
    assert session.metadata["meal_auto_pause_at"]
    assert admin_messages == []
    assert [event["event_type"] for event in session.metadata["compliance_events"]] == [
        "meal_auto_paused",
    ]


def test_meal_compliance_auto_pause_notifies_slack_only_worker() -> None:
    runtime = _build_runtime()
    posted: list[tuple[str, str]] = []

    async def post_message(channel_id: str, message: str) -> dict[str, str]:
        posted.append((channel_id, message))
        return {"channel": "D123", "ts": str(len(posted))}

    async def safe_admin_notice(*_args, **_kwargs):
        return None

    runtime.slack = SimpleNamespace(post_message=post_message)
    runtime._safe_send_compliance_admin_notice = safe_admin_notice  # type: ignore[method-assign]
    user = UserProfile(
        user_key="sam",
        display_name="Sam",
        slack_user_id="U123",
        preferred_transport="slack",
        meal_tracking_required=True,
    )
    session = SessionState(
        user_key="sam",
        session_date="2026-07-28",
        stage="active",
        clocked_in_at="2026-07-28T09:00:00-07:00",
        work_segments=[
            {
                "clocked_in_at": "2026-07-28T09:00:00-07:00",
                "clocked_out_at": None,
            }
        ],
    )

    changed = asyncio.run(
        runtime._maybe_check_meal_compliance(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-07-28T14:00:00-07:00"),
        )
    )

    assert changed is True
    assert session.stage == "on_lunch_break"
    assert len(posted) == 1
    assert posted[0][0] == "U123"
    assert "automatically paused your work time" in posted[0][1]


def test_meal_compliance_sends_direct_warning_before_auto_pause() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now, **_kwargs):
        sent.append(content)
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(user_key="alex", display_name="Alex", discord_user_id=1)
    session = SessionState(
        user_key="alex",
        session_date="2026-07-28",
        stage="active",
        clocked_in_at="2026-07-28T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-07-28T09:00:00-07:00", "clocked_out_at": None}],
    )

    changed = asyncio.run(
        runtime._maybe_check_meal_compliance(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-07-28T13:30:00-07:00"),
        )
    )

    assert changed is True
    assert len(sent) == 1
    assert "30 minutes" in sent[0]
    assert session.stage == "active"
    assert session.metadata["meal_compliance_warning_at"]


def test_overtime_compliance_auto_clocks_out_at_limit() -> None:
    runtime = _build_runtime()
    user_messages: list[str] = []
    admin_messages: list[str] = []

    async def send_dm(_client, _user, _session, content: str, _now, **_kwargs):
        user_messages.append(content)
        return None

    async def send_admin(_client, content: str, **_kwargs):
        admin_messages.append(content)
        return ["George"]

    def refresh_summary(session: SessionState, _now: datetime) -> None:
        session.time_summary["clocked_in_total_seconds"] = 8 * 60 * 60 + 60

    runtime._send_dm = send_dm
    runtime._send_admin_notice = send_admin
    runtime._refresh_session_time_summary = refresh_summary
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
        gusto_entity_uuid="gusto-alex",
        overtime_approval_required=True,
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-07-28",
        stage="active",
        clocked_in_at="2026-07-28T09:00:00-07:00",
    )

    changed = asyncio.run(
        runtime._maybe_check_overtime_compliance(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-07-28T17:01:00-07:00"),
        )
    )

    assert changed is True
    assert any("automatically clocked you out" in message for message in user_messages)
    assert admin_messages == []
    assert session.clocked_out_at == "2026-07-28T17:01:00-07:00"
    assert session.stage == "clocked_out"


def test_overtime_compliance_auto_clock_out_notifies_slack_only_worker() -> None:
    runtime = _build_runtime()
    posted: list[tuple[str, str]] = []

    async def post_message(channel_id: str, message: str) -> dict[str, str]:
        posted.append((channel_id, message))
        return {"channel": "D123", "ts": str(len(posted))}

    async def safe_admin_notice(*_args, **_kwargs):
        return None

    async def finalize_day(*_args, **_kwargs):
        return None

    def refresh_summary(session: SessionState, _now: datetime) -> None:
        session.time_summary["clocked_in_total_seconds"] = 8 * 60 * 60

    runtime.slack = SimpleNamespace(post_message=post_message)
    runtime._safe_send_compliance_admin_notice = safe_admin_notice  # type: ignore[method-assign]
    runtime._finalize_clickup_day = finalize_day  # type: ignore[method-assign]
    runtime._refresh_session_time_summary = refresh_summary  # type: ignore[method-assign]
    user = UserProfile(
        user_key="sam",
        display_name="Sam",
        slack_user_id="U123",
        preferred_transport="slack",
        overtime_approval_required=True,
    )
    session = SessionState(
        user_key="sam",
        session_date="2026-07-28",
        stage="active",
        clocked_in_at="2026-07-28T09:00:00-07:00",
        work_segments=[
            {
                "clocked_in_at": "2026-07-28T09:00:00-07:00",
                "clocked_out_at": None,
            }
        ],
    )

    changed = asyncio.run(
        runtime._maybe_check_overtime_compliance(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-07-28T17:00:00-07:00"),
        )
    )

    assert changed is True
    assert session.stage == "clocked_out"
    assert len(posted) == 1
    assert posted[0][0] == "U123"
    assert "automatically clocked you out" in posted[0][1]


def test_portal_quality_deadline_stops_future_time_and_requires_manager_release() -> None:
    runtime = _build_runtime()
    messages: list[str] = []

    async def send_dm(_client, _user, _session, content: str, _now, **_kwargs):
        messages.append(content)
        return None

    async def finalize_day(*_args, **_kwargs):
        return "Task timer stopped."

    runtime._send_dm = send_dm  # type: ignore[method-assign]
    runtime._finalize_clickup_day = finalize_day  # type: ignore[method-assign]
    user = UserProfile(
        user_key="aj",
        display_name="AJ",
        slack_user_id="UAJ",
        preferred_transport="slack",
    )
    session = SessionState(
        user_key="aj",
        session_date="2026-08-10",
        stage="active",
        clocked_in_at="2026-08-10T09:00:00-07:00",
        work_segments=[
            {"clocked_in_at": "2026-08-10T09:00:00-07:00", "clocked_out_at": None}
        ],
        metadata={
            "portal_quality_warning": {
                "started_at": "2026-08-10T09:50:00-07:00",
                "deadline_at": "2026-08-10T10:00:00-07:00",
                "context": "checkpoint",
                "reasons": ["The update repeated a prior submission."],
            }
        },
    )

    changed = asyncio.run(
        runtime._maybe_enforce_portal_quality_warning(
            None,
            user,
            session,
            datetime.fromisoformat("2026-08-10T10:02:00-07:00"),
        )
    )

    assert changed is True
    assert session.clocked_out_at == "2026-08-10T10:00:00-07:00"
    assert session.work_segments[0]["clocked_out_at"] == "2026-08-10T10:00:00-07:00"
    assert session.metadata["portal_quality_restart_blocked"]["status"] == "manager_approval_required"
    assert session.metadata["auto_clock_out_reason"] == "Worker portal quality correction deadline expired."
    assert any("Time already recorded remains intact" in message for message in messages)

    approved = asyncio.run(
        runtime.approve_portal_quality_restart(
            None,
            user,
            session,
            approved_by="Erik",
            comments="Reviewed the corrected fixture result and evidence target.",
            now=datetime.fromisoformat("2026-08-10T10:05:00-07:00"),
        )
    )
    assert "Approved tracked-work restart" in approved
    assert "portal_quality_restart_blocked" not in session.metadata
    assert session.stage == "clocked_out"
    assert any("approved a tracked-work restart" in message for message in messages)


def test_overtime_compliance_notifies_admin_only_when_risk_remains_unresolved() -> None:
    runtime = _build_runtime()
    runtime.config.labor.auto_clock_out_at_overtime_limit = False
    user_messages: list[str] = []
    admin_messages: list[str] = []

    async def send_dm(_client, _user, _session, content: str, _now, **_kwargs):
        user_messages.append(content)
        return None

    async def send_admin(_client, content: str, **_kwargs):
        admin_messages.append(content)
        return ["George"]

    def refresh_summary(session: SessionState, _now: datetime) -> None:
        session.time_summary["clocked_in_total_seconds"] = 8 * 60 * 60

    runtime._send_dm = send_dm  # type: ignore[method-assign]
    runtime._send_admin_notice = send_admin  # type: ignore[method-assign]
    runtime._refresh_session_time_summary = refresh_summary  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        overtime_approval_required=True,
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-07-28",
        stage="active",
        clocked_in_at="2026-07-28T09:00:00-07:00",
    )

    changed = asyncio.run(
        runtime._maybe_check_overtime_compliance(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-07-28T17:00:00-07:00"),
        )
    )

    assert changed is True
    assert session.stage == "active"
    assert "Stop work and clock out" in user_messages[0]
    assert len(admin_messages) == 1
    assert "Unresolved overtime risk" in admin_messages[0]


def test_overtime_compliance_warns_before_limit_without_clocking_out() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now, **_kwargs):
        sent.append(content)
        return None

    def refresh_summary(session: SessionState, _now: datetime) -> None:
        session.time_summary["clocked_in_total_seconds"] = 7 * 60 * 60 + 30 * 60

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._refresh_session_time_summary = refresh_summary  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        overtime_approval_required=True,
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-07-28",
        stage="active",
        clocked_in_at="2026-07-28T09:00:00-07:00",
    )

    changed = asyncio.run(
        runtime._maybe_check_overtime_compliance(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-07-28T16:30:00-07:00"),
        )
    )

    assert changed is True
    assert len(sent) == 1
    assert "30 minutes left" in sent[0]
    assert session.stage == "active"
    assert session.clocked_out_at is None


def test_overtime_compliance_does_not_require_gusto_mapping() -> None:
    runtime = _build_runtime()

    async def fake_send(*_args, **_kwargs):
        return None

    async def fake_admin(*_args, **_kwargs):
        return ["George"]

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._send_admin_notice = fake_admin  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        worker_type="intern",
        overtime_approval_required=True,
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-07-28",
        stage="active",
        clocked_in_at="2026-07-28T09:00:00-07:00",
        time_summary={"clocked_in_total_seconds": 9 * 60 * 60},
    )

    changed = asyncio.run(
        runtime._maybe_check_overtime_compliance(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-07-28T18:00:00-07:00"),
        )
    )

    assert changed is True
    assert session.stage == "clocked_out"
    assert session.metadata["overtime_admin_alert_at"]


def test_post_lunch_guidance_projects_clock_out_time_for_nonexempt_employee() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        worker_type="employee_hourly",
        gusto_entity_uuid="gusto-alex",
        overtime_approval_required=True,
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-07-28",
        stage="active",
        clocked_in_at="2026-07-28T09:00:00-07:00",
        work_segments=[
            {
                "clocked_in_at": "2026-07-28T09:00:00-07:00",
                "clocked_out_at": None,
            }
        ],
        metadata={
            "lunch_started_at": "2026-07-28T12:30:00-07:00",
            "lunch_ended_at": "2026-07-28T13:00:00-07:00",
        },
    )

    guidance = runtime._post_lunch_clock_out_guidance(
        user,
        session,
        datetime.fromisoformat("2026-07-28T13:00:00-07:00"),
    )

    assert "plan to clock out by about 5:30 PM" in guidance


def test_queued_meal_guidance_is_appended_to_next_normal_update() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        worker_type="intern",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-07-28",
        stage="active",
        clocked_in_at="2026-07-28T09:00:00-07:00",
        work_segments=[
            {
                "clocked_in_at": "2026-07-28T09:00:00-07:00",
                "clocked_out_at": None,
            }
        ],
        metadata={"meal_guidance_queued_at": "2026-07-28T13:30:00-07:00"},
    )
    now = datetime.fromisoformat("2026-07-28T13:35:00-07:00")

    content, included = runtime._append_queued_meal_guidance(
        user,
        session,
        "How is your project going?",
        now,
    )

    assert included is True
    assert "How is your project going?" in content
    assert "30-minute lunch by 2:00 PM" in content
    assert "real, uninterrupted break" in content


def _activation_result(task_id: str, task_name: str, *, clickup_status_name: str | None = "in progress") -> TaskActivationResult:
    return TaskActivationResult(
        task_id=task_id,
        task_name=task_name,
        clickup_status_name=clickup_status_name,
        tracking_state={
            "timer_running": True,
            "timer_task_id": task_id,
            "timer_task_name": task_name,
            "timer_note": None,
        },
    )


def test_runtime_prompts_for_missing_active_task_onboarding() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
    )
    now = datetime.fromisoformat("2026-05-28T10:00:00")
    changed = asyncio.run(runtime._maybe_prompt_task_onboarding(SimpleNamespace(), user, session, now))
    assert changed is True
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_onboarding"
    assert prompt["step"] == "select_task"
    assert any("task name or task ID" in item for item in sent)


def test_runtime_prompts_for_missing_start_photo_every_onboarding_interval() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="awaiting_start_photo",
        clocked_in_at="2026-05-28T09:00:00",
        awaiting_start_photo=True,
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "step": "photo",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
    }
    session.metadata["last_task_onboarding_prompt_at"] = "2026-05-28T09:00:00"

    changed = asyncio.run(
        runtime._maybe_prompt_task_onboarding(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T09:06:00"),
        )
    )

    assert changed is True
    assert sent == [
        "I still need the task-start picture for `formalize project tree`. "
        "The task will not be officially tracked until you upload that starting image."
    ]
    assert session.metadata["last_task_onboarding_prompt_at"] == "2026-05-28T09:06:00"


def test_runtime_does_not_prompt_for_start_photo_before_interval_elapses() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="awaiting_start_photo",
        clocked_in_at="2026-05-28T09:00:00",
        awaiting_start_photo=True,
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "step": "photo",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
    }
    session.metadata["last_task_onboarding_prompt_at"] = "2026-05-28T09:03:00"

    changed = asyncio.run(
        runtime._maybe_prompt_task_onboarding(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T09:06:00"),
        )
    )

    assert changed is False
    assert sent == []


def test_runtime_photo_step_text_reply_repeats_tracking_warning() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="awaiting_start_photo",
        clocked_in_at="2026-05-28T09:00:00",
        awaiting_start_photo=True,
    )
    prompt = {
        "type": "task_onboarding",
        "step": "photo",
        "source": "daily_clock_in",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "draft": {},
    }
    session.metadata["clickup_prompt"] = prompt
    inbound = MessageRecord(
        message_id="msg-photo-missing",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T09:06:00"),
        content="I already started working on it",
        attachments=[],
    )

    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T09:06:00"),
            prompt,
        )
    )

    assert handled is True
    assert sent == [
        "I still need the task-start picture for `formalize project tree`. "
        "The task will not be officially tracked until you upload that starting image."
    ]


def test_runtime_does_not_prompt_task_onboarding_during_clock_out_artifacts() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="awaiting_clock_out_artifacts",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "step": "estimated_duration",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
    }

    changed = asyncio.run(
        runtime._maybe_prompt_task_onboarding(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T10:00:00"),
        )
    )

    assert changed is False
    assert sent == []


def test_runtime_clock_out_request_bypasses_missing_task_onboarding_prompt() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "source": "missing_active_task",
        "step": "select_task",
        "reason": "No active ClickUp task with running tracking was confirmed while clocked in.",
        "draft": {},
    }

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    inbound = MessageRecord(
        message_id="msg-clock-out",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T17:07:00"),
        content="can i clock out?",
        attachments=[],
    )

    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            inbound,
            inbound.created_at,
        )
    )

    assert session.stage == "awaiting_clock_out_artifacts"
    assert session.awaiting_clock_out_photo is True
    assert session.awaiting_clock_out_summary is True
    assert "clock_out_summary_message_id" not in session.metadata
    assert "clickup_prompt" not in session.metadata
    assert sent == ["clock out"]


def test_runtime_reported_clock_out_request_bypasses_missing_task_onboarding_prompt() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    user = UserProfile(
        user_key="christie",
        display_name="Christie",
        discord_user_id=1,
        discord_username="christie",
        storage_folder_name="ChristieJackett",
    )
    session = SessionState(
        user_key="christie",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "source": "missing_active_task",
        "step": "select_task",
        "reason": "No active ClickUp task with running tracking was confirmed while clocked in.",
        "draft": {},
    }

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    inbound = MessageRecord(
        message_id="msg-clock-out-reported",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T17:07:00"),
        content="i have to go can i clock out?",
        attachments=[],
    )

    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            inbound,
            inbound.created_at,
        )
    )

    assert session.stage == "awaiting_clock_out_artifacts"
    assert session.awaiting_clock_out_photo is True
    assert session.awaiting_clock_out_summary is True
    assert "clock_out_summary_message_id" not in session.metadata
    assert "clickup_prompt" not in session.metadata
    assert sent == ["clock out"]


def test_runtime_onboarding_answer_that_mentions_future_clock_out_stays_in_onboarding() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=1,
        discord_username="navin",
        storage_folder_name="Navin",
    )
    session = SessionState(
        user_key="navin",
        session_date="2026-07-16",
        stage="awaiting_plan",
        clocked_in_at="2026-07-16T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-07-16T09:00:00-07:00", "clocked_out_at": None}],
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "source": "daily_clock_in",
        "step": "estimated_duration",
        "task_id": "cad-task",
        "task_name": "magnetic beneficiation machine",
        "draft": {},
    }

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    inbound = MessageRecord(
        message_id="msg-onboarding-duration",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-07-16T09:15:00-07:00"),
        content=(
            "Being project lead will take the duration of this project. I plan to write my daily status "
            "update in the afternoon or when I clock out. Continuing the CAD part will take 1-2 hours."
        ),
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    prompt = session.metadata["clickup_prompt"]
    assert session.stage == "awaiting_plan"
    assert prompt["step"] == "reconsider_threshold"
    assert prompt["draft"]["estimated_duration"] == inbound.content
    assert "clock_out_return_state" not in session.metadata
    assert sent == ["How long will you give this approach before you decide you are stuck or not producing good results?"]


def test_runtime_clock_out_cancellation_restores_interrupted_onboarding() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=1,
        discord_username="navin",
        storage_folder_name="Navin",
    )
    original_prompt = {
        "type": "task_onboarding",
        "source": "daily_clock_in",
        "step": "estimated_duration",
        "task_id": "cad-task",
        "task_name": "magnetic beneficiation machine",
        "draft": {"effectiveness": "The approach matches the current design constraints."},
    }
    session = SessionState(
        user_key="navin",
        session_date="2026-07-16",
        stage="awaiting_plan",
        clocked_in_at="2026-07-16T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-07-16T09:00:00-07:00", "clocked_out_at": None}],
        metadata={"clickup_prompt": deepcopy(original_prompt)},
    )

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    start = MessageRecord(
        message_id="msg-accidental-clock-out",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-07-16T09:15:00-07:00"),
        content="clock out",
        attachments=[],
    )
    cancel = MessageRecord(
        message_id="msg-cancel-clock-out",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-07-16T09:16:00-07:00"),
        content="no i did not mean to clock out",
        attachments=[],
    )

    asyncio.run(runtime._start_clock_out(SimpleNamespace(), user, session, start, start.created_at))
    assert session.stage == "awaiting_clock_out_artifacts"
    assert "clickup_prompt" not in session.metadata

    asyncio.run(runtime._handle_clock_out_artifacts(SimpleNamespace(), user, session, cancel, cancel.created_at))

    assert session.stage == "awaiting_plan"
    assert session.clocked_out_at is None
    assert session.awaiting_clock_out_photo is False
    assert session.awaiting_clock_out_summary is False
    assert session.metadata["clickup_prompt"] == original_prompt
    assert "clock_out_return_state" not in session.metadata
    assert "clock_out_summary_message_id" not in session.metadata
    assert sent == [
        "clock out",
        (
            "Okay, I canceled the clock-out process. You are still clocked in.\n\n"
            "How long do you expect this task to take? A rough answer like `45 minutes`, `2 hours`, "
            "or `half a day` is fine."
        ),
    ]


def test_runtime_clock_me_out_pollo_bypasses_waiting_for_admin_review() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    user = UserProfile(
        user_key="tony",
        display_name="Tony",
        discord_user_id=1,
        discord_username="tony",
        storage_folder_name="Tony",
    )
    session = SessionState(
        user_key="tony",
        session_date="2026-07-16",
        stage="awaiting_admin_review",
        clocked_in_at="2026-07-16T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-07-16T09:00:00-07:00", "clocked_out_at": None}],
    )

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    inbound = MessageRecord(
        message_id="msg-clock-me-out-pollo",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-07-16T16:13:00-07:00"),
        content="CLOCK ME OUT POLLO",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert session.stage == "awaiting_clock_out_artifacts"
    assert session.awaiting_clock_out_photo is True
    assert session.awaiting_clock_out_summary is True
    assert sent == ["clock out"]


def test_runtime_clock_out_photo_only_follow_up_does_not_finish_session() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    finalized = False

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)

    async def fake_finalize(*_args, **_kwargs):
        nonlocal finalized
        finalized = True
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._finalize_clickup_day = fake_finalize  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        clocked_in_at="2026-06-10T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-10T09:00:00-07:00", "clocked_out_at": None}],
    )
    start = MessageRecord(
        message_id="msg-clock-out-start",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T17:00:00-07:00"),
        content="clock out",
        attachments=[],
    )
    photo_only = MessageRecord(
        message_id="msg-clock-out-photo",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T17:02:00-07:00"),
        content="",
        attachments=[
            AttachmentRecord(
                filename="final.jpg",
                url="https://example.com/final.jpg",
                content_type="image/jpeg",
                size=1,
                local_path="/tmp/final.jpg",
            )
        ],
    )

    asyncio.run(runtime._start_clock_out(SimpleNamespace(), user, session, start, start.created_at))
    asyncio.run(runtime._handle_clock_out_artifacts(SimpleNamespace(), user, session, photo_only, photo_only.created_at))

    assert session.stage == "awaiting_clock_out_artifacts"
    assert session.awaiting_clock_out_photo is False
    assert session.awaiting_clock_out_summary is True
    assert finalized is False
    assert sent == [
        "clock out",
        "I still need the written wrap-up before I close out today.",
    ]


def test_runtime_clock_out_text_only_follow_up_does_not_finish_session() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    finalized = False

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)

    async def fake_finalize(*_args, **_kwargs):
        nonlocal finalized
        finalized = True
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._finalize_clickup_day = fake_finalize  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        clocked_in_at="2026-06-10T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-10T09:00:00-07:00", "clocked_out_at": None}],
    )
    start = MessageRecord(
        message_id="msg-clock-out-start",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T17:00:00-07:00"),
        content="clock out",
        attachments=[],
    )
    text_only = MessageRecord(
        message_id="msg-clock-out-wrap-up",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T17:03:00-07:00"),
        content="Wrapped the controller mount and need to align the rails tomorrow.",
        attachments=[],
    )

    asyncio.run(runtime._start_clock_out(SimpleNamespace(), user, session, start, start.created_at))
    asyncio.run(runtime._handle_clock_out_artifacts(SimpleNamespace(), user, session, text_only, text_only.created_at))

    assert session.stage == "awaiting_clock_out_artifacts"
    assert session.awaiting_clock_out_photo is True
    assert session.awaiting_clock_out_summary is False
    assert session.metadata["clock_out_summary_message_id"] == "msg-clock-out-wrap-up"
    assert finalized is False
    assert sent == [
        "clock out",
        "I still need the picture before I close out today.",
    ]


def test_runtime_clock_out_photo_and_wrap_up_across_follow_up_messages_finishes_session() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    finalized = False

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)

    async def fake_finalize(*_args, **_kwargs):
        nonlocal finalized
        finalized = True
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._finalize_clickup_day = fake_finalize  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        clocked_in_at="2026-06-10T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-10T09:00:00-07:00", "clocked_out_at": None}],
    )
    start = MessageRecord(
        message_id="msg-clock-out-start",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T17:00:00-07:00"),
        content="clock out",
        attachments=[],
    )
    photo_only = MessageRecord(
        message_id="msg-clock-out-photo",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T17:02:00-07:00"),
        content="",
        attachments=[
            AttachmentRecord(
                filename="final.jpg",
                url="https://example.com/final.jpg",
                content_type="image/jpeg",
                size=1,
                local_path="/tmp/final.jpg",
            )
        ],
    )
    wrap_up = MessageRecord(
        message_id="msg-clock-out-wrap-up",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T17:04:00-07:00"),
        content="Finished the rack fit check, confirmed the mount lines up, and will reprint the bracket tomorrow.",
        attachments=[],
    )

    asyncio.run(runtime._start_clock_out(SimpleNamespace(), user, session, start, start.created_at))
    asyncio.run(runtime._handle_clock_out_artifacts(SimpleNamespace(), user, session, photo_only, photo_only.created_at))
    asyncio.run(runtime._handle_clock_out_artifacts(SimpleNamespace(), user, session, wrap_up, wrap_up.created_at))

    assert session.stage == "clocked_out"
    assert session.clocked_out_at == "2026-06-10T17:04:00-07:00"
    assert session.awaiting_clock_out_photo is False
    assert session.awaiting_clock_out_summary is False
    assert session.metadata["clock_out_summary_message_id"] == "msg-clock-out-wrap-up"
    assert finalized is True
    assert sent[0] == "clock out"
    assert sent[1] == "I still need the written wrap-up before I close out today."
    assert "saved everything" in sent[2].lower()


def test_runtime_handle_incoming_message_serializes_clock_out_artifact_follow_up() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="ndango",
        display_name="Ndango",
        discord_user_id=1,
        discord_username="ndango",
        storage_folder_name="NdangoDinga",
    )
    runtime.roster_by_key[user.user_key] = user
    runtime.roster_by_discord_id[user.discord_user_id] = user
    session = SessionState(
        user_key="ndango",
        session_date="2026-06-22",
        stage="active",
        clocked_in_at="2026-06-22T08:55:00-07:00",
        intake_completed_at="2026-06-22T08:57:37-07:00",
        work_segments=[{"clocked_in_at": "2026-06-22T08:55:00-07:00", "clocked_out_at": None}],
    )
    runtime.state_store.save_session(session)
    sent: list[str] = []
    prompt_started = asyncio.Event()
    release_prompt = asyncio.Event()

    async def fake_send(_client, _user, _session, content: str, now: datetime, *, view=None):
        del _client, _user, _session, view
        sent.append(content)
        if content == "clock out":
            prompt_started.set()
            await release_prompt.wait()
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=now,
            content=content,
            attachments=[],
        )

    async def fake_build(message, _session, _user):
        del _session, _user
        return MessageRecord(
            message_id=str(message.id),
            direction="inbound",
            author_id=message.author.id,
            created_at=message.created_at,
            content=message.content,
            attachments=list(message.attachments),
        )

    async def fake_finalize(*_args, **_kwargs):
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._build_inbound_record = fake_build  # type: ignore[method-assign]
    runtime._finalize_clickup_day = fake_finalize  # type: ignore[method-assign]

    async def run_test() -> None:
        client = SimpleNamespace()
        first = SimpleNamespace(
            id="msg-clock-out",
            author=SimpleNamespace(id=user.discord_user_id),
            created_at=datetime.fromisoformat("2026-06-22T17:14:30-07:00"),
            content="clock out",
            attachments=[],
        )
        second = SimpleNamespace(
            id="msg-artifacts",
            author=SimpleNamespace(id=user.discord_user_id),
            created_at=datetime.fromisoformat("2026-06-22T17:16:21-07:00"),
            content=(
                "Today we reprinted our 3d assembly to correct for some mistakes. "
                "We need to reprint the rack again tomorrow."
            ),
            attachments=[
                AttachmentRecord(
                    filename="final.jpg",
                    url="https://example.com/final.jpg",
                    content_type="image/jpeg",
                    size=1,
                    local_path="/tmp/final.jpg",
                    original_filename="IMG_7765.jpg",
                    description="final assembly photo",
                    tags=["assembly"],
                    analysis_model=None,
                )
            ],
        )
        first_task = asyncio.create_task(runtime.handle_incoming_message(client, first))
        await prompt_started.wait()
        second_task = asyncio.create_task(runtime.handle_incoming_message(client, second))
        await asyncio.sleep(0)
        assert second_task.done() is False
        release_prompt.set()
        await first_task
        await second_task

    asyncio.run(run_test())

    stored = runtime.state_store.get_session(user.user_key, session.session_date)
    assert stored.stage == "clocked_out"
    assert stored.clocked_out_at == "2026-06-22T17:16:21-07:00"
    assert stored.awaiting_clock_out_photo is False
    assert stored.awaiting_clock_out_summary is False
    assert stored.metadata["clock_out_summary_message_id"] == "msg-artifacts"
    assert sent[0] == "clock out"
    assert "saved" in sent[1].lower()


def test_runtime_scheduler_skips_progress_automation_during_clock_out_artifacts() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="ndango",
        display_name="Ndango",
        discord_user_id=1,
        discord_username="ndango",
        storage_folder_name="NdangoDinga",
    )
    runtime.roster_by_key[user.user_key] = user
    session = SessionState(
        user_key="ndango",
        session_date="2026-06-22",
        stage="awaiting_clock_out_artifacts",
        clocked_in_at="2026-06-22T08:55:00-07:00",
        intake_completed_at="2026-06-22T08:57:37-07:00",
        awaiting_clock_out_photo=True,
        awaiting_clock_out_summary=False,
        last_user_message_at="2026-06-22T17:16:21-07:00",
        pending_clickup_sync=True,
        work_segments=[{"clocked_in_at": "2026-06-22T08:55:00-07:00", "clocked_out_at": None}],
    )
    runtime.state_store.save_session(session)
    auto_clock_out_called = False
    follow_up_called = False
    flush_called = False

    async def fake_auto_clock_out(*_args, **_kwargs):
        nonlocal auto_clock_out_called
        auto_clock_out_called = True
        return True

    async def fake_follow_up(*_args, **_kwargs):
        nonlocal follow_up_called
        follow_up_called = True
        return True

    async def fake_flush(*_args, **_kwargs):
        nonlocal flush_called
        flush_called = True
        return True

    runtime._maybe_auto_clock_out_inactive = fake_auto_clock_out  # type: ignore[method-assign]
    runtime._maybe_send_follow_up = fake_follow_up  # type: ignore[method-assign]
    runtime._maybe_flush_clickup = fake_flush  # type: ignore[method-assign]

    asyncio.run(
        runtime._run_scheduler_for_user(
            SimpleNamespace(),
            user,
            datetime.fromisoformat("2026-06-22T17:22:30-07:00"),
        )
    )

    stored = runtime.state_store.get_session(user.user_key, session.session_date)
    assert auto_clock_out_called is False
    assert follow_up_called is False
    assert flush_called is False
    assert stored.stage == "awaiting_clock_out_artifacts"
    assert stored.clocked_out_at is None
    assert "auto_clock_out_at" not in stored.metadata


def test_worker_schedule_and_planned_time_off_control_proactive_prompts() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        timezone="America/Los_Angeles",
        regular_workdays=["monday", "wednesday", "friday"],
        planned_time_off=["2026-06-22", "2026-07-01..2026-07-03"],
    )

    assert runtime.is_user_scheduled_to_work(
        user, datetime.fromisoformat("2026-06-24T10:00:00-07:00")
    ) is True
    assert runtime.is_user_scheduled_to_work(
        user, datetime.fromisoformat("2026-06-23T10:00:00-07:00")
    ) is False
    assert runtime.is_user_scheduled_to_work(
        user, datetime.fromisoformat("2026-06-22T10:00:00-07:00")
    ) is False
    assert runtime.is_user_scheduled_to_work(
        user, datetime.fromisoformat("2026-07-03T10:00:00-07:00")
    ) is False


def test_worker_typical_start_time_controls_clock_in_window() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        timezone="America/Los_Angeles",
        typical_start_time="10:30",
        typical_end_time="16:00",
    )
    session = SessionState(user_key="alex", session_date="2026-06-22")

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]

    before = asyncio.run(
        runtime._maybe_send_clock_in(
            SimpleNamespace(), user, session, datetime.fromisoformat("2026-06-22T10:15:00-07:00")
        )
    )
    inside = asyncio.run(
        runtime._maybe_send_clock_in(
            SimpleNamespace(), user, session, datetime.fromisoformat("2026-06-22T10:30:00-07:00")
        )
    )

    assert before is False
    assert inside is True
    assert sent == ["clock in"]


def test_meal_and_overtime_enforcement_still_run_when_worker_is_clocked_in_off_schedule() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        timezone="America/Los_Angeles",
        regular_workdays=["monday"],
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-27",
        stage="active",
        clocked_in_at="2026-06-27T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-27T09:00:00-07:00", "clocked_out_at": None}],
    )
    runtime.state_store.save_session(session)
    called: list[str] = []

    async def fake_false(*_args, **_kwargs):
        return False

    async def fake_meal(*_args, **_kwargs):
        called.append("meal")
        return False

    async def fake_overtime(*_args, **_kwargs):
        called.append("overtime")
        return False

    for name in (
        "_maybe_send_auto_clock_out_warning",
        "_maybe_auto_clock_out_inactive",
        "_maybe_prompt_task_onboarding",
        "_maybe_send_lunch_break_check_in",
        "_maybe_assess_pending_follow_up_probe",
        "_maybe_timeout_progress_probe",
        "_maybe_send_follow_up",
        "_maybe_alert_admin",
        "_maybe_flush_clickup",
        "_maybe_post_slack_daily_update",
    ):
        setattr(runtime, name, fake_false)
    runtime._maybe_check_meal_compliance = fake_meal  # type: ignore[method-assign]
    runtime._maybe_check_overtime_compliance = fake_overtime  # type: ignore[method-assign]

    asyncio.run(
        runtime._run_scheduler_for_user(
            SimpleNamespace(), user, datetime.fromisoformat("2026-06-27T14:00:00-07:00")
        )
    )

    assert called == ["meal", "overtime"]


def test_runtime_clock_out_artifacts_finalize_before_later_scheduler_tick() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    user = UserProfile(
        user_key="ndango",
        display_name="Ndango",
        discord_user_id=1,
        discord_username="ndango",
        storage_folder_name="NdangoDinga",
    )
    runtime.roster_by_key[user.user_key] = user
    session = SessionState(
        user_key="ndango",
        session_date="2026-06-22",
        stage="active",
        clocked_in_at="2026-06-22T08:55:00-07:00",
        intake_completed_at="2026-06-22T08:57:37-07:00",
        work_segments=[{"clocked_in_at": "2026-06-22T08:55:00-07:00", "clocked_out_at": None}],
    )

    async def fake_send(_client, _user, _session, content: str, now: datetime, *, view=None):
        del _client, _user, _session, view
        sent.append(content)
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=now,
            content=content,
            attachments=[],
        )

    async def fake_finalize(*_args, **_kwargs):
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._finalize_clickup_day = fake_finalize  # type: ignore[method-assign]
    clock_out = MessageRecord(
        message_id="msg-clock-out",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-22T17:14:30-07:00"),
        content="clock out",
        attachments=[],
    )
    artifacts = MessageRecord(
        message_id="msg-artifacts",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-22T17:16:21-07:00"),
        content=(
            "Today we reprinted our 3d assembly to correct for some mistakes. "
            "We need to reprint the rack again tomorrow."
        ),
        attachments=[
            AttachmentRecord(
                filename="final.jpg",
                url="https://example.com/final.jpg",
                content_type="image/jpeg",
                size=1,
                local_path="/tmp/final.jpg",
                original_filename="IMG_7765.jpg",
                description="final assembly photo",
                tags=["assembly"],
                analysis_model=None,
            )
        ],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, clock_out, clock_out.created_at))
    assert session.stage == "awaiting_clock_out_artifacts"

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, artifacts, artifacts.created_at))
    assert session.stage == "clocked_out"
    assert session.clocked_out_at == "2026-06-22T17:16:21-07:00"

    runtime.state_store.save_session(session)
    asyncio.run(
        runtime._run_scheduler_for_user(
            SimpleNamespace(),
            user,
            datetime.fromisoformat("2026-06-22T17:22:30-07:00"),
        )
    )

    stored = runtime.state_store.get_session(user.user_key, session.session_date)
    assert stored.stage == "clocked_out"
    assert stored.clocked_out_at == "2026-06-22T17:16:21-07:00"
    assert "auto_clock_out_at" not in stored.metadata
    assert sent[0] == "clock out"
    assert "saved" in sent[1].lower()


def test_runtime_follow_up_reply_starts_aggregation_without_immediate_probe() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        intake_completed_at="2026-05-28T09:20:00",
    )
    session.metadata["pending_follow_up"] = {
        "message_id": "follow-1",
        "question_text": "What progress have you made since the last check-in?",
        "sent_at": "2026-05-28T10:00:00",
        "awaiting_reply": True,
    }
    inbound = MessageRecord(
        message_id="msg-dry",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T10:00:15"),
        content="still working",
        attachments=[],
    )

    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            inbound,
            inbound.created_at,
        )
    )

    aggregate = session.metadata["follow_up_response_aggregation"]
    assert aggregate["question_text"] == "What progress have you made since the last check-in?"
    assert aggregate["reply_message_ids"] == ["msg-dry"]
    assert aggregate["reply_fragments"] == ["still working"]
    assert "clickup_prompt" not in session.metadata


def test_runtime_follow_up_reply_merges_second_message_within_grace_window() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        intake_completed_at="2026-05-28T09:20:00",
    )
    session.metadata["pending_follow_up"] = {
        "message_id": "follow-1",
        "question_text": "What progress have you made since the last check-in?",
        "sent_at": "2026-05-28T10:00:00",
        "awaiting_reply": True,
    }

    first = MessageRecord(
        message_id="msg-1",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T10:00:15"),
        content="still wiring it",
        attachments=[],
    )
    second = MessageRecord(
        message_id="msg-2",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T10:00:35"),
        content="I got the motor spinning and I'm testing CAN now",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, first, first.created_at))
    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, second, second.created_at))

    aggregate = session.metadata["follow_up_response_aggregation"]
    assert aggregate["reply_message_ids"] == ["msg-1", "msg-2"]
    assert "motor spinning" in session.latest_status


def test_runtime_follow_up_probe_starts_after_one_minute_of_silence() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    admin_notices: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    async def fake_admin_notice(_client, content: str, *, target_admins=None, files=None, user=None, session=None, view_factory=None):
        del files, user, session
        admin_notices.append(content)
        if view_factory and target_admins:
            for admin in target_admins:
                assert view_factory(admin) is not None
        return [admin.name for admin in (target_admins or runtime.admin_profiles())]

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._send_admin_notice = fake_admin_notice  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        intake_completed_at="2026-05-28T09:20:00",
    )
    session.metadata["pending_follow_up"] = {
        "message_id": "follow-1",
        "question_text": "What progress have you made since the last check-in?",
        "sent_at": "2026-05-28T10:00:00",
        "awaiting_reply": True,
    }
    session.metadata["follow_up_response_aggregation"] = {
        "follow_up_message_id": "follow-1",
        "question_text": "What progress have you made since the last check-in?",
        "first_reply_message_id": "msg-dry",
        "reply_message_ids": ["msg-dry"],
        "reply_fragments": ["still working"],
        "attachment_count": 0,
        "first_reply_at": "2026-05-28T10:00:15",
        "last_reply_at": "2026-05-28T10:00:15",
        "grace_window_open": True,
        "previous_status": "Still wiring the controller.",
    }

    changed = asyncio.run(
        runtime._maybe_assess_pending_follow_up_probe(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T10:01:20"),
        )
    )

    assert changed is True
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "progress_probe"
    assert "follow_up_response_aggregation" not in session.metadata
    assert any("I still cannot tell what progress was made" in item for item in sent)
    assert any("Would you like to see their response to the progress probe?" in item for item in admin_notices)


def test_runtime_follow_up_probe_accepts_concrete_combined_reply() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        intake_completed_at="2026-05-28T09:20:00",
    )
    session.metadata["pending_follow_up"] = {
        "message_id": "follow-1",
        "question_text": "What progress have you made since the last check-in?",
        "sent_at": "2026-05-28T10:00:00",
        "awaiting_reply": True,
    }
    session.metadata["follow_up_response_aggregation"] = {
        "follow_up_message_id": "follow-1",
        "question_text": "What progress have you made since the last check-in?",
        "first_reply_message_id": "msg-1",
        "reply_message_ids": ["msg-1", "msg-2"],
        "reply_fragments": ["still wiring it", "I got the motor spinning and I'm testing CAN now"],
        "attachment_count": 0,
        "first_reply_at": "2026-05-28T10:00:15",
        "last_reply_at": "2026-05-28T10:00:35",
        "grace_window_open": True,
        "previous_status": "Still wiring the controller.",
    }

    changed = asyncio.run(
        runtime._maybe_assess_pending_follow_up_probe(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T10:01:40"),
        )
    )

    assert changed is True
    assert "follow_up_response_aggregation" not in session.metadata
    assert "clickup_prompt" not in session.metadata
    assert "motor spinning" in (session.latest_status or "")


def test_runtime_progress_probe_admin_opt_in_subscribes_and_sends_exchange() -> None:
    runtime = _build_runtime(admins=[AdminProfile(name="George", discord_user_id=999)])
    sent_to_admins: list[tuple[list[str], str]] = []
    persisted: list[str] = []

    async def fake_admin_notice(_client, content: str, *, target_admins=None, files=None, user=None, session=None, view_factory=None):
        del files, user, session, view_factory
        sent_to_admins.append(([admin.name for admin in (target_admins or [])], content))
        return [admin.name for admin in (target_admins or [])]

    async def fake_persist(_user, _session, *, now, previous_session, trigger, details=None):
        del now, previous_session, details
        persisted.append(trigger)
        runtime.state_store.save_session(_session)
        return False

    runtime._send_admin_notice = fake_admin_notice  # type: ignore[method-assign]
    runtime._persist_session_state = fake_persist  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    runtime.roster_by_key[user.user_key] = user
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["clickup_prompt"] = {
        "type": "progress_probe",
        "probe_id": "probe:msg-dry",
        "question_text": "What progress have you made since the last check-in?",
        "original_reply_message_ids": ["msg-dry"],
        "original_reply_text": "still working",
        "probe_round": 1,
        "probe_exchange": [
            {"role": "bot", "content": "What specifically changed since the last check-in?", "message_id": "bot-1"}
        ],
        "subscribed_admin_ids": [],
        "last_activity_at": "2026-05-28T10:01:20",
    }
    runtime.state_store.save_session(session)

    class _FakeResponse:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def is_done(self) -> bool:
            return False

        async def edit_message(self, *, content=None, view=None):
            del view
            self.messages.append(content or "")

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=999),
        created_at=datetime.fromisoformat("2026-05-28T10:02:00"),
        client=SimpleNamespace(),
        response=_FakeResponse(),
        followup=SimpleNamespace(send=lambda *_args, **_kwargs: asyncio.sleep(0)),
    )

    asyncio.run(
        runtime.handle_progress_probe_admin_interaction(
            interaction,
            user.user_key,
            session.session_date,
            "probe:msg-dry",
        )
    )

    prompt = session.metadata["clickup_prompt"]
    assert prompt["subscribed_admin_ids"] == [999]
    assert prompt["last_activity_at"] == "2026-05-28T10:02:00-07:00"
    assert persisted == ["progress_probe_admin_subscription"]
    assert sent_to_admins[0][0] == ["George"]
    assert "Original aggregated reply" in sent_to_admins[0][1]
    assert interaction.response.messages[-1] == "Subscribed. I will forward later probe replies here."


def test_runtime_progress_probe_times_out_after_thirty_minutes() -> None:
    runtime = _build_runtime(admins=[AdminProfile(name="George", discord_user_id=999)])
    admin_notices: list[str] = []
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["clickup_prompt"] = {
        "type": "progress_probe",
        "probe_id": "probe:msg-dry",
        "question_text": "What progress have you made since the last check-in?",
        "original_reply_message_ids": ["msg-dry"],
        "original_reply_text": "still working",
        "probe_round": 1,
        "probe_exchange": [
            {"role": "bot", "content": "What specifically changed since the last check-in?", "message_id": "bot-1"}
        ],
        "subscribed_admin_ids": [999],
        "last_activity_at": "2026-05-28T10:01:20",
    }

    async def fake_admin_notice(_client, content: str, *, target_admins=None, files=None, user=None, session=None, view_factory=None):
        del target_admins, files, user, session, view_factory
        admin_notices.append(content)
        return ["George"]

    runtime._send_admin_notice = fake_admin_notice  # type: ignore[method-assign]

    changed = asyncio.run(
        runtime._maybe_timeout_progress_probe(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T10:32:00"),
        )
    )

    assert changed is True
    assert "clickup_prompt" not in session.metadata
    assert session.metadata["progress_probe_history"][-1]["closure_reason"] == "timed_out"
    assert admin_notices == ["Probe closed: no reply to the progress probe for 30 minutes."]


def test_runtime_clock_in_confirmation_recovers_missing_clock_in_and_starts_task_selection_sequence() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "step": "photo",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
    }
    inbound = MessageRecord(
        message_id="msg-clockin",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:00:00"),
        content="yes ive clocked in a while ago",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._maybe_recover_missing_clock_in(
            SimpleNamespace(),
            user,
            session,
            inbound,
            SimpleNamespace(clocked_in=True),
            datetime.fromisoformat("2026-05-28T14:00:00"),
        )
    )
    assert handled is True
    assert session.clocked_in_at == "2026-05-28T14:00:00"
    assert session.work_segments == [{"clocked_in_at": "2026-05-28T14:00:00", "clocked_out_at": None}]
    assert session.stage == "awaiting_task_selection"
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_onboarding"
    assert prompt["step"] == "select_task"
    assert prompt["recommended_task_id"] == "868jun6qg"
    assert any("highest-priority assigned task" in item for item in sent)


def test_runtime_same_day_reclockin_resumes_active_task_and_opens_new_segment() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="clocked_out",
        clocked_in_at="2026-05-28T09:00:00",
        intake_completed_at="2026-05-28T09:20:00",
        clocked_out_at="2026-05-28T11:00:00",
        work_segments=[{"clocked_in_at": "2026-05-28T09:00:00", "clocked_out_at": "2026-05-28T11:00:00"}],
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    handled = asyncio.run(
        runtime._maybe_resume_same_day_work(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T22:00:00"),
        )
    )
    assert handled is True
    assert session.stage == "active"
    assert session.clocked_out_at is None
    assert session.last_follow_up_at == "2026-05-28T22:00:00"
    assert session.work_segments == [
        {"clocked_in_at": "2026-05-28T09:00:00", "clocked_out_at": "2026-05-28T11:00:00"},
        {"clocked_in_at": "2026-05-28T22:00:00", "clocked_out_at": None},
    ]
    tracking = session.metadata["clickup_time_tracking"]
    assert tracking["task_id"] == "868jun6qg"
    assert tracking["started_at"] == "2026-05-28T22:00:00"
    assert any("clocked back in and resumed" in item.lower() for item in sent)


def test_runtime_same_day_reclockin_without_active_task_starts_light_task_selection() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="clocked_out",
        clocked_in_at="2026-05-28T09:00:00",
        intake_completed_at="2026-05-28T09:20:00",
        clocked_out_at="2026-05-28T11:00:00",
        work_segments=[{"clocked_in_at": "2026-05-28T09:00:00", "clocked_out_at": "2026-05-28T11:00:00"}],
    )
    handled = asyncio.run(
        runtime._maybe_resume_same_day_work(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T22:00:00"),
        )
    )
    assert handled is True
    assert session.stage == "awaiting_task_selection"
    assert session.work_segments[-1] == {"clocked_in_at": "2026-05-28T22:00:00", "clocked_out_at": None}
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_onboarding"
    assert prompt["source"] == "same_day_reclockin"
    assert prompt["step"] == "select_task"
    assert any("clocked back in" in item.lower() for item in sent)


def test_runtime_process_inbound_clock_me_in_pollo_resumes_same_day_session() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="clocked_out",
        clocked_in_at="2026-05-28T09:00:00",
        intake_completed_at="2026-05-28T09:20:00",
        clocked_out_at="2026-05-28T12:30:00",
        work_segments=[{"clocked_in_at": "2026-05-28T09:00:00", "clocked_out_at": "2026-05-28T12:30:00"}],
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    inbound = MessageRecord(
        message_id="msg-clock-me-in-pollo",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T13:30:00"),
        content="Clock me in Pollo",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert session.stage == "active"
    assert session.clocked_out_at is None
    assert session.work_segments[-1] == {"clocked_in_at": "2026-05-28T13:30:00", "clocked_out_at": None}
    assert any("clocked back in and resumed" in item.lower() for item in sent)


def test_runtime_process_inbound_resume_work_resumes_auto_clocked_out_session() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="clocked_out",
        clocked_in_at="2026-05-28T09:00:00",
        intake_completed_at="2026-05-28T09:20:00",
        clocked_out_at="2026-05-28T12:30:00",
        work_segments=[{"clocked_in_at": "2026-05-28T09:00:00", "clocked_out_at": "2026-05-28T12:30:00"}],
        metadata={
            "active_clickup_task_id": "868jun6qg",
            "active_clickup_task_name": "formalize project tree",
            "auto_clock_out_at": "2026-05-28T12:30:00",
        },
    )
    inbound = MessageRecord(
        message_id="msg-resume-work",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T13:30:00"),
        content="resume work",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert session.stage == "active"
    assert session.clocked_out_at is None
    assert session.work_segments[-1] == {"clocked_in_at": "2026-05-28T13:30:00", "clocked_out_at": None}
    assert any("clocked back in and resumed" in item.lower() for item in sent)


def test_finish_task_onboarding_sets_intake_completed_at_when_missing() -> None:
    runtime = _build_runtime()
    activated: list[tuple[str, str]] = []

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> TaskActivationResult:
        activated.append((task_id, task_name))
        return _activation_result(task_id, task_name)

    runtime._activate_clickup_task = fake_activate  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_start_photo")
    prompt = {
        "source": "clock_in_recovery",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
    }
    now = datetime.fromisoformat("2026-05-28T10:30:00")

    asyncio.run(runtime._finish_task_onboarding(user, session, prompt, now))

    assert session.stage == "active"
    assert session.intake_completed_at == "2026-05-28T10:30:00"
    assert session.last_follow_up_at == "2026-05-28T10:30:00"
    assert activated == [("868jun6qg", "formalize project tree")]


def test_normalize_session_state_backfills_intake_completed_at_from_task_onboarding_metadata() -> None:
    runtime = _build_runtime()
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["last_task_onboarding_completed_at"] = "2026-05-28T10:50:35.887697-07:00"

    changed = runtime._normalize_session_state(session)

    assert changed is True
    assert session.intake_completed_at == "2026-05-28T10:50:35.887697-07:00"


def test_scheduler_tick_continues_after_one_user_failure(caplog) -> None:
    runtime = _build_runtime()
    broken = UserProfile(
        user_key="broken",
        display_name="Broken User",
        discord_user_id=1,
        discord_username="broken",
        storage_folder_name="BrokenUser",
    )
    healthy = UserProfile(
        user_key="healthy",
        display_name="Healthy User",
        discord_user_id=2,
        discord_username="healthy",
        storage_folder_name="HealthyUser",
    )
    sessions = {
        broken.user_key: SessionState(user_key="broken", session_date="2026-06-03"),
        healthy.user_key: SessionState(user_key="healthy", session_date="2026-06-03"),
    }
    fixed_now = datetime.fromisoformat("2026-06-03T10:00:00-07:00")
    attempted: list[str] = []
    persisted: list[str] = []
    dashboards: list[str] = []
    resolved_subjects: list[str] = []

    async def fake_clock_in(_client, user, _session, _now):
        attempted.append(user.user_key)
        if user.user_key == "broken":
            raise RuntimeError("boom")
        return True

    async def fake_false(*_args, **_kwargs):
        return False

    async def fake_persist(user, _session, *, now, previous_session, trigger, details):
        del now, previous_session, trigger, details
        persisted.append(user.user_key)
        return True

    async def fake_dashboard():
        dashboards.append("written")

    runtime.roster_by_key = {broken.user_key: broken, healthy.user_key: healthy}
    runtime.get_user_session_for_moment = lambda user, moment=None: (sessions[user.user_key], fixed_now)  # type: ignore[method-assign]
    runtime._maybe_send_clock_in = fake_clock_in  # type: ignore[method-assign]
    runtime._maybe_auto_clock_out_inactive = fake_false  # type: ignore[method-assign]
    runtime._maybe_prompt_task_onboarding = fake_false  # type: ignore[method-assign]
    runtime._maybe_send_lunch_break_check_in = fake_false  # type: ignore[method-assign]
    runtime._maybe_assess_pending_follow_up_probe = fake_false  # type: ignore[method-assign]
    runtime._maybe_timeout_progress_probe = fake_false  # type: ignore[method-assign]
    runtime._maybe_send_follow_up = fake_false  # type: ignore[method-assign]
    runtime._maybe_alert_admin = fake_false  # type: ignore[method-assign]
    runtime._maybe_flush_clickup = fake_false  # type: ignore[method-assign]
    runtime._persist_session_state = fake_persist  # type: ignore[method-assign]
    runtime.write_dashboard = fake_dashboard  # type: ignore[method-assign]
    runtime.state_store.resolve_matching_operational_issues = (  # type: ignore[attr-defined]
        lambda **kwargs: resolved_subjects.append(kwargs["details_match"]["user_key"])
    )

    with caplog.at_level(logging.ERROR):
        asyncio.run(runtime.scheduler_tick(SimpleNamespace()))

    assert attempted == ["broken", "healthy"]
    assert persisted == ["healthy"]
    assert resolved_subjects == ["healthy"]
    assert dashboards == ["written"]
    assert "Scheduler tick failed for user broken (Broken User)" in caplog.text


def test_refresh_configuration_filters_inactive_roster_users() -> None:
    runtime = InternManagementRuntime.__new__(InternManagementRuntime)
    runtime.bootstrap = SimpleNamespace(default_timezone="America/Los_Angeles")
    runtime.config = None
    runtime.roster_by_discord_id = {}
    runtime.roster_by_key = {}
    runtime.clickup = None
    runtime._config_loaded_at = None

    config = AgentConfig(
        timezone="America/Los_Angeles",
        admin_discord_user_id=1,
        roster_file_name="roster.csv",
        dashboard_file_name="dashboard.md",
        schedule=ScheduleConfig(),
        clickup=ClickUpConfig(workspace_id="9011286053"),
        prompts=PromptConfig(
            clock_in="clock in",
            clock_in_reminder="reminder",
            plan_question="plan",
            start_photo_question="photo",
            risk_question="risk",
            follow_up_questions=["follow up"],
            clock_out_prompt="clock out",
        ),
    )

    async def fake_load_agent_config():
        return config

    async def fake_load_roster(_config):
        return [
            UserProfile(
                user_key="andrew",
                display_name="Andrew",
                discord_user_id=1,
                discord_username="andrew",
                storage_folder_name="AndrewOre",
                active=True,
            ),
            UserProfile(
                user_key="ali",
                display_name="Ali",
                discord_user_id=2,
                discord_username="ali",
                storage_folder_name="AliAhmed",
                active=False,
            ),
        ]

    runtime.store = SimpleNamespace(
        load_agent_config=fake_load_agent_config,
        load_roster=fake_load_roster,
    )

    asyncio.run(runtime.refresh_configuration(force=True))

    assert set(runtime.roster_by_key) == {"andrew"}
    assert set(runtime.roster_by_discord_id) == {1}


def test_runtime_pause_current_task_tracking_moves_entry_to_history() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["clickup_time_tracking"] = {
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "started_at": "2026-05-28T09:00:00",
        "source": "local",
    }
    note = asyncio.run(
        runtime._pause_current_task_tracking(
            user,
            session,
            datetime.fromisoformat("2026-05-28T10:30:00"),
            set_hold=False,
            end_reason="admin_switch",
        )
    )
    assert note is not None
    assert "paused local task tracking" in note
    assert "clickup_time_tracking" not in session.metadata
    history = session.metadata["clickup_time_tracking_history"]
    assert isinstance(history, list)
    assert history[0]["task_id"] == "868jun6qg"


def test_runtime_start_lunch_break_pauses_timer_and_moves_stage() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["clickup_time_tracking"] = {
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "started_at": "2026-05-28T09:00:00",
        "source": "local",
    }
    changed = asyncio.run(
        runtime._maybe_start_lunch_break(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T12:00:00"),
        )
    )
    assert changed is True
    assert session.stage == "on_lunch_break"
    assert session.metadata["lunch_windows"] == [
        {
            "started_at": "2026-05-28T12:00:00",
            "ended_at": None,
            "source": "runtime_lunch_break",
        }
    ]
    assert session.metadata["lunch_resume_stage"] == "active"
    assert session.metadata["lunch_resume_task_id"] == "868jun6qg"
    assert "clickup_time_tracking" not in session.metadata
    history = session.metadata["clickup_time_tracking_history"]
    assert history[0]["end_reason"] == "lunch_break"
    assert any("lunch break" in item.lower() for item in sent)


def test_runtime_lunch_break_check_in_uses_follow_up_interval() -> None:
    runtime = _build_runtime()
    runtime.config.schedule.follow_up_interval_minutes = 30
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="on_lunch_break",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["lunch_last_prompt_at"] = "2026-05-28T12:00:00"
    changed = asyncio.run(
        runtime._maybe_send_lunch_break_check_in(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T12:31:00"),
        )
    )
    assert changed is True
    assert sent == [runtime.config.prompts.lunch_break_check_in]
    assert session.metadata["lunch_resume_requested_at"] == "2026-05-28T12:31:00"


def test_runtime_ambiguous_lunch_mention_prompts_for_confirmation() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
    )
    asyncio.run(
        runtime._route_message(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-lunch-maybe",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T12:15:00"),
                content="I will probably finish the wiring after lunch.",
                attachments=[],
            ),
            SimpleNamespace(clocking_out=False, starting_lunch=False, ending_lunch=False),
            datetime.fromisoformat("2026-05-28T12:15:00"),
        )
    )
    assert session.stage == "active"
    assert session.metadata["lunch_confirmation_requested_at"] == "2026-05-28T12:15:00"
    assert any("reply `yes` or `no`" in item.lower() for item in sent)


def test_runtime_ambiguous_lunch_mention_during_task_selection_skips_onboarding_spam() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_persist(*_args, **_kwargs) -> bool:
        return True

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._persist_session_state = fake_persist  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="awaiting_task_selection",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "step": "select_task",
        "source": "missing_active_task",
        "draft": {},
    }
    inbound = MessageRecord(
        message_id="msg-lunch-task-selection",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T12:15:00"),
        content="lunch",
        attachments=[],
    )
    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T12:15:00"),
        )
    )
    assert session.stage == "awaiting_task_selection"
    assert session.metadata["lunch_confirmation_requested_at"] == "2026-05-28T12:15:00"
    assert session.metadata["clickup_prompt"]["type"] == "task_onboarding"
    assert sent == ["You mentioned lunch. Do you want me to start a lunch break right now? Reply `yes` or `no`."]


def test_runtime_explicit_lunch_start_during_task_selection_enters_lunch_state() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_persist(*_args, **_kwargs) -> bool:
        return True

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._persist_session_state = fake_persist  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="awaiting_task_selection",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "step": "select_task",
        "source": "missing_active_task",
        "draft": {},
    }
    inbound = MessageRecord(
        message_id="msg-lunch-start-task-selection",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T12:16:00"),
        content="I am going to lunch now.",
        attachments=[],
    )
    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T12:16:00"),
        )
    )
    assert session.stage == "on_lunch_break"
    assert session.metadata["lunch_resume_stage"] == "awaiting_task_selection"
    assert session.metadata["clickup_prompt"]["type"] == "task_onboarding"
    assert any("marked you on lunch break" in item.lower() for item in sent)


def test_runtime_lunch_confirmation_yes_starts_lunch_break() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["lunch_confirmation_requested_at"] = "2026-05-28T12:15:00"
    asyncio.run(
        runtime._route_message(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-lunch-yes",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T12:16:00"),
                content="yes",
                attachments=[],
            ),
            SimpleNamespace(clocking_out=False, starting_lunch=False, ending_lunch=False),
            datetime.fromisoformat("2026-05-28T12:16:00"),
        )
    )
    assert session.stage == "on_lunch_break"
    assert "lunch_confirmation_requested_at" not in session.metadata
    assert any("marked you on lunch break" in item.lower() for item in sent)


def test_runtime_lunch_confirmation_yes_from_task_selection_preserves_onboarding_prompt() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="awaiting_task_selection",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "step": "select_task",
        "source": "missing_active_task",
        "draft": {},
    }
    session.metadata["lunch_confirmation_requested_at"] = "2026-05-28T12:15:00"
    asyncio.run(
        runtime._route_message(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-lunch-yes-task-selection",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T12:16:00"),
                content="yes",
                attachments=[],
            ),
            SimpleNamespace(clocking_out=False, starting_lunch=False, ending_lunch=False),
            datetime.fromisoformat("2026-05-28T12:16:00"),
        )
    )
    assert session.stage == "on_lunch_break"
    assert session.metadata["lunch_resume_stage"] == "awaiting_task_selection"
    assert session.metadata["clickup_prompt"]["type"] == "task_onboarding"
    assert "lunch_confirmation_requested_at" not in session.metadata


def test_runtime_lunch_confirmation_no_keeps_user_active() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["lunch_confirmation_requested_at"] = "2026-05-28T12:15:00"
    asyncio.run(
        runtime._route_message(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-lunch-no",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T12:16:00"),
                content="no",
                attachments=[],
            ),
            SimpleNamespace(clocking_out=False, starting_lunch=False, ending_lunch=False),
            datetime.fromisoformat("2026-05-28T12:16:00"),
        )
    )
    assert session.stage == "active"
    assert "lunch_confirmation_requested_at" not in session.metadata
    assert any("keep you on your current work" in item.lower() for item in sent)


def test_runtime_lunch_confirmation_no_from_task_selection_keeps_onboarding_prompt() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="awaiting_task_selection",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "step": "select_task",
        "source": "missing_active_task",
        "draft": {},
    }
    session.metadata["lunch_confirmation_requested_at"] = "2026-05-28T12:15:00"
    asyncio.run(
        runtime._route_message(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-lunch-no-task-selection",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T12:16:00"),
                content="no",
                attachments=[],
            ),
            SimpleNamespace(clocking_out=False, starting_lunch=False, ending_lunch=False),
            datetime.fromisoformat("2026-05-28T12:16:00"),
        )
    )
    assert session.stage == "awaiting_task_selection"
    assert session.metadata["clickup_prompt"]["type"] == "task_onboarding"
    assert "lunch_confirmation_requested_at" not in session.metadata


def test_runtime_end_lunch_break_resumes_task_timer() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="on_lunch_break",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["lunch_resume_stage"] = "active"
    session.metadata["lunch_resume_task_id"] = "868jun6qg"
    session.metadata["lunch_resume_task_name"] = "formalize project tree"
    session.metadata["lunch_started_at"] = "2026-05-28T12:00:00"
    session.metadata["lunch_windows"] = [
        {
            "started_at": "2026-05-28T12:00:00",
            "ended_at": None,
            "source": "runtime_lunch_break",
        }
    ]
    asyncio.run(
        runtime._handle_lunch_break_message(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-lunch-done",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T12:30:00"),
                content="im done",
                attachments=[],
            ),
            SimpleNamespace(clocking_out=False, ending_lunch=False),
            datetime.fromisoformat("2026-05-28T12:30:00"),
        )
    )
    assert session.stage == "active"
    tracking = session.metadata["clickup_time_tracking"]
    assert tracking["task_id"] == "868jun6qg"
    assert tracking["started_at"] == "2026-05-28T12:30:00"
    assert session.metadata["lunch_windows"][0]["ended_at"] == "2026-05-28T12:30:00"
    assert any("resumed task tracking" in item.lower() for item in sent)


def test_runtime_end_lunch_break_resumes_task_timer_for_short_back_reply() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="on_lunch_break",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["lunch_resume_stage"] = "active"
    session.metadata["lunch_resume_task_id"] = "868jun6qg"
    session.metadata["lunch_resume_task_name"] = "formalize project tree"
    asyncio.run(
        runtime._handle_lunch_break_message(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-lunch-back",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T12:31:00"),
                content="back",
                attachments=[],
            ),
            SimpleNamespace(clocking_out=False, ending_lunch=False),
            datetime.fromisoformat("2026-05-28T12:31:00"),
        )
    )
    assert session.stage == "active"
    tracking = session.metadata["clickup_time_tracking"]
    assert tracking["task_id"] == "868jun6qg"
    assert tracking["started_at"] == "2026-05-28T12:31:00"
    assert any("welcome back" in item.lower() for item in sent)


def test_runtime_end_lunch_break_resumes_when_explicit_signal_phrase_detected() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="on_lunch_break",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["lunch_resume_stage"] = "active"
    session.metadata["lunch_resume_task_id"] = "868jun6qg"
    session.metadata["lunch_resume_task_name"] = "formalize project tree"
    asyncio.run(
        runtime._handle_lunch_break_message(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-lunch-explicit",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T12:32:00"),
                content="i'm back from lunch",
                attachments=[],
            ),
            SimpleNamespace(clocking_out=False, ending_lunch=True),
            datetime.fromisoformat("2026-05-28T12:32:00"),
        )
    )
    assert session.stage == "active"
    tracking = session.metadata["clickup_time_tracking"]
    assert tracking["task_id"] == "868jun6qg"
    assert tracking["started_at"] == "2026-05-28T12:32:00"
    assert any("welcome back" in item.lower() for item in sent)


def test_runtime_end_lunch_break_uses_reported_return_time() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
        timezone="America/Los_Angeles",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-07-14",
        stage="on_lunch_break",
        clocked_in_at="2026-07-14T09:00:00-07:00",
        metadata={
            "active_clickup_task_id": "868jun6qg",
            "active_clickup_task_name": "formalize project tree",
            "lunch_started_at": "2026-07-14T12:46:54-07:00",
            "lunch_resume_stage": "active",
            "lunch_resume_task_id": "868jun6qg",
            "lunch_resume_task_name": "formalize project tree",
            "lunch_windows": [
                {
                    "started_at": "2026-07-14T12:46:54-07:00",
                    "ended_at": None,
                    "source": "runtime_lunch_break",
                }
            ],
        },
    )
    now = datetime.fromisoformat("2026-07-14T15:23:15-07:00")

    asyncio.run(
        runtime._handle_lunch_break_message(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-reported-lunch-return",
                direction="inbound",
                author_id=1,
                created_at=now,
                content="POLLO I GOT BACK AT 1 20",
                attachments=[],
            ),
            SimpleNamespace(clocking_out=False, clocked_in=False, ending_lunch=False),
            now,
        )
    )

    assert session.stage == "active"
    assert session.metadata["lunch_ended_at"] == "2026-07-14T13:20:00-07:00"
    assert session.metadata["lunch_windows"][0]["ended_at"] == "2026-07-14T13:20:00-07:00"
    assert session.metadata["clickup_time_tracking"]["started_at"] == "2026-07-14T13:20:00-07:00"
    assert session.metadata["last_reported_lunch_return"] == {
        "returned_at": "2026-07-14T13:20:00-07:00",
        "reported_at": "2026-07-14T15:23:15-07:00",
        "source": "intern_message",
    }
    assert any("resumed task tracking" in item.lower() for item in sent)


def test_runtime_lunch_resume_accepts_clock_in_and_natural_back_phrasing() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        timezone="America/Los_Angeles",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-07-24",
        stage="on_lunch_break",
        metadata={"lunch_started_at": "2026-07-24T13:10:00-07:00"},
    )

    assert runtime._looks_like_lunch_resume_reply("clock me back in") is True
    assert runtime._looks_like_lunch_resume_reply("No more lunch break, I am back from it") is True
    assert runtime._looks_like_lunch_resume_reply("I have been done with lunch since 1:30") is True
    assert runtime._reported_lunch_return_at(
        user,
        session,
        "I have been done with lunch since 1:30",
        datetime.fromisoformat("2026-07-24T14:43:11-07:00"),
    ) == datetime.fromisoformat("2026-07-24T13:30:00-07:00")


def test_runtime_reported_lunch_return_rejects_time_before_lunch() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        timezone="America/Los_Angeles",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-07-24",
        stage="on_lunch_break",
        metadata={"lunch_started_at": "2026-07-24T13:10:00-07:00"},
    )

    assert runtime._reported_lunch_return_at(
        user,
        session,
        "I got back at 12:30",
        datetime.fromisoformat("2026-07-24T14:00:00-07:00"),
    ) is None


def test_runtime_end_lunch_break_without_task_falls_back_to_task_selection() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="on_lunch_break",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["lunch_resume_stage"] = "active"
    asyncio.run(
        runtime._end_lunch_break(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T12:30:00"),
        )
    )
    assert session.stage == "awaiting_task_selection"
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_onboarding"
    assert prompt["step"] == "select_task"
    assert any("active clickup task" in item.lower() for item in sent)


def test_runtime_end_lunch_break_resumes_existing_task_onboarding_step() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="on_lunch_break",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "step": "plan",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "source": "missing_active_task",
        "draft": {},
    }
    session.metadata["lunch_resume_stage"] = "awaiting_plan"
    session.metadata["lunch_resume_task_id"] = "868jun6qg"
    session.metadata["lunch_resume_task_name"] = "formalize project tree"
    asyncio.run(
        runtime._end_lunch_break(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T12:30:00"),
        )
    )
    assert session.stage == "awaiting_plan"
    assert session.metadata["clickup_prompt"]["step"] == "plan"
    assert any("let's pick up where we left off" in item.lower() for item in sent)
    assert any("i still need your plan" in item.lower() for item in sent)


def test_runtime_task_onboarding_accepts_recommended_reply_for_daily_clock_in() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    prompt = {
        "type": "task_onboarding",
        "step": "select_task",
        "source": "daily_clock_in",
        "recommended_task_id": "868jun6qg",
        "recommended_task_name": "formalize project tree",
        "draft": {},
    }
    inbound = MessageRecord(
        message_id="msg-recommended",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:01:00"),
        content="recommended",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:01:00"),
            prompt,
        )
    )
    assert handled is True
    assert prompt["candidate_task_id"] == "868jun6qg"
    assert prompt["candidate_task_name"] == "formalize project tree"
    assert prompt["step"] == "confirm_task"
    assert session.stage == "awaiting_task_selection"
    assert any("formalize project tree" in item for item in sent)


def test_runtime_task_onboarding_accepts_numbered_task_choice() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    prompt = {
        "type": "task_onboarding",
        "step": "select_task",
        "source": "daily_clock_in",
        "draft": {},
    }
    inbound = MessageRecord(
        message_id="msg-option-two",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:01:00"),
        content="2",
        attachments=[],
    )

    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(), user, session, inbound, inbound.created_at, prompt
        )
    )

    assert handled is True
    assert prompt["candidate_task_id"] == "868jun6qh"
    assert prompt["candidate_task_name"] == "secondary cleanup"
    assert prompt["step"] == "confirm_task"


def test_runtime_task_onboarding_confirmation_yes_advances_to_plan() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    prompt = {
        "type": "task_onboarding",
        "step": "confirm_task",
        "source": "daily_clock_in",
        "candidate_task_id": "868jun6qg",
        "candidate_task_name": "formalize project tree",
        "draft": {},
    }
    inbound = MessageRecord(
        message_id="msg-confirm",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:01:30"),
        content="yes",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:01:30"),
            prompt,
        )
    )
    assert handled is True
    assert prompt["task_id"] == "868jun6qg"
    assert prompt["task_name"] == "formalize project tree"
    assert prompt["step"] == "plan"
    assert session.stage == "awaiting_plan"
    assert any("What is your plan for `formalize project tree`?" in item for item in sent)


def test_runtime_task_onboarding_confirmation_no_returns_to_task_selection() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    prompt = {
        "type": "task_onboarding",
        "step": "confirm_task",
        "source": "daily_clock_in",
        "candidate_task_id": "868jun6qg",
        "candidate_task_name": "formalize project tree",
        "draft": {},
    }
    inbound = MessageRecord(
        message_id="msg-confirm-no",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:01:30"),
        content="no",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:01:30"),
            prompt,
        )
    )
    assert handled is True
    assert prompt["step"] == "select_task"
    assert "candidate_task_id" not in prompt
    assert session.stage == "awaiting_task_selection"
    assert any("Choose one option by replying with its number" in item for item in sent)


def test_runtime_task_onboarding_confirmation_re_resolves_corrected_task() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    prompt = {
        "type": "task_onboarding",
        "step": "confirm_task",
        "source": "daily_clock_in",
        "candidate_task_id": "868jun6qg",
        "candidate_task_name": "formalize project tree",
        "draft": {},
    }
    inbound = MessageRecord(
        message_id="msg-confirm-other",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:01:30"),
        content="secondary cleanup",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:01:30"),
            prompt,
        )
    )
    assert handled is True
    assert prompt["step"] == "confirm_task"
    assert prompt["candidate_task_id"] == "868jun6qh"
    assert prompt["candidate_task_name"] == "secondary cleanup"
    assert session.stage == "awaiting_task_selection"
    assert any("secondary cleanup" in item.lower() for item in sent)


def test_runtime_task_selection_prompt_renders_hierarchy_tree_and_create_escape_hatch() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )

    async def fake_build_hierarchy(_user, *, tasks=None, limit=25):
        del _user, tasks, limit
        return {
            "tasks_by_id": {
                "parent-1": {"id": "parent-1", "name": "Robot Build"},
                "task-1": {"id": "task-1", "name": "Harness Validation"},
            },
            "assigned_task_ids": ["task-1"],
            "root_ids": ["parent-1"],
            "children_by_parent_id": {"parent-1": ["task-1"]},
        }

    def fake_render_hierarchy(_hierarchy, *, recommended_task_id=None):
        del _hierarchy, recommended_task_id
        return "\\- Robot Build | id=parent-1\n   \\- Harness Validation | id=task-1 [assigned]"

    runtime.clickup.build_assigned_task_hierarchy = fake_build_hierarchy  # type: ignore[method-assign]
    runtime.clickup.render_assigned_task_hierarchy = fake_render_hierarchy  # type: ignore[method-assign]

    prompt = asyncio.run(runtime._task_selection_prompt(user))

    assert "Choose one option by replying with its number, name, or ID" in prompt
    assert "formalize project tree" in prompt
    assert "reply `create task` if none fit" in prompt.lower()
    assert "1. `formalize project tree`" in prompt


def test_runtime_task_selection_prompt_shows_ranked_options_across_clickup_spaces() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28")

    async def fake_list_assigned_tasks(_user, limit=8):
        del _user, limit
        return []

    async def fake_suggest_next_tasks(_user, _session, _messages, *, exclude_task_ids=None, limit=3):
        del _user, _session, _messages, exclude_task_ids, limit
        return [
            {
                "id": "company-overhead",
                "name": "Organize the fabrication shop",
                "priority": {"priority": "high"},
                "space": {"name": "Company Operations"},
                "folder": {"name": "Shop Improvements"},
                "list": {"id": "shop-list", "name": "Overhead"},
                "_don_pollo_workspace_option": True,
            },
            {
                "id": "flight-harness",
                "name": "Validate the PERDEX harness",
                "priority": {"priority": "normal"},
                "space": {"name": "Flight Projects"},
                "list": {"id": "hardware-list", "name": "Hardware"},
                "_don_pollo_workspace_option": True,
            },
        ]

    runtime.clickup = SimpleNamespace(
        list_assigned_tasks=fake_list_assigned_tasks,
        suggest_next_tasks=fake_suggest_next_tasks,
        task_location_label=lambda task: " / ".join(
            str(task[key]["name"])
            for key in ("space", "folder", "list")
            if isinstance(task.get(key), dict) and task[key].get("name")
        ),
    )

    context = asyncio.run(runtime._task_selection_context(user, session))

    assert "Choose one option by replying with its number, name, or ID" in context["message"]
    assert "Company Operations / Shop Improvements / Overhead" in context["message"]
    assert "Flight Projects / Hardware" in context["message"]
    assert "company-overhead" in context["message"]
    assert "[recommended]" in context["message"]
    assert {item["id"] for item in context["candidate_tasks"]} == {
        "company-overhead",
        "flight-harness",
    }


def test_runtime_task_selection_is_numbered_and_limited_to_five() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28")

    async def fake_list_assigned_tasks(_user, limit=8):
        return [
            {
                "id": f"assigned-{index}",
                "name": f"Assigned task {index}",
                "status": {"status": "to do"},
                "priority": {"priority": "normal"},
                "list": {"id": "list-1", "name": "Assigned"},
            }
            for index in range(limit)
        ]

    async def fake_suggest(_user, _session, _messages, *, exclude_task_ids=None, limit=5):
        del exclude_task_ids
        return [
            {
                "id": "workspace-1",
                "name": "Improve shop layout",
                "status": {"status": "to do"},
                "priority": {"priority": "high"},
                "space": {"name": "Operations"},
                "list": {"id": "shop", "name": "Shop"},
                "_don_pollo_workspace_option": True,
            }
        ][:limit]

    runtime.clickup.list_assigned_tasks = fake_list_assigned_tasks  # type: ignore[method-assign]
    runtime.clickup.suggest_next_tasks = fake_suggest  # type: ignore[method-assign]

    context = asyncio.run(runtime._task_selection_context(user, session))

    assert len(context["candidate_tasks"]) == 5
    assert [line.split(".", 1)[0] for line in context["message"].splitlines() if line[:1].isdigit()] == [
        "1",
        "2",
        "3",
        "4",
        "5",
    ]
    assert context["candidate_tasks"][-1]["id"] == "workspace-1"


def test_runtime_confirmed_workspace_option_is_assigned_before_onboarding() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    assigned: list[str] = []
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="awaiting_task_selection",
    )
    prompt = {
        "type": "task_onboarding",
        "source": "daily_clock_in",
        "step": "select_task",
        "draft": {},
    }
    option = {
        "id": "company-overhead",
        "name": "Organize the fabrication shop",
        "assignees": [],
        "space": {"name": "Company Operations"},
        "list": {"id": "shop-list", "name": "Overhead"},
        "_don_pollo_workspace_option": True,
    }

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del _client, _user, _session, _now, view
        sent.append(content)

    async def fake_resolve_assigned(_user, _hint, *, include_mission_board=False):
        del _user, _hint, include_mission_board
        return None

    async def fake_suggest(_user, _session, _messages, *, exclude_task_ids=None, limit=8):
        del _user, _session, _messages, exclude_task_ids, limit
        return [option]

    async def fake_get_task(task_id: str):
        assert task_id == "company-overhead"
        return dict(option)

    async def fake_assign(task, _user):
        assigned.append(str(task["id"]))
        return True

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime.clickup = SimpleNamespace(
        resolve_task_for_user=fake_resolve_assigned,
        suggest_next_tasks=fake_suggest,
        match_task_hint=lambda tasks, hint: tasks[0] if "shop" in hint.lower() else None,
        get_task=fake_get_task,
        ensure_task_assigned_to_user=fake_assign,
    )

    selected = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            object(),
            user,
            session,
            MessageRecord(
                message_id="choose",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T09:00:00"),
                content="shop cleanup",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T09:00:00"),
            prompt,
        )
    )
    assert selected is True
    assert prompt["candidate_workspace_option"] is True
    assert assigned == []

    confirmed = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            object(),
            user,
            session,
            MessageRecord(
                message_id="confirm",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T09:01:00"),
                content="yes",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T09:01:00"),
            prompt,
        )
    )

    assert confirmed is True
    assert assigned == ["company-overhead"]
    assert prompt["task_id"] == "company-overhead"
    assert prompt["step"] == "plan"
    assert any("Organize the fabrication shop" in message for message in sent)


def test_runtime_task_onboarding_create_task_request_starts_creation_flow() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    prompt = {
        "type": "task_onboarding",
        "step": "select_task",
        "source": "daily_clock_in",
        "reason": "Pick a task.",
        "draft": {},
    }
    session.metadata["clickup_prompt"] = prompt
    inbound = MessageRecord(
        message_id="msg-create-task",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:01:45"),
        content="create task",
        attachments=[],
    )

    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            inbound.created_at,
            prompt,
        )
    )

    assert handled is True
    new_prompt = session.metadata["clickup_prompt"]
    assert new_prompt["type"] == "task_creation"
    assert new_prompt["step"] == "placement"
    assert new_prompt["source"] == "daily_clock_in"
    assert isinstance(new_prompt["placement_candidates"], list)
    assert session.stage == "awaiting_task_selection"
    assert any("let's create a new task" in item.lower() for item in sent)


def test_runtime_task_creation_under_parent_waits_for_approval_then_creates() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    admin_messages: list[str] = []
    create_calls: list[dict[str, object]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_admin_notice(_client, content: str, **_kwargs):
        admin_messages.append(content)
        return ["George"]

    async def fake_create_task(
        list_id: str,
        *,
        name: str,
        description: str,
        assignee_ids=None,
        priority=None,
        due_date=None,
        status=None,
        tags=None,
        parent_task_id=None,
    ):
        create_calls.append(
            {
                "list_id": list_id,
                "name": name,
                "description": description,
                "assignee_ids": assignee_ids,
                "priority": priority,
                "due_date": due_date,
                "status": status,
                "tags": tags,
                "parent_task_id": parent_task_id,
            }
        )
        return {"id": "created-subtask-1", "name": name}

    async def fake_resolve_clickup_user_id(_user):
        return "87438366"

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._send_admin_notice = fake_admin_notice  # type: ignore[method-assign]
    runtime.clickup.create_task = fake_create_task  # type: ignore[method-assign]
    runtime.clickup.resolve_clickup_user_id = fake_resolve_clickup_user_id  # type: ignore[method-assign]

    async def fake_refresh(*_args, **_kwargs):
        return None

    async def fake_persist(*_args, **_kwargs):
        return None

    async def fake_write_dashboard():
        return None

    runtime.refresh_configuration = fake_refresh  # type: ignore[method-assign]
    runtime._persist_session_state = fake_persist  # type: ignore[method-assign]
    runtime.write_dashboard = fake_write_dashboard  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    prompt = {
        "type": "task_creation",
        "source": "daily_clock_in",
        "step": "placement",
        "tree_text": "\\- Robot Build | id=parent-1",
        "placement_candidates": [
            {"id": "parent-1", "name": "Robot Build", "list_id": "list-42", "parent_task_id": ""},
        ],
        "draft": {},
    }
    session.metadata["clickup_prompt"] = prompt
    base_time = datetime.fromisoformat("2026-05-28T14:05:00")

    async def run_steps() -> None:
        await runtime._handle_task_creation_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord("msg-parent", "inbound", 1, base_time, "parent-1", []),
            base_time,
            prompt,
        )
        await runtime._handle_task_creation_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord("msg-title", "inbound", 1, base_time, "Motor Bracket Drill Template", []),
            base_time,
            prompt,
        )
        await runtime._handle_task_creation_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                "msg-desc",
                "inbound",
                1,
                base_time,
                "Need a template so we can drill the bracket consistently.",
                [],
            ),
            base_time,
            prompt,
        )

    asyncio.run(run_steps())

    assert create_calls == []
    proposal = session.metadata["pending_admin_task_proposal"]
    assert proposal["category"] == "project"
    assert proposal["draft"]["parent_task_id"] == "parent-1"
    assert session.metadata["clickup_prompt"]["type"] == "task_creation_pending_approval"
    assert any("proposed a new project task" in item.lower() for item in admin_messages)
    assert any("Placement: under `Robot Build`" in item for item in admin_messages)

    result = asyncio.run(
        runtime.resolve_admin_task_proposal(
            SimpleNamespace(),
            user,
            session,
            approve_create=True,
            admin_message="Approved.",
            now=base_time,
        )
    )

    assert result == "Created `Motor Bracket Drill Template` for Andrew."
    assert create_calls == [
        {
            "list_id": "list-42",
            "name": "Motor Bracket Drill Template",
            "description": "Need a template so we can drill the bracket consistently.",
            "assignee_ids": ["87438366"],
            "priority": None,
            "due_date": None,
            "status": None,
            "tags": None,
            "parent_task_id": "parent-1",
        }
    ]
    new_prompt = session.metadata["clickup_prompt"]
    assert new_prompt["type"] == "task_onboarding"
    assert new_prompt["step"] == "plan"
    assert new_prompt["task_id"] == "created-subtask-1"
    assert new_prompt["task_name"] == "Motor Bracket Drill Template"
    assert session.stage == "awaiting_plan"
    assert any("What is your plan for `Motor Bracket Drill Template`?" in item for item in sent)


def test_task_proposals_classify_shop_work_as_overhead_and_target_named_approvers() -> None:
    runtime = _build_runtime(
        admins=[
            AdminProfile(name="Erik Franks", discord_user_id=1),
            AdminProfile(name="George", discord_user_id=2),
            AdminProfile(name="Operations", discord_user_id=3),
        ]
    )

    category = runtime._task_proposal_category(
        {
            "title": "Improve shop organization",
            "description": "Clean tooling stations and label shared storage.",
        }
    )

    assert category == "overhead"
    assert [admin.name for admin in runtime._task_approval_admins()] == [
        "Erik Franks",
        "George",
    ]


def test_runtime_task_creation_top_level_uses_mission_board_and_starts_plan() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    create_calls: list[dict[str, object]] = []
    runtime.config.clickup.mission_board_list_id = "mission-board"
    runtime.config.clickup.new_task_approval_required = False

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_admin_notice(_client, _content: str, **_kwargs):
        return ["George"]

    async def fake_create_task(list_id: str, **kwargs):
        create_calls.append({"list_id": list_id, **kwargs})
        return {"id": "created-top-level-1", "name": kwargs["name"]}

    async def fake_resolve_clickup_user_id(_user):
        return "87438366"

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._send_admin_notice = fake_admin_notice  # type: ignore[method-assign]
    runtime.clickup.create_task = fake_create_task  # type: ignore[method-assign]
    runtime.clickup.resolve_clickup_user_id = fake_resolve_clickup_user_id  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    prompt = {
        "type": "task_creation",
        "source": "daily_clock_in",
        "step": "placement",
        "tree_text": "",
        "placement_candidates": [],
        "draft": {},
    }
    session.metadata["clickup_prompt"] = prompt
    base_time = datetime.fromisoformat("2026-05-28T14:10:00")

    async def run_steps() -> None:
        await runtime._handle_task_creation_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord("msg-top", "inbound", 1, base_time, "top level", []),
            base_time,
            prompt,
        )
        await runtime._handle_task_creation_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord("msg-title", "inbound", 1, base_time, "Investigate Loose Battery Mount", []),
            base_time,
            prompt,
        )
        await runtime._handle_task_creation_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                "msg-desc",
                "inbound",
                1,
                base_time,
                "Need to inspect the mount and document what hardware is missing.",
                [],
            ),
            base_time,
            prompt,
        )

    asyncio.run(run_steps())

    assert create_calls[0]["list_id"] == "mission-board"
    assert create_calls[0]["parent_task_id"] is None
    assert session.metadata["clickup_prompt"]["step"] == "plan"
    assert session.metadata["clickup_prompt"]["task_id"] == "created-top-level-1"
    assert session.stage == "awaiting_plan"
    assert any("at the top level in Mission Board" in item for item in sent)


def test_runtime_task_creation_top_level_without_mission_board_prompts_again() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    prompt = {
        "type": "task_creation",
        "source": "daily_clock_in",
        "step": "placement",
        "tree_text": "",
        "placement_candidates": [],
        "draft": {},
    }

    handled = asyncio.run(
        runtime._handle_task_creation_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord("msg-top", "inbound", 1, datetime.fromisoformat("2026-05-28T14:12:00"), "top level", []),
            datetime.fromisoformat("2026-05-28T14:12:00"),
            prompt,
        )
    )

    assert handled is True
    assert prompt["step"] == "placement"
    assert any("Mission Board is not configured" in item for item in sent)


def test_runtime_task_creation_go_back_from_placement_returns_to_task_selection() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    session.metadata["pending_intern_task_switch"] = {
        "source": "intern_switch",
        "previous_stage": "active",
        "previous_task_id": "868jun6qg",
        "previous_task_name": "formalize project tree",
        "previous_selection_reason": "Confirmed by intern during task onboarding.",
    }
    prompt = {
        "type": "task_creation",
        "source": "intern_switch",
        "step": "placement",
        "tree_text": "\\- Robot Build | id=parent-1",
        "placement_candidates": [
            {"id": "parent-1", "name": "Robot Build", "list_id": "list-42", "parent_task_id": ""},
        ],
        "draft": {},
    }
    session.metadata["clickup_prompt"] = prompt

    handled = asyncio.run(
        runtime._handle_task_creation_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord("msg-go-back", "inbound", 1, datetime.fromisoformat("2026-05-28T14:12:30"), "go back", []),
            datetime.fromisoformat("2026-05-28T14:12:30"),
            prompt,
        )
    )

    assert handled is True
    new_prompt = session.metadata["clickup_prompt"]
    assert new_prompt["type"] == "task_onboarding"
    assert new_prompt["step"] == "select_task"
    assert new_prompt["source"] == "intern_switch"
    assert session.stage == "awaiting_task_selection"
    assert session.metadata["pending_intern_task_switch"]["previous_task_id"] == "868jun6qg"
    assert any("go back to your task tree" in item.lower() for item in sent)
    assert any("Choose one option by replying with its number" in item for item in sent)


def test_runtime_task_creation_go_back_from_title_returns_to_placement() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    prompt = {
        "type": "task_creation",
        "source": "daily_clock_in",
        "step": "title",
        "tree_text": "\\- Robot Build | id=parent-1",
        "placement_candidates": [
            {"id": "parent-1", "name": "Robot Build", "list_id": "list-42", "parent_task_id": ""},
        ],
        "draft": {
            "parent_task_id": "parent-1",
            "parent_task_name": "Robot Build",
            "list_id": "list-42",
            "title": "Bad title",
        },
    }

    handled = asyncio.run(
        runtime._handle_task_creation_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord("msg-go-back-title", "inbound", 1, datetime.fromisoformat("2026-05-28T14:13:00"), "go back", []),
            datetime.fromisoformat("2026-05-28T14:13:00"),
            prompt,
        )
    )

    assert handled is True
    assert prompt["step"] == "placement"
    assert "title" not in prompt["draft"]
    assert sent[-1].startswith("Okay, let's create a new task.")


def test_runtime_task_creation_go_back_from_description_returns_to_title() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    prompt = {
        "type": "task_creation",
        "source": "daily_clock_in",
        "step": "description",
        "tree_text": "\\- Robot Build | id=parent-1",
        "placement_candidates": [
            {"id": "parent-1", "name": "Robot Build", "list_id": "list-42", "parent_task_id": ""},
        ],
        "draft": {
            "parent_task_id": "parent-1",
            "parent_task_name": "Robot Build",
            "list_id": "list-42",
            "title": "Motor Bracket Drill Template",
        },
    }

    handled = asyncio.run(
        runtime._handle_task_creation_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord("msg-go-back-description", "inbound", 1, datetime.fromisoformat("2026-05-28T14:13:30"), "go back", []),
            datetime.fromisoformat("2026-05-28T14:13:30"),
            prompt,
        )
    )

    assert handled is True
    assert prompt["step"] == "title"
    assert "title" not in prompt["draft"]
    assert sent[-1] == "Okay, let's go back. What should the new task be called?"


def test_runtime_task_onboarding_collects_interactive_plan_before_photo() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    activated: list[tuple[str, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> TaskActivationResult:
        activated.append((task_id, task_name))
        return _activation_result(task_id, task_name)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._activate_clickup_task = fake_activate  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_plan")
    prompt = {
        "type": "task_onboarding",
        "step": "plan",
        "source": "daily_clock_in",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "draft": {},
    }
    session.metadata["clickup_prompt"] = prompt
    base_time = datetime.fromisoformat("2026-05-28T14:02:00")

    def inbound(message_id: str, content: str = "", attachments: list[AttachmentRecord] | None = None) -> MessageRecord:
        return MessageRecord(
            message_id=message_id,
            direction="inbound",
            author_id=1,
            created_at=base_time,
            content=content,
            attachments=attachments or [],
        )

    responses = [
        ("msg-plan", "I will reorganize the project tree and move the onboarding files into their final folders.", "tangible_result"),
        ("msg-result", "A cleaned up repo tree with the onboarding docs in the right place and no duplicate folders.", "necessity"),
        ("msg-necessity", "We need a stable structure before more docs and scripts get added on top of the current mess.", "effectiveness"),
        ("msg-effectiveness", "This is the fastest path because it removes confusion first and gives me a concrete structure to validate.", "estimated_duration"),
        ("msg-duration", "About 2 hours.", "reconsider_threshold"),
        ("msg-threshold", "If I am still reshuffling folders after 45 minutes without a cleaner structure, I should stop and rethink it.", "fallback_plan"),
    ]

    for message_id, content, expected_step in responses:
        handled = asyncio.run(
            runtime._handle_task_onboarding_prompt(
                SimpleNamespace(),
                user,
                session,
                inbound(message_id, content),
                base_time,
                prompt,
            )
        )
        assert handled is True
        assert prompt["step"] == expected_step
        assert session.stage == "awaiting_plan"

    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound(
                "msg-fallback",
                "I would pause, ask for a quick review of the folder choices, and switch to a smaller cleanup slice if needed.",
            ),
            base_time,
            prompt,
        )
    )
    assert handled is True
    assert prompt["step"] == "photo"
    assert session.stage == "awaiting_start_photo"
    assert session.awaiting_start_photo is True
    assert session.latest_blocker is None
    assert session.latest_feedback == "Use the tangible result to keep the scope tight."
    assert "Tangible result:" in (session.latest_plan or "")
    assert "Expected duration: About 2 hours." in (session.latest_plan or "")
    assert sent[-1].startswith("Use the tangible result to keep the scope tight.")

    photo = AttachmentRecord(
        filename="before.jpg",
        original_filename="before.jpg",
        url="demo://before",
        content_type="image/jpeg",
        size=123,
        local_path="C:/tmp/before.jpg",
    )
    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound("msg-photo", attachments=[photo]),
            base_time,
            prompt,
        )
    )
    assert handled is True
    assert session.stage == "active"
    assert "clickup_prompt" not in session.metadata
    assert session.metadata["task_onboarding_plan"].startswith("I will reorganize the project tree")
    assert session.metadata["task_onboarding_tangible_result"].startswith("A cleaned up repo tree")
    assert session.metadata["task_onboarding_estimated_duration"] == "About 2 hours."
    assert session.metadata["task_onboarding_reconsider_threshold"].startswith("If I am still reshuffling folders")
    assert session.metadata["task_onboarding_fallback_plan"].startswith("I would pause, ask for a quick review")
    assert "Alternative if it is not working:" in session.metadata["last_task_onboarding_summary"]
    assert activated == [("868jun6qg", "formalize project tree")]
    assert sent[-1] == "Perfect. `formalize project tree` is active in ClickUp and task tracking is running."


def test_runtime_task_onboarding_rejects_vague_repeated_plan_before_advancing() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_plan")
    prompt = {
        "type": "task_onboarding",
        "step": "plan",
        "source": "daily_clock_in",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "draft": {},
    }
    inbound = MessageRecord(
        message_id="msg-vague-plan",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:02:00"),
        content="make progress",
        attachments=[],
    )

    for _ in range(2):
        handled = asyncio.run(
            runtime._handle_task_onboarding_prompt(
                SimpleNamespace(), user, session, inbound, inbound.created_at, prompt
            )
        )
        assert handled is True
        assert prompt["step"] == "plan"

    assert "finish line" in sent[0]
    assert "same pattern again" in sent[1]
    assert prompt["draft"]["weak_plan_attempts"] == 2


def test_runtime_task_onboarding_midstream_switch_task_restarts_on_corrected_task() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_plan")
    session.latest_plan = "Old plan"
    session.latest_feedback = "Old feedback"
    prompt = {
        "type": "task_onboarding",
        "step": "tangible_result",
        "source": "daily_clock_in",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "draft": {"plan": "Old draft plan"},
    }
    inbound = MessageRecord(
        message_id="msg-switch-task",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:02:30"),
        content="Actually switch task to secondary cleanup",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:02:30"),
            prompt,
        )
    )
    assert handled is True
    assert prompt["step"] == "plan"
    assert prompt["task_id"] == "868jun6qh"
    assert prompt["task_name"] == "secondary cleanup"
    assert prompt["draft"] == {}
    assert session.stage == "awaiting_plan"
    assert session.latest_plan is None
    assert session.latest_feedback is None
    assert any("restarting onboarding on `secondary cleanup`" in item.lower() for item in sent)


def test_runtime_task_onboarding_wrong_task_without_hint_returns_to_selection() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_plan")
    prompt = {
        "type": "task_onboarding",
        "step": "plan",
        "source": "daily_clock_in",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "draft": {"plan": "Old draft plan"},
    }
    inbound = MessageRecord(
        message_id="msg-wrong-task",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:02:30"),
        content="wrong task",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:02:30"),
            prompt,
        )
    )
    assert handled is True
    assert prompt["step"] == "select_task"
    assert "task_id" not in prompt
    assert prompt["draft"] == {}
    assert session.stage == "awaiting_task_selection"
    assert any("Choose one option by replying with its number" in item for item in sent)


def test_runtime_task_onboarding_fallback_plan_mentions_switch_without_triggering_correction() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_feedback(_user, _prompt, _text: str) -> str:
        return "Keep the scope narrow."

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._task_onboarding_feedback = fake_feedback  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_plan")
    prompt = {
        "type": "task_onboarding",
        "step": "fallback_plan",
        "source": "daily_clock_in",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "draft": {
            "plan": "Plan",
            "tangible_result": "Result",
            "necessity": "Need",
            "effectiveness": "Effective",
            "estimated_duration": "2 hours",
            "reconsider_threshold": "45 minutes",
        },
    }
    inbound = MessageRecord(
        message_id="msg-fallback-switch-later",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:02:30"),
        content="If this stalls, I would ask for help and switch to a smaller cleanup slice later.",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:02:30"),
            prompt,
        )
    )
    assert handled is True
    assert prompt["step"] == "photo"
    assert prompt["task_id"] == "868jun6qg"
    assert session.stage == "awaiting_start_photo"


def test_runtime_task_onboarding_photo_step_can_switch_to_corrected_task() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_start_photo")
    session.awaiting_start_photo = True
    prompt = {
        "type": "task_onboarding",
        "step": "photo",
        "source": "daily_clock_in",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "draft": {
            "plan": "Plan",
        },
    }
    inbound = MessageRecord(
        message_id="msg-photo-switch",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:05:00"),
        content="use secondary cleanup instead",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:05:00"),
            prompt,
        )
    )
    assert handled is True
    assert prompt["step"] == "plan"
    assert prompt["task_id"] == "868jun6qh"
    assert prompt["task_name"] == "secondary cleanup"
    assert session.stage == "awaiting_plan"
    assert session.awaiting_start_photo is False


def test_runtime_task_onboarding_photo_step_switch_request_returns_to_selection() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_start_photo")
    session.awaiting_start_photo = True
    session.latest_plan = "Old plan"
    session.latest_feedback = "Old feedback"
    prompt = {
        "type": "task_onboarding",
        "step": "photo",
        "source": "daily_clock_in",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "draft": {"plan": "Plan"},
    }
    session.metadata["clickup_prompt"] = prompt

    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                "msg-photo-switch-bare",
                "inbound",
                1,
                datetime.fromisoformat("2026-05-28T14:05:30"),
                "I need to switch my task",
                [],
            ),
            datetime.fromisoformat("2026-05-28T14:05:30"),
            prompt,
        )
    )

    assert handled is True
    assert prompt["step"] == "select_task"
    assert "task_id" not in prompt
    assert session.stage == "awaiting_task_selection"
    assert session.awaiting_start_photo is False
    assert session.latest_plan is None
    assert session.latest_feedback is None
    assert any("switch tasks before this one officially starts" in item.lower() for item in sent)
    assert any("Choose one option by replying with its number" in item for item in sent)


def test_runtime_task_onboarding_photo_step_url_requires_attachment() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_start_photo")
    session.awaiting_start_photo = True
    prompt = {
        "type": "task_onboarding",
        "step": "photo",
        "source": "daily_clock_in",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "draft": {},
    }
    session.metadata["clickup_prompt"] = prompt

    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                "msg-photo-url",
                "inbound",
                1,
                datetime.fromisoformat("2026-05-28T14:06:00"),
                "https://cdn.discordapp.com/example.png",
                [],
            ),
            datetime.fromisoformat("2026-05-28T14:06:00"),
            prompt,
        )
    )

    assert handled is True
    assert session.stage == "awaiting_start_photo"
    assert session.awaiting_start_photo is True
    assert sent == [
        "I need the image uploaded as an attachment here. A link by itself does not count as the task-start photo."
    ]


def test_runtime_task_onboarding_deleted_task_404_returns_to_selection() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_finish(_user, _session, _prompt: dict[str, object], _now: datetime) -> TaskActivationResult:
        raise requests.HTTPError(
            "404 Client Error: Not Found",
            response=SimpleNamespace(status_code=404),
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._finish_task_onboarding = fake_finish  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_start_photo")
    session.awaiting_start_photo = True
    session.latest_plan = "Plan"
    session.latest_feedback = "Feedback"
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "step": "photo",
        "source": "daily_clock_in",
        "task_id": "868jzp5f5",
        "task_name": "go back",
        "draft": {"plan": "Plan"},
    }
    session.metadata["active_clickup_task_id"] = "868jzp5f5"
    session.metadata["active_clickup_task_name"] = "go back"
    session.metadata["progress_photo_paths"] = ["C:/tmp/previous.jpg"]
    prompt = session.metadata["clickup_prompt"]
    photo = AttachmentRecord(
        filename="before.jpg",
        original_filename="before.jpg",
        url="demo://before",
        content_type="image/jpeg",
        size=123,
        local_path="C:/tmp/before.jpg",
    )

    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                "msg-photo-404",
                "inbound",
                1,
                datetime.fromisoformat("2026-05-28T14:06:30"),
                "",
                [photo],
            ),
            datetime.fromisoformat("2026-05-28T14:06:30"),
            prompt,
        )
    )

    assert handled is True
    new_prompt = session.metadata["clickup_prompt"]
    assert new_prompt["type"] == "task_onboarding"
    assert new_prompt["step"] == "select_task"
    assert session.stage == "awaiting_task_selection"
    assert session.awaiting_start_photo is False
    assert session.latest_plan is None
    assert session.latest_feedback is None
    assert "active_clickup_task_id" not in session.metadata
    assert "active_clickup_task_name" not in session.metadata
    assert session.metadata["progress_photo_paths"] == ["C:/tmp/previous.jpg"]
    assert any("That ClickUp task no longer exists" in item for item in sent)
    assert any("Choose one option by replying with its number" in item for item in sent)


def test_runtime_task_onboarding_completion_message_does_not_claim_clickup_status_without_confirmation() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_finish(_user, _session, prompt: dict[str, object], _now: datetime) -> TaskActivationResult:
        return TaskActivationResult(
            task_id=str(prompt.get("task_id") or ""),
            task_name=str(prompt.get("task_name") or ""),
            clickup_status_name=None,
            tracking_state={
                "timer_running": True,
                "timer_task_id": str(prompt.get("task_id") or ""),
                "timer_task_name": str(prompt.get("task_name") or ""),
                "timer_note": None,
            },
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._finish_task_onboarding = fake_finish  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_start_photo")
    prompt = {
        "type": "task_onboarding",
        "step": "photo",
        "source": "daily_clock_in",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "draft": {},
    }
    session.metadata["clickup_prompt"] = prompt
    photo = AttachmentRecord(
        filename="before.jpg",
        original_filename="before.jpg",
        url="demo://before",
        content_type="image/jpeg",
        size=123,
        local_path="C:/tmp/before.jpg",
    )
    inbound = MessageRecord(
        message_id="msg-photo",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:02:00"),
        content="",
        attachments=[photo],
    )
    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:02:00"),
            prompt,
        )
    )
    assert handled is True
    assert sent[-1] == (
        "Perfect. I started task tracking for `formalize project tree`, but I could not confirm that ClickUp moved it to `in progress`."
    )


def test_runtime_task_onboarding_reminder_covers_new_steps() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_plan")
    tracking = {"active_task_name": None, "timer_running": False}

    duration_reminder = asyncio.run(
        runtime._task_onboarding_reminder(
            user,
            session,
            tracking,
            {"type": "task_onboarding", "step": "estimated_duration", "task_name": "formalize project tree"},
        )
    )
    fallback_reminder = asyncio.run(
        runtime._task_onboarding_reminder(
            user,
            session,
            tracking,
            {"type": "task_onboarding", "step": "fallback_plan", "task_name": "formalize project tree"},
        )
    )

    assert "rough time estimate" in duration_reminder.lower()
    assert "alternative plan" in fallback_reminder.lower()


def test_runtime_initiate_admin_task_switch_starts_interactive_onboarding() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    persisted: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_resolve_task_for_user(_user, _task_hint: str, include_mission_board=False, include_workspace=False):
        return {
            "id": "868jurjm4",
            "name": "see if task priority works",
            "status": {"status": "to do"},
        }

    async def fake_ensure_task_assigned_to_user(_task, _user) -> bool:
        return True

    async def fake_persist(_user, _session, *, now, previous_session, trigger, details):
        persisted.append(trigger)
        return True

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._persist_session_state = fake_persist  # type: ignore[method-assign]
    runtime.clickup = SimpleNamespace(
        resolve_task_for_user=fake_resolve_task_for_user,
        ensure_task_assigned_to_user=fake_ensure_task_assigned_to_user,
    )
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")

    result = asyncio.run(
        runtime.initiate_admin_task_switch(
            SimpleNamespace(),
            user,
            session,
            "see if task priority works",
            now=datetime.fromisoformat("2026-05-28T16:00:00"),
        )
    )

    assert "Started a task-switch onboarding flow" in result
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_onboarding"
    assert prompt["source"] == "admin_switch"
    assert prompt["step"] == "plan"
    assert persisted == ["admin_task_switch"]
    assert any("Admin wants you to switch" in item for item in sent)
    assert any("What is your plan for `see if task priority works`?" in item for item in sent)


def test_runtime_debug_reset_workday_resets_local_state_and_marks_cutoff() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    persisted: list[tuple[str, str]] = []

    async def fake_persist(user, session, *, now, previous_session, trigger, details):
        persisted.append((user.display_name, trigger))
        return True

    async def fake_dashboard() -> None:
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._persist_session_state = fake_persist  # type: ignore[method-assign]
    runtime.write_dashboard = fake_dashboard  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        first_sign_of_life_at="2026-05-28T09:00:00",
        clocked_in_at="2026-05-28T09:00:00",
        intake_completed_at="2026-05-28T09:20:00",
        last_follow_up_at="2026-05-28T10:00:00",
        latest_plan="build the thing",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    result = asyncio.run(
        runtime.debug_reset_workday(
            SimpleNamespace(),
            user,
            session,
            now=datetime.fromisoformat("2026-05-28T11:00:00"),
            notify_user=True,
        )
    )
    assert session.stage == "awaiting_clock_in"
    assert session.clocked_in_at is None
    assert session.latest_plan is None
    assert session.metadata["debug_reset_reason"] == "admin_debug_resetworkday"
    assert "debug_reset_at" in session.metadata
    assert persisted == [("Andrew", "debug_reset_workday")]
    assert any("reset today's workday flow" in item for item in sent)
    assert "ignore anything from before the reset" in result


def test_runtime_initiates_admin_review_when_task_finishes() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    admin_notices: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_admin_notice(_client, user, _session, _now: datetime, *, review=None) -> None:
        assert isinstance(review, dict)
        admin_notices.append(user.display_name)

    async def fake_pause(_user, _session, _now, *, set_hold: bool, end_reason: str):
        return "paused"

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._send_admin_review_request = fake_admin_notice  # type: ignore[method-assign]
    runtime._pause_current_task_tracking = fake_pause  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    image_path = Path("tests") / "completion-review.jpg"
    image_path.write_bytes(b"fake image bytes")
    inbound = MessageRecord(
        message_id="msg-1",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:00:00"),
        content="I finished formalize project tree.",
        attachments=[
            AttachmentRecord(
                filename="completion-review.jpg",
                url="",
                content_type="image/jpeg",
                size=16,
                local_path=str(image_path),
            )
        ],
    )
    asyncio.run(
        runtime._initiate_admin_review(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:00:00"),
        )
    )
    assert session.stage == "awaiting_admin_review"
    reviews = session.metadata["pending_admin_reviews"]
    assert len(reviews) == 1
    review = reviews[0]
    assert review["task_id"] == "868jun6qg"
    assert review["completion_photo_paths"] == [str(image_path)]
    assert "pending_admin_review" not in session.metadata
    assert admin_notices == ["Andrew"]
    assert any("waiting on admin review" in item for item in sent)
    image_path.unlink(missing_ok=True)


def test_runtime_completion_without_photo_requests_finish_confirmation() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    inbound = MessageRecord(
        message_id="msg-1",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:00:00"),
        content="I finished formalize project tree.",
        attachments=[],
    )
    asyncio.run(
        runtime._apply_post_route_clickup_automation(
            SimpleNamespace(),
            user,
            session,
            inbound,
            SimpleNamespace(recovered=False, stuck=False),
            datetime.fromisoformat("2026-05-28T14:00:00"),
        )
    )
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_finish_confirmation"
    assert prompt["summary_candidate"] == "I finished formalize project tree."
    assert "pending_admin_review" not in session.metadata
    assert any("ready for admin review" in item for item in sent)


def test_runtime_generic_done_requests_summary_and_photo() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    inbound = MessageRecord(
        message_id="msg-1",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:00:00"),
        content="im done",
        attachments=[],
    )
    asyncio.run(
        runtime._apply_post_route_clickup_automation(
            SimpleNamespace(),
            user,
            session,
            inbound,
            SimpleNamespace(recovered=False, stuck=False),
            datetime.fromisoformat("2026-05-28T14:00:00"),
        )
    )
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_finish_confirmation"
    assert prompt["summary_candidate"] == "im done"
    assert any("ready for admin review" in item for item in sent)


def test_runtime_finish_confirmation_yes_starts_review_submission() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["clickup_prompt"] = {
        "type": "task_finish_confirmation",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "summary_candidate": "im done",
        "requested_at": "2026-05-28T14:00:00",
    }
    inbound = MessageRecord(
        message_id="msg-yes",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:01:00"),
        content="yes",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_clickup_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            SimpleNamespace(recovered=False, stuck=False),
            datetime.fromisoformat("2026-05-28T14:01:00"),
        )
    )
    assert handled is True
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_review_submission"
    assert prompt["summary"] == ""
    assert prompt["needs_summary"] is True
    assert prompt["needs_photo"] is True
    assert any("short summary" in item and "picture" in item for item in sent)


def test_runtime_finish_confirmation_no_keeps_task_active() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["clickup_prompt"] = {
        "type": "task_finish_confirmation",
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "summary_candidate": "I finished formalize project tree.",
        "requested_at": "2026-05-28T14:00:00",
    }
    inbound = MessageRecord(
        message_id="msg-no",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:01:00"),
        content="no",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_clickup_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            SimpleNamespace(recovered=False, stuck=False),
            datetime.fromisoformat("2026-05-28T14:01:00"),
        )
    )
    assert handled is True
    assert "clickup_prompt" not in session.metadata
    assert "pending_admin_review" not in session.metadata
    assert session.stage == "active"
    assert any("leave the task active" in item for item in sent)


def test_runtime_almost_done_progress_does_not_trigger_finish_confirmation() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="ali",
        display_name="Ali",
        discord_user_id=2,
        discord_username="ali",
        storage_folder_name="AliAhmed",
    )
    session = SessionState(user_key="ali", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "Convert CAD to URDF"
    inbound = MessageRecord(
        message_id="msg-progress",
        direction="inbound",
        author_id=2,
        created_at=datetime.fromisoformat("2026-05-28T17:25:00"),
        content="all the unnecessary components are suppressed, and almost done with mating",
        attachments=[],
    )
    asyncio.run(
        runtime._apply_post_route_clickup_automation(
            SimpleNamespace(),
            user,
            session,
            inbound,
            SimpleNamespace(recovered=False, stuck=False),
            datetime.fromisoformat("2026-05-28T17:25:00"),
        )
    )
    assert "clickup_prompt" not in session.metadata
    assert "pending_admin_review" not in session.metadata
    assert sent == []


def test_runtime_finished_but_still_working_progress_does_not_trigger_finish_confirmation() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=3,
        discord_username="navin",
        storage_folder_name="NavinNagavel",
    )
    session = SessionState(user_key="navin", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "Onshape CAD work"
    inbound = MessageRecord(
        message_id="msg-progress-2",
        direction="inbound",
        author_id=3,
        created_at=datetime.fromisoformat("2026-05-28T14:35:00"),
        content=(
            "I have finished developing the CAD part, sent it to George, waiting for approval, "
            "and in the meantime I am still working through some Onshape CAD tutorials."
        ),
        attachments=[],
    )
    asyncio.run(
        runtime._apply_post_route_clickup_automation(
            SimpleNamespace(),
            user,
            session,
            inbound,
            SimpleNamespace(recovered=False, stuck=False),
            datetime.fromisoformat("2026-05-28T14:35:00"),
        )
    )
    assert "clickup_prompt" not in session.metadata
    assert "pending_admin_review" not in session.metadata
    assert sent == []


def test_runtime_generic_done_not_treated_as_clock_out() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    inbound = MessageRecord(
        message_id="msg-1",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:00:00"),
        content="im done",
        attachments=[],
    )
    asyncio.run(
        runtime._route_message(
            SimpleNamespace(),
            user,
            session,
            inbound,
            SimpleNamespace(clocking_out=True, stuck=False, recovered=False, clocked_in=False),
            datetime.fromisoformat("2026-05-28T14:00:00"),
        )
    )
    assert session.stage == "active"
    assert session.clocked_out_at is None
    assert session.latest_status == "im done"
    assert sent == []


def test_runtime_stuck_prompt_offers_admin_names_and_task_draft() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    inbound = MessageRecord(
        message_id="msg-stuck",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:10:00"),
        content="I am blocked and need help on the controller issue.",
        attachments=[],
    )
    asyncio.run(
        runtime._maybe_prompt_stuck_assistance(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:10:00"),
        )
    )
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "blocker_resolution"
    assert prompt["step"] == "offer_help"
    assert any("Ask admin" in item for item in sent)
    assert any("Draft an unblocker task" in item for item in sent)


def test_runtime_stuck_prompt_can_notify_specific_admin() -> None:
    runtime = _build_runtime(
        admins=[
            AdminProfile(name="George", discord_user_id=999),
            AdminProfile(name="Erik", discord_user_id=1000),
        ]
    )
    sent: list[str] = []
    admin_messages: list[tuple[str, list[str]]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_admin_notice(_client, content: str, *, target_admins=None, files=None, user=None, session=None):
        admin_messages.append((content, [admin.name for admin in (target_admins or [])]))
        return [admin.name for admin in (target_admins or [])]

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._send_admin_notice = fake_admin_notice  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active", latest_blocker="controller issue")
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["clickup_prompt"] = {
        "type": "blocker_resolution",
        "step": "choose_admin",
        "blocker_text": "controller issue",
    }
    inbound = MessageRecord(
        message_id="msg-help",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:11:00"),
        content="Can George help me with this?",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_blocker_resolution_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:11:00"),
            session.metadata["clickup_prompt"],
        )
    )
    assert handled is True
    assert "clickup_prompt" not in session.metadata
    assert admin_messages[0][1] == ["George"]
    assert any("I messaged George" in item for item in sent)


def test_runtime_blocker_prompt_declined_help_moves_to_keep_or_clear_followup() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=3,
        discord_username="navin",
        storage_folder_name="NavinNagavel",
    )
    session = SessionState(
        user_key="navin",
        session_date="2026-05-28",
        stage="active",
        stuck_since="2026-05-28T13:15:00",
        latest_blocker="waiting on fitment direction",
    )
    session.metadata["clickup_prompt"] = {
        "type": "blocker_resolution",
        "step": "offer_help",
        "blocker_text": "waiting on fitment direction",
        "origin_message_id": "msg-blocked",
    }
    inbound = MessageRecord(
        message_id="msg-no-help",
        direction="inbound",
        author_id=3,
        created_at=datetime.fromisoformat("2026-05-28T13:16:00"),
        content="No I don't need any help",
        attachments=[],
    )
    handled = asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            inbound,
            inbound.created_at,
        )
    )
    assert handled is None
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "blocker_resolution"
    assert prompt["step"] == "declined_help_followup"
    assert any("keep this blocker logged" in item.lower() for item in sent)


def test_runtime_blocker_prompt_keep_logged_marks_no_help_requested() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=3,
        discord_username="navin",
        storage_folder_name="NavinNagavel",
    )
    session = SessionState(
        user_key="navin",
        session_date="2026-05-28",
        stage="active",
        stuck_since="2026-05-28T13:15:00",
        latest_blocker="waiting on fitment direction",
    )
    session.metadata["clickup_prompt"] = {
        "type": "blocker_resolution",
        "step": "declined_help_followup",
        "blocker_text": "waiting on fitment direction",
        "help_decision": "declined",
    }
    inbound = MessageRecord(
        message_id="msg-keep",
        direction="inbound",
        author_id=3,
        created_at=datetime.fromisoformat("2026-05-28T13:17:00"),
        content="keep logged",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_blocker_resolution_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            inbound.created_at,
            session.metadata["clickup_prompt"],
        )
    )
    assert handled is True
    assert "clickup_prompt" not in session.metadata
    assert session.metadata["blocker_state"] == "blocked_no_help"
    assert session.latest_blocker == "waiting on fitment direction"
    assert any("will keep the blocker logged locally" in item.lower() for item in sent)


def test_runtime_blocker_prompt_clear_it_clears_blocker_state() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=3,
        discord_username="navin",
        storage_folder_name="NavinNagavel",
    )
    session = SessionState(
        user_key="navin",
        session_date="2026-05-28",
        stage="active",
        stuck_since="2026-05-28T13:15:00",
        latest_blocker="waiting on fitment direction",
    )
    session.metadata["clickup_prompt"] = {
        "type": "blocker_resolution",
        "step": "declined_help_followup",
        "blocker_text": "waiting on fitment direction",
        "help_decision": "declined",
    }
    inbound = MessageRecord(
        message_id="msg-clear",
        direction="inbound",
        author_id=3,
        created_at=datetime.fromisoformat("2026-05-28T13:17:00"),
        content="clear it",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_blocker_resolution_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            inbound.created_at,
            session.metadata["clickup_prompt"],
        )
    )
    assert handled is True
    assert "clickup_prompt" not in session.metadata
    assert session.metadata["blocker_state"] == "not_blocked"
    assert session.latest_blocker is None
    assert session.stuck_since is None
    assert any("cleared the blocker" in item.lower() for item in sent)


def test_runtime_blocked_no_help_does_not_reprompt_until_help_is_requested() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=3,
        discord_username="navin",
        storage_folder_name="NavinNagavel",
    )
    session = SessionState(
        user_key="navin",
        session_date="2026-05-28",
        stage="active",
        stuck_since="2026-05-28T13:15:00",
        latest_blocker="waiting on fitment direction",
    )
    session.metadata["blocker_state"] = "blocked_no_help"
    inbound = MessageRecord(
        message_id="msg-still-blocked",
        direction="inbound",
        author_id=3,
        created_at=datetime.fromisoformat("2026-05-28T13:18:00"),
        content="still blocked on fitment",
        attachments=[],
    )
    asyncio.run(
        runtime._apply_post_route_clickup_automation(
            SimpleNamespace(),
            user,
            session,
            inbound,
            SimpleNamespace(recovered=False, stuck=True, blocked_status=True, help_requested=False),
            inbound.created_at,
        )
    )
    assert "clickup_prompt" not in session.metadata
    assert sent == []

    inbound_help = MessageRecord(
        message_id="msg-help-now",
        direction="inbound",
        author_id=3,
        created_at=datetime.fromisoformat("2026-05-28T13:20:00"),
        content="I need help now",
        attachments=[],
    )
    asyncio.run(
        runtime._apply_post_route_clickup_automation(
            SimpleNamespace(),
            user,
            session,
            inbound_help,
            SimpleNamespace(recovered=False, stuck=True, blocked_status=False, help_requested=True),
            inbound_help.created_at,
        )
    )
    assert session.metadata["clickup_prompt"]["type"] == "blocker_resolution"
    assert any("What do you want me to do about it" in item for item in sent)


def test_runtime_send_admin_notice_fans_out_to_all_admins_by_default() -> None:
    runtime = _build_runtime(
        admins=[
            AdminProfile(name="George", discord_user_id=999),
            AdminProfile(name="Erik", discord_user_id=1000),
        ]
    )
    appended: list[tuple[str, str, int, str]] = []

    class _FakeDM:
        def __init__(self, label: str) -> None:
            self.label = label

        async def send(self, content=None, files=None):
            del files
            return SimpleNamespace(
                id=f"msg-{self.label}",
                created_at=datetime.fromisoformat("2026-05-28T14:30:00"),
                content=content or "",
            )

    class _FakeDiscordUser:
        def __init__(self, label: str) -> None:
            self.label = label

        async def create_dm(self):
            return _FakeDM(self.label)

    class _FakeClient:
        async def fetch_user(self, discord_user_id: int):
            label = "George" if discord_user_id == 999 else "Erik"
            return _FakeDiscordUser(label)

    runtime.state_store = SimpleNamespace(
        append_message=lambda user_key, session_date, message: appended.append(
            (user_key, session_date, message.author_id, message.content)
        )
    )
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")

    sent_to = asyncio.run(
        runtime._send_admin_notice(
            _FakeClient(),
            "Test multi-admin fanout",
            user=user,
            session=session,
        )
    )

    assert sent_to == ["George", "Erik"]
    assert appended == []


def test_runtime_admin_notice_does_not_pollute_intern_transcript() -> None:
    runtime = _build_runtime(
        admins=[
            AdminProfile(name="George", discord_user_id=999),
            AdminProfile(name="Erik", discord_user_id=1000),
            AdminProfile(name="Sarah", discord_user_id=1001),
            AdminProfile(name="Peter", discord_user_id=1002),
            AdminProfile(name="Rob", discord_user_id=1003),
        ]
    )

    class _FakeDM:
        async def send(self, content=None, files=None, view=None):
            del files, view
            return SimpleNamespace(
                id="msg-admin",
                created_at=datetime.fromisoformat("2026-07-08T12:33:40-07:00"),
                content=content or "",
            )

    class _FakeDiscordUser:
        async def create_dm(self):
            return _FakeDM()

    class _FakeClient:
        async def fetch_user(self, _discord_user_id: int):
            return _FakeDiscordUser()

    user = UserProfile(
        user_key="tony",
        display_name="Tony Crayne",
        discord_user_id=1,
        discord_username="tony",
        storage_folder_name="TonyCrayne",
    )
    session = SessionState(user_key="tony", session_date="2026-07-08", stage="active")
    runtime.state_store.append_message(
        user.user_key,
        session.session_date,
        MessageRecord(
            message_id="msg-tony-progress",
            direction="inbound",
            author_id=1,
            created_at=datetime.fromisoformat("2026-07-08T12:32:00-07:00"),
            content="Still wiring the lever controller.",
            attachments=[],
        ),
    )

    asyncio.run(
        runtime._send_admin_notice(
            _FakeClient(),
            "Tony Crayne sent a weak scheduled check-in reply.",
            user=user,
            session=session,
        )
    )

    transcript = build_transcript_markdown(
        user,
        session,
        runtime.state_store.list_messages(user.user_key, session.session_date),
    )

    assert transcript.count("weak scheduled check-in reply") == 0
    assert transcript.count("Still wiring the lever controller.") == 1


def test_runtime_admin_notice_mirrors_actionable_request_to_erik_slack() -> None:
    runtime = _build_runtime(
        admins=[
            AdminProfile(
                name="Erik",
                discord_user_id=1000,
                slack_user_id="UERIK",
            )
        ]
    )
    slack_messages: list[tuple[str, str]] = []

    class _FakeSlack:
        async def post_message(self, channel_id: str, content: str):
            slack_messages.append((channel_id, content))
            return {"ok": True, "ts": "1.0"}

    class _FakeDM:
        async def send(self, content=None, files=None, view=None):
            del content, files, view
            return SimpleNamespace(id="discord-admin-message")

    class _FakeDiscordUser:
        async def create_dm(self):
            return _FakeDM()

    class _FakeClient:
        async def fetch_user(self, _discord_user_id: int):
            return _FakeDiscordUser()

    runtime.slack = _FakeSlack()

    sent_to = asyncio.run(
        runtime._send_admin_notice(
            _FakeClient(),
            "Alex needs review on the firmware task.",
        )
    )

    assert sent_to == ["Erik"]
    assert slack_messages == [
        ("UERIK", "Alex needs review on the firmware task.")
    ]


def test_runtime_recovered_signal_resumes_task_and_tracking() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    activated: list[tuple[str, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> TaskActivationResult:
        activated.append((task_id, task_name))
        return _activation_result(task_id, task_name)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._activate_clickup_task = fake_activate  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        stuck_since="2026-05-28T14:00:00",
        latest_blocker="waiting on controller dimensions",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    inbound = MessageRecord(
        message_id="msg-recovered",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:15:00"),
        content="I am unblocked now and back to work.",
        attachments=[],
    )
    asyncio.run(
        runtime._apply_post_route_clickup_automation(
            SimpleNamespace(),
            user,
            session,
            inbound,
            SimpleNamespace(recovered=True, stuck=False),
            datetime.fromisoformat("2026-05-28T14:15:00"),
        )
    )
    assert session.stuck_since is None
    assert session.latest_blocker is None
    assert session.metadata["last_resolved_blocker"] == "waiting on controller dimensions"
    assert activated == [("868jun6qg", "formalize project tree")]
    assert any("confirmed `formalize project tree` is back to `in progress`" in item.lower() for item in sent)


def test_runtime_auto_clock_out_activity_requires_explicit_resume_and_preserves_status() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    activated: list[tuple[str, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> TaskActivationResult:
        activated.append((task_id, task_name))
        return _activation_result(task_id, task_name)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._activate_clickup_task = fake_activate  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="clocked_out",
        clocked_in_at="2026-05-28T09:00:00",
        intake_completed_at="2026-05-28T09:20:00",
        clocked_out_at="2026-05-28T15:00:00",
        work_segments=[{"clocked_in_at": "2026-05-28T09:00:00", "clocked_out_at": "2026-05-28T15:00:00"}],
        latest_blocker="waiting on controller dimensions",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["auto_clock_out_at"] = "2026-05-28T15:00:00"
    inbound = MessageRecord(
        message_id="msg-blocked",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T15:16:00"),
        content="im blocked again",
        attachments=[],
    )
    signals = SimpleNamespace(
        clocked_in=False,
        clocking_out=False,
        starting_lunch=False,
        ending_lunch=False,
        recovered=False,
        stuck=True,
    )

    asyncio.run(runtime._route_message(SimpleNamespace(), user, session, inbound, signals, inbound.created_at))
    asyncio.run(runtime._apply_post_route_clickup_automation(SimpleNamespace(), user, session, inbound, signals, inbound.created_at))

    assert session.stage == "clocked_out"
    assert session.clocked_out_at == "2026-05-28T15:00:00"
    assert session.metadata["auto_clock_out_at"] == "2026-05-28T15:00:00"
    assert activated == []
    assert session.latest_status == "im blocked again"
    assert session.latest_blocker == "im blocked again"
    assert any("did not restart it" in item.lower() for item in sent)


def test_runtime_persist_session_state_writes_transition_log_only_on_change() -> None:
    runtime = _build_runtime()
    appended: list[tuple[Path, dict[str, object]]] = []
    saved: list[str] = []

    async def fake_archive(_user, _session) -> None:
        return None

    async def fake_ensure_workspace(_user, _session_date: str):
        return SimpleNamespace(daily_dir=Path("tests"))

    async def fake_append_json_line(path: Path, payload: dict[str, object]) -> Path:
        appended.append((path, payload))
        return path

    runtime._archive_session = fake_archive  # type: ignore[method-assign]
    runtime.store = SimpleNamespace(
        ensure_user_workspace=fake_ensure_workspace,
        append_json_line=fake_append_json_line,
    )
    runtime.state_store = SimpleNamespace(save_session=lambda session: saved.append(session.stage))
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    previous = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    current = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        latest_status="Still working on the controller integration.",
        metadata={
            "task_onboarding_plan": "Rewire the controller harness.",
            "task_onboarding_tangible_result": "A working harness that powers on cleanly.",
            "task_onboarding_estimated_duration": "2 hours",
            "last_task_onboarding_summary": "Plan: Rewire the controller harness.",
        },
    )
    logged = asyncio.run(
        runtime._persist_session_state(
            user,
            current,
            now=datetime.fromisoformat("2026-05-28T14:30:00"),
            previous_session=previous,
            trigger="unit_test",
            details={"source": "test"},
        )
    )
    assert logged is True
    assert saved == ["active"]
    assert appended
    path, payload = appended[0]
    assert path.name == "state_machine_changes.jsonl"
    assert payload["trigger"] == "unit_test"
    assert "latest.status" in payload["changed_fields"]
    assert payload["current"]["latest"]["status"] == "Still working on the controller integration."
    assert payload["current"]["task_onboarding"]["estimated_duration"] == "2 hours"
    assert payload["current"]["task_onboarding"]["summary"] == "Plan: Rewire the controller harness."

    appended.clear()
    saved.clear()
    logged = asyncio.run(
        runtime._persist_session_state(
            user,
            current,
            now=datetime.fromisoformat("2026-05-28T14:31:00"),
            previous_session=runtime._clone_session_state(current),
            trigger="unit_test",
            details={"source": "test"},
        )
    )
    assert logged is False
    assert saved == ["active"]
    assert appended == []


def test_runtime_refresh_session_time_summary_populates_daily_totals() -> None:
    runtime = _build_runtime()
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T22:00:00",
        work_segments=[
            {"clocked_in_at": "2026-05-28T09:00:00", "clocked_out_at": "2026-05-28T11:00:00"},
            {"clocked_in_at": "2026-05-28T22:00:00", "clocked_out_at": None},
        ],
        metadata={
            "clickup_time_tracking_history": [
                {
                    "task_id": "task-a",
                    "task_name": "Morning wiring",
                    "started_at": "2026-05-28T09:30:00",
                    "closed_at": "2026-05-28T10:00:00",
                    "duration_seconds": 1800,
                }
            ],
            "clickup_time_tracking": {
                "task_id": "task-b",
                "task_name": "Night software",
                "started_at": "2026-05-28T22:30:00",
            },
        },
    )
    now = datetime.fromisoformat("2026-05-28T23:30:00")

    runtime._refresh_session_time_summary(session, now)

    assert session.time_summary["gross_clocked_in_total_seconds"] == 12600
    assert session.time_summary["unpaid_lunch_deducted_seconds"] == 0
    assert session.time_summary["clocked_in_total_seconds"] == 12600
    assert session.time_summary["clocked_in_total_human"] == "3h 30m"
    assert session.time_summary["task_tracked_total_seconds"] == 5400
    assert session.time_summary["task_tracked_total_human"] == "1h 30m"
    assert session.time_summary["work_segment_count"] == 2
    assert session.time_summary["has_open_work_segment"] is True
    assert session.time_summary["active_task_timer_running"] is True
    assert session.time_summary["time_by_task"] == [
        {
            "task_id": "task-b",
            "task_name": "Night software",
            "seconds": 3600,
            "human_duration": "1h",
        },
        {
            "task_id": "task-a",
            "task_name": "Morning wiring",
            "seconds": 1800,
            "human_duration": "30m",
        },
    ]


def test_runtime_refresh_session_time_summary_deducts_recorded_lunch_on_any_date() -> None:
    runtime = _build_runtime()
    session = SessionState(
        user_key="andrew",
        session_date="2026-07-08",
        stage="clocked_out",
        clocked_in_at="2026-07-08T09:00:00-07:00",
        clocked_out_at="2026-07-08T18:00:00-07:00",
        work_segments=[
            {"clocked_in_at": "2026-07-08T09:00:00-07:00", "clocked_out_at": "2026-07-08T18:00:00-07:00"}
        ],
        metadata={
            "lunch_started_at": "2026-07-08T12:20:00-07:00",
            "lunch_ended_at": "2026-07-08T13:00:00-07:00",
        },
    )

    runtime._refresh_session_time_summary(session, datetime.fromisoformat("2026-07-08T18:00:00-07:00"))

    assert session.time_summary["gross_clocked_in_total_seconds"] == 32400
    assert session.time_summary["unpaid_lunch_deducted_seconds"] == 2400
    assert session.time_summary["clocked_in_total_seconds"] == 30000
    assert session.time_summary["clocked_in_total_human"] == "8h 20m"


def test_runtime_refresh_session_time_summary_never_deducts_unrecorded_flat_lunch() -> None:
    runtime = _build_runtime()
    session = SessionState(
        user_key="andrew",
        session_date="2026-07-09",
        stage="clocked_out",
        clocked_in_at="2026-07-09T09:00:00-07:00",
        clocked_out_at="2026-07-09T17:30:00-07:00",
        work_segments=[
            {"clocked_in_at": "2026-07-09T09:00:00-07:00", "clocked_out_at": "2026-07-09T12:30:00-07:00"},
            {"clocked_in_at": "2026-07-09T13:00:00-07:00", "clocked_out_at": "2026-07-09T17:30:00-07:00"},
        ],
    )

    runtime._refresh_session_time_summary(session, datetime.fromisoformat("2026-07-09T17:30:00-07:00"))

    assert session.time_summary["gross_clocked_in_total_seconds"] == 28800
    assert session.time_summary["unpaid_lunch_deducted_seconds"] == 0
    assert session.time_summary["clocked_in_total_seconds"] == 28800


def test_runtime_refresh_session_time_summary_deducts_open_and_multiple_lunch_windows() -> None:
    runtime = _build_runtime()
    session = SessionState(
        user_key="andrew",
        session_date="2026-07-28",
        stage="on_lunch_break",
        clocked_in_at="2026-07-28T09:00:00-07:00",
        work_segments=[
            {"clocked_in_at": "2026-07-28T09:00:00-07:00", "clocked_out_at": None},
        ],
        metadata={
            "lunch_started_at": "2026-07-28T15:00:00-07:00",
            "lunch_windows": [
                {
                    "started_at": "2026-07-28T12:00:00-07:00",
                    "ended_at": "2026-07-28T12:20:00-07:00",
                },
                {
                    "started_at": "2026-07-28T15:00:00-07:00",
                    "ended_at": None,
                },
            ],
        },
    )

    runtime._refresh_session_time_summary(
        session,
        datetime.fromisoformat("2026-07-28T15:30:00-07:00"),
    )

    assert session.time_summary["gross_clocked_in_total_seconds"] == 23400
    assert session.time_summary["unpaid_lunch_deducted_seconds"] == 3000
    assert session.time_summary["clocked_in_total_seconds"] == 20400


def test_runtime_recorded_lunch_is_clamped_to_work_segments() -> None:
    runtime = _build_runtime()
    session = SessionState(
        user_key="andrew",
        session_date="2026-07-28",
        stage="clocked_out",
        clocked_in_at="2026-07-28T09:00:00-07:00",
        clocked_out_at="2026-07-28T14:00:00-07:00",
        work_segments=[
            {
                "clocked_in_at": "2026-07-28T09:00:00-07:00",
                "clocked_out_at": "2026-07-28T12:15:00-07:00",
            },
            {
                "clocked_in_at": "2026-07-28T12:45:00-07:00",
                "clocked_out_at": "2026-07-28T14:00:00-07:00",
            },
        ],
        metadata={
            "lunch_started_at": "2026-07-28T12:00:00-07:00",
            "lunch_ended_at": "2026-07-28T13:00:00-07:00",
        },
    )

    runtime._refresh_session_time_summary(
        session,
        datetime.fromisoformat("2026-07-28T14:00:00-07:00"),
    )

    assert session.time_summary["gross_clocked_in_total_seconds"] == 16200
    assert session.time_summary["unpaid_lunch_deducted_seconds"] == 1800
    assert session.time_summary["clocked_in_total_seconds"] == 14400


def test_runtime_persist_session_state_updates_time_summary_before_save() -> None:
    runtime = _build_runtime()
    saved_summaries: list[dict[str, object]] = []

    async def fake_archive(_user, _session) -> None:
        return None

    async def fake_ensure_workspace(_user, _session_date: str):
        return SimpleNamespace(daily_dir=Path("tests"))

    async def fake_append_json_line(path: Path, payload: dict[str, object]) -> Path:
        return path

    runtime._archive_session = fake_archive  # type: ignore[method-assign]
    runtime.store = SimpleNamespace(
        ensure_user_workspace=fake_ensure_workspace,
        append_json_line=fake_append_json_line,
    )
    runtime.state_store = SimpleNamespace(
        save_session=lambda session: saved_summaries.append(dict(session.time_summary))
    )
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    previous = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    current = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        work_segments=[{"clocked_in_at": "2026-05-28T09:00:00", "clocked_out_at": "2026-05-28T11:00:00"}],
    )

    logged = asyncio.run(
        runtime._persist_session_state(
            user,
            current,
            now=datetime.fromisoformat("2026-05-28T11:00:00"),
            previous_session=previous,
            trigger="unit_test",
            details={"source": "test"},
        )
    )

    assert logged is True
    assert saved_summaries
    assert saved_summaries[0]["clocked_in_total_seconds"] == 7200
    assert saved_summaries[0]["task_tracked_total_seconds"] == 0


def test_runtime_recovered_reply_clears_stuck_prompt_and_cancels_pending_unblocker() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    admin_messages: list[str] = []
    activated: list[tuple[str, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_admin_notice(_client, content: str, **_kwargs):
        admin_messages.append(content)
        return ["George"]

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> TaskActivationResult:
        activated.append((task_id, task_name))
        return _activation_result(task_id, task_name)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._send_admin_notice = fake_admin_notice  # type: ignore[method-assign]
    runtime._activate_clickup_task = fake_activate  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        stuck_since="2026-05-28T14:00:00",
        latest_blocker="waiting on pinout",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["clickup_prompt"] = {
        "type": "blocker_resolution",
        "step": "offer_help",
        "blocker_text": "waiting on pinout",
    }
    session.metadata["pending_admin_unblocker_task"] = {
        "draft": {"title": "Get controller pinout"},
    }
    inbound = MessageRecord(
        message_id="msg-recovered",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:16:00"),
        content="Actually I am unblocked and can continue now.",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_clickup_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            SimpleNamespace(recovered=True, stuck=False, clocking_out=False),
            datetime.fromisoformat("2026-05-28T14:16:00"),
        )
    )
    assert handled is True
    assert "clickup_prompt" not in session.metadata
    assert "pending_admin_unblocker_task" not in session.metadata
    assert session.metadata["blocker_state"] == "not_blocked"
    assert activated == [("868jun6qg", "formalize project tree")]
    assert any("cancelled the pending unblocker-task draft" in item for item in sent)
    assert any("cancelled the pending unblocker-task draft" in item for item in admin_messages)


def test_runtime_unblocker_task_prompt_submits_admin_review() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    admin_requests: list[dict[str, object]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_admin_review(_client, _user, _session, _now: datetime) -> None:
        admin_requests.append(dict(_session.metadata["pending_admin_unblocker_task"]))

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._send_admin_unblocker_task_request = fake_admin_review  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active", latest_blocker="waiting on pinout")
    prompt = {
        "type": "unblocker_task_draft",
        "step": "title",
        "draft": {"blocker_text": "waiting on pinout"},
    }
    now = datetime.fromisoformat("2026-05-28T14:12:00")

    async def run_steps():
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("1","inbound",1,now,"Get controller pinout from electrical",[]), now, prompt)
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("2","inbound",1,now,"Need someone to pull the right CAN and GPIO pin map so I can finish wiring.",[]), now, prompt)
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("3","inbound",1,now,"George",[]), now, prompt)
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("4","inbound",1,now,"high",[]), now, prompt)
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("5","inbound",1,now,"none",[]), now, prompt)

    asyncio.run(run_steps())
    assert "clickup_prompt" not in session.metadata
    proposal = session.metadata["pending_admin_unblocker_task"]
    assert proposal["draft"]["title"] == "Get controller pinout from electrical"
    assert proposal["draft"]["priority"] == "high"
    assert admin_requests
    assert any("sent the unblocker-task draft to admin" in item for item in sent)


def test_runtime_mistaken_blocker_task_creation_restores_opt_in_prompt() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime.interface_intelligence.resolve_task_draft_intent = (  # type: ignore[attr-defined]
        lambda _text, **_kwargs: asyncio.sleep(
            0,
            result=SimpleNamespace(action="mistaken_task_creation", confidence=0.92),
        )
    )
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["clickup_prompt"] = {
        "type": "blocker_task",
        "step": "title",
        "draft": {
            "origin_message_id": "origin-1",
            "title": "placeholder",
        },
    }

    handled = asyncio.run(
        runtime._handle_clickup_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-mistake-blocker",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T14:12:00"),
                content="actually don't make a task for this, that was the wrong thing",
                attachments=[],
            ),
            SimpleNamespace(recovered=False, stuck=False, clocking_out=False),
            datetime.fromisoformat("2026-05-28T14:12:00"),
        )
    )

    assert handled is True
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "blocker_task"
    assert prompt["step"] == "opt_in"
    assert prompt["draft"] == {"origin_message_id": "origin-1"}
    assert any("won't create a blocker task" in item.lower() for item in sent)
    assert "Mission Board blocker task" in sent[-1]


def test_runtime_mistaken_unblocker_task_creation_restores_blocker_resolution_prompt() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime.interface_intelligence.resolve_task_draft_intent = (  # type: ignore[attr-defined]
        lambda _text, **_kwargs: asyncio.sleep(
            0,
            result=SimpleNamespace(action="mistaken_task_creation", confidence=0.95),
        )
    )
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active", latest_blocker="waiting on pinout")
    prompt = {
        "type": "unblocker_task_draft",
        "step": "title",
        "draft": {"blocker_text": "waiting on pinout"},
        "return_prompt": {
            "type": "blocker_resolution",
            "step": "offer_help",
            "origin_message_id": "origin-blocker",
            "blocker_text": "waiting on pinout",
            "help_decision": None,
            "blocked_state_after_decline": None,
        },
    }

    handled = asyncio.run(
        runtime._handle_unblocker_task_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-mistake-unblocker",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T14:12:00"),
                content="never mind, I meant get help without making another task",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T14:12:00"),
            prompt,
        )
    )

    assert handled is True
    restored = session.metadata["clickup_prompt"]
    assert restored["type"] == "blocker_resolution"
    assert restored["step"] == "offer_help"
    assert any("won't create an unblocker task" in item.lower() for item in sent)
    assert "That sounds blocked." in sent[-1]


def test_runtime_mistaken_admin_revision_unblocker_task_creation_cancels_cleanly() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime.interface_intelligence.resolve_task_draft_intent = (  # type: ignore[attr-defined]
        lambda _text, **_kwargs: asyncio.sleep(
            0,
            result=SimpleNamespace(action="mistaken_task_creation", confidence=0.89),
        )
    )
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    prompt = {
        "type": "unblocker_task_draft",
        "step": "description",
        "draft": {"title": "Get controller pinout"},
        "revision_feedback": "Need a cleaner title.",
    }
    session.metadata["clickup_prompt"] = prompt

    handled = asyncio.run(
        runtime._handle_unblocker_task_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-mistake-revision-unblocker",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T14:12:00"),
                content="sorry that was a mistake, don't draft that task",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T14:12:00"),
            prompt,
        )
    )

    assert handled is True
    assert "clickup_prompt" not in session.metadata
    assert sent[-1] == "Okay, I cancelled the unblocker-task draft."


def test_runtime_resolves_admin_unblocker_assignee_from_clickup_member_lookup() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )

    async def fake_resolve_workspace_member_id(*, name=None, email=None):
        assert name == "George"
        return "198031927"

    runtime.clickup = SimpleNamespace(resolve_workspace_member_id=fake_resolve_workspace_member_id)
    label, assignee_id, note, self_assigned = asyncio.run(runtime._resolve_unblocker_assignee(user, "George"))
    assert label == "George"
    assert assignee_id == "198031927"
    assert note is None
    assert self_assigned is False


def test_runtime_resolves_self_unblocker_assignee_alias_to_requester() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )

    label, assignee_id, note, self_assigned = asyncio.run(runtime._resolve_unblocker_assignee(user, "me"))

    assert label == "Andrew"
    assert assignee_id == "87438366"
    assert note is None
    assert self_assigned is True


def test_runtime_unblocker_task_prompt_self_assignment_creates_task_and_switches() -> None:
    runtime = _build_runtime()
    runtime.config.clickup.mission_board_list_id = "mission-board"
    runtime.config.clickup.new_task_approval_required = False
    sent: list[str] = []
    admin_messages: list[str] = []
    create_calls: list[dict[str, object]] = []
    pauses: list[tuple[bool, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_admin_notice(_client, content: str, **_kwargs):
        admin_messages.append(content)
        return ["George"]

    async def fake_create_task(list_id: str, **kwargs):
        create_calls.append({"list_id": list_id, **kwargs})
        return {"id": "self-unblocker-1", "name": kwargs["name"]}

    async def fake_pause(_user, _session, _now, *, set_hold: bool, end_reason: str):
        pauses.append((set_hold, end_reason))
        return "I paused local task tracking for `formalize project tree` and will keep the time window in the local log."

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._send_admin_notice = fake_admin_notice  # type: ignore[method-assign]
    runtime.clickup.create_task = fake_create_task  # type: ignore[method-assign]
    runtime._pause_current_task_tracking = fake_pause  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        latest_blocker="waiting on pinout",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    prompt = {
        "type": "unblocker_task_draft",
        "step": "title",
        "draft": {"blocker_text": "waiting on pinout"},
    }
    session.metadata["clickup_prompt"] = prompt
    now = datetime.fromisoformat("2026-05-28T14:12:00")

    async def run_steps():
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("1", "inbound", 1, now, "Get controller pinout from electrical", []), now, prompt)
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("2", "inbound", 1, now, "Need someone to pull the right CAN and GPIO pin map so I can finish wiring.", []), now, prompt)
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("3", "inbound", 1, now, "me", []), now, prompt)
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("4", "inbound", 1, now, "high", []), now, prompt)
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("5", "inbound", 1, now, "none", []), now, prompt)

    asyncio.run(run_steps())

    assert len(create_calls) == 1
    assert create_calls[0]["list_id"] == "mission-board"
    assert create_calls[0]["name"] == "Get controller pinout from electrical"
    assert create_calls[0]["assignee_ids"] == ["87438366"]
    assert create_calls[0]["priority"] == "high"
    assert create_calls[0]["due_date"] is None
    assert create_calls[0]["tags"] == ["unblocker", "andrew"]
    assert "waiting on pinout" in str(create_calls[0]["description"])
    assert pauses == [(True, "self_assigned_unblocker_switch")]
    assert "pending_admin_unblocker_task" not in session.metadata
    assert session.metadata["clickup_prompt"]["type"] == "task_onboarding"
    assert session.metadata["clickup_prompt"]["source"] == "self_assigned_unblocker_task"
    assert session.metadata["clickup_prompt"]["step"] == "plan"
    assert session.metadata["clickup_prompt"]["task_id"] == "self-unblocker-1"
    assert session.stage == "awaiting_plan"
    assert session.metadata["blocker_state"] == "not_blocked"
    assert session.latest_blocker is None
    assert session.metadata["last_resolved_blocker"] == "waiting on pinout"
    assert "active_clickup_task_id" not in session.metadata
    assert any("created `Get controller pinout from electrical` as your unblocker task" in item for item in sent)
    assert any("What is your plan for `Get controller pinout from electrical`?" in item for item in sent)
    assert any("created a self-assigned unblocker task and switched onto it" in item for item in admin_messages)
    assert any("Blocked task: `formalize project tree` (868jun6qg)" in item for item in admin_messages)


def test_runtime_unblocker_task_prompt_self_assignment_falls_back_to_admin_review_when_user_mapping_missing() -> None:
    runtime = _build_runtime()
    runtime.config.clickup.mission_board_list_id = "mission-board"
    sent: list[str] = []
    admin_requests: list[dict[str, object]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_admin_review(_client, _user, _session, _now: datetime) -> None:
        admin_requests.append(dict(_session.metadata["pending_admin_unblocker_task"]))

    async def fake_resolve_clickup_user_id(_user):
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._send_admin_unblocker_task_request = fake_admin_review  # type: ignore[method-assign]
    runtime.clickup.resolve_clickup_user_id = fake_resolve_clickup_user_id  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active", latest_blocker="waiting on pinout")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    prompt = {
        "type": "unblocker_task_draft",
        "step": "title",
        "draft": {"blocker_text": "waiting on pinout"},
    }
    session.metadata["clickup_prompt"] = prompt
    now = datetime.fromisoformat("2026-05-28T14:12:00")

    async def run_steps():
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("1", "inbound", 1, now, "Get controller pinout from electrical", []), now, prompt)
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("2", "inbound", 1, now, "Need someone to pull the right CAN and GPIO pin map so I can finish wiring.", []), now, prompt)
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("3", "inbound", 1, now, "me", []), now, prompt)
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("4", "inbound", 1, now, "high", []), now, prompt)
        await runtime._handle_unblocker_task_prompt(SimpleNamespace(), user, session, MessageRecord("5", "inbound", 1, now, "none", []), now, prompt)

    asyncio.run(run_steps())

    assert "clickup_prompt" not in session.metadata
    assert session.metadata["pending_admin_unblocker_task"]["draft"]["self_assigned"] is True
    assert admin_requests
    assert any("could not resolve your clickup assignee id" in item.lower() for item in sent)
    assert any("sent the unblocker-task draft to admin for review instead" in item.lower() for item in sent)


def test_runtime_second_admin_unblocker_resolution_is_rejected_after_first_resolution() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_create_task(*_args, **_kwargs):
        return {"id": "868ju-test", "name": "Get controller pinout"}

    async def fake_set_task_state(_task_id: str, state: str):
        return state

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime.clickup = SimpleNamespace(create_task=fake_create_task, set_task_state=fake_set_task_state)
    runtime.config.clickup.mission_board_list_id = "901113819433"
    runtime.refresh_configuration = lambda force=False: asyncio.sleep(0)  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["pending_admin_unblocker_task"] = {
        "active_task_name": "formalize project tree",
        "draft": {
            "title": "Get controller pinout",
            "description": "Need the controller pin map.",
            "help_needed": "Electrical team to confirm the CAN and GPIO pins.",
            "priority": "high",
            "assignee_id": "198031927",
        },
    }

    first = asyncio.run(
        runtime.resolve_admin_unblocker_task(
            SimpleNamespace(),
            user,
            session,
            approve_create=True,
            admin_message="Create it.",
            now=datetime.fromisoformat("2026-05-28T14:25:00"),
        )
    )
    second = asyncio.run(
        runtime.resolve_admin_unblocker_task(
            SimpleNamespace(),
            user,
            session,
            approve_create=False,
            admin_message="Actually revise it.",
            now=datetime.fromisoformat("2026-05-28T14:26:00"),
        )
    )

    assert first == "Created `Get controller pinout` for Andrew."
    assert second == "Andrew does not have an unblocker task draft waiting on admin review."
    assert any("Admin approved the unblocker task" in item for item in sent)


def test_runtime_admin_resume_clears_blocker_and_restarts_task() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    activated: list[tuple[str, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> TaskActivationResult:
        activated.append((task_id, task_name))
        return _activation_result(task_id, task_name)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._activate_clickup_task = fake_activate  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        stuck_since="2026-05-28T13:00:00",
        latest_blocker="waiting on approval",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    result = asyncio.run(
        runtime.resume_user_after_unblock(
            SimpleNamespace(),
            user,
            session,
            now=datetime.fromisoformat("2026-05-28T14:20:00"),
            source="admin",
            actor_note="Andrew is unblocked, put him back on the task.",
            notify_user=True,
        )
    )
    assert result == "Perfect. I confirmed `formalize project tree` is back to `in progress` and task tracking is running again."
    assert session.stuck_since is None
    assert session.latest_blocker is None
    assert activated == [("868jun6qg", "formalize project tree")]
    assert any("task tracking is running again" in item for item in sent)


def test_runtime_task_review_prompt_can_collect_summary_after_photo() -> None:
    runtime = _build_runtime()
    captured: dict[str, object] = {}

    async def fake_initiate(_client, _user, _session, _inbound, _now, *, review_summary=None, completion_photo_paths=None):
        captured["summary"] = review_summary
        captured["photo_paths"] = completion_photo_paths

    runtime._initiate_admin_review = fake_initiate  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    photo_path = str(Path("tests") / "completion-review.jpg")
    session.metadata["clickup_prompt"] = {
        "type": "task_review_submission",
        "summary": "",
        "needs_summary": True,
        "needs_photo": False,
        "photo_paths": [photo_path],
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
    }
    inbound = MessageRecord(
        message_id="msg-2",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:05:00"),
        content="I finished the project tree cleanup and verified the folders.",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_task_review_submission_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:05:00"),
            session.metadata["clickup_prompt"],
        )
    )
    assert handled is True
    assert "clickup_prompt" not in session.metadata
    assert captured["summary"] == "I finished the project tree cleanup and verified the folders."
    assert captured["photo_paths"] == [photo_path]


def test_runtime_task_review_prompt_not_done_cancels_and_keeps_task_active() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["clickup_prompt"] = {
        "type": "task_review_submission",
        "summary": "I finished the project tree cleanup.",
        "needs_summary": False,
        "needs_photo": True,
        "photo_paths": [],
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
    }
    inbound = MessageRecord(
        message_id="msg-not-done",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T14:06:00"),
        content="It's not done yet. I'm still working.",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_task_review_submission_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T14:06:00"),
            session.metadata["clickup_prompt"],
        )
    )
    assert handled is True
    assert "clickup_prompt" not in session.metadata
    assert session.stage == "active"
    assert "pending_admin_review" not in session.metadata
    assert any("left the task active" in item for item in sent)


def test_runtime_resolve_admin_review_rework_starts_same_task_onboarding() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    states: list[tuple[str | None, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_state(_session, task_id: str | None, state: str) -> None:
        states.append((task_id, state))

    async def fake_comment(_task_id: str, _comment_text: str) -> None:
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._safe_set_task_state = fake_state  # type: ignore[method-assign]
    runtime.clickup = SimpleNamespace(comment_on_task=fake_comment)
    runtime.refresh_configuration = lambda force=False: asyncio.sleep(0)  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_admin_review")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["pending_admin_review"] = {
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
    }
    result = asyncio.run(
        runtime.resolve_admin_review(
            SimpleNamespace(),
            user,
            session,
            approve_close=False,
            admin_message="Fix the wiring alignment and send a better photo.",
            now=datetime.fromisoformat("2026-05-28T15:00:00"),
        )
    )
    assert "Sent review feedback back to Andrew" in result
    assert session.stage == "active"
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_onboarding"
    assert prompt["step"] == "plan"
    assert prompt["task_id"] == "868jun6qg"
    assert states == [("868jun6qg", "in_progress")]
    assert any("wants more work" in item for item in sent)
    assert any("Admin feedback to account for:" in item for item in sent)
    assert any("What is your plan for `formalize project tree`?" in item for item in sent)


def test_runtime_resolve_admin_review_close_starts_next_task_selection() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    states: list[tuple[str | None, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_state(_session, task_id: str | None, state: str) -> None:
        states.append((task_id, state))

    async def fake_comment(_task_id: str, _comment_text: str) -> None:
        return None

    async def fake_prompt(_user, _session=None) -> str:
        return "Assigned tasks I can see:\n- Next task"

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._safe_set_task_state = fake_state  # type: ignore[method-assign]
    runtime._task_selection_prompt = fake_prompt  # type: ignore[method-assign]
    runtime.clickup = SimpleNamespace(comment_on_task=fake_comment)
    runtime.refresh_configuration = lambda force=False: asyncio.sleep(0)  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_admin_review")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["pending_admin_review"] = {
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
    }
    result = asyncio.run(
        runtime.resolve_admin_review(
            SimpleNamespace(),
            user,
            session,
            approve_close=True,
            admin_message="Looks good, close it.",
            now=datetime.fromisoformat("2026-05-28T15:30:00"),
        )
    )
    assert "Closed `formalize project tree`" in result
    assert session.stage == "active"
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_onboarding"
    assert prompt["step"] == "select_task"
    assert "active_clickup_task_id" not in session.metadata
    assert states == [("868jun6qg", "complete")]
    closed_tasks = session.metadata["recently_closed_clickup_tasks"]
    assert closed_tasks[0]["task_id"] == "868jun6qg"
    assert any("Assigned tasks I can see" in item for item in sent)


def test_runtime_second_admin_review_resolution_is_rejected_after_first_resolution() -> None:
    runtime = _build_runtime()

    async def fake_send(_client, _user, _session, _content: str, _now: datetime) -> None:
        return None

    async def fake_state(_session, _task_id: str | None, _state: str) -> None:
        return None

    async def fake_comment(_task_id: str, _comment_text: str) -> None:
        return None

    async def fake_prompt(_user, _session=None) -> str:
        return "Assigned tasks I can see:\n- Next task"

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._safe_set_task_state = fake_state  # type: ignore[method-assign]
    runtime._task_selection_prompt = fake_prompt  # type: ignore[method-assign]
    runtime.clickup = SimpleNamespace(comment_on_task=fake_comment)
    runtime.refresh_configuration = lambda force=False: asyncio.sleep(0)  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_admin_review")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["pending_admin_review"] = {
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
    }

    first = asyncio.run(
        runtime.resolve_admin_review(
            SimpleNamespace(),
            user,
            session,
            approve_close=True,
            admin_message="Looks good, close it.",
            now=datetime.fromisoformat("2026-05-28T15:30:00"),
        )
    )
    second = asyncio.run(
        runtime.resolve_admin_review(
            SimpleNamespace(),
            user,
            session,
            approve_close=False,
            admin_message="Actually, do more work.",
            now=datetime.fromisoformat("2026-05-28T15:31:00"),
        )
    )

    assert "Closed `formalize project tree`" in first
    assert second == "Andrew does not have a task waiting on admin review."


def test_runtime_intern_switch_request_from_active_pauses_and_opens_selection() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    pauses: list[tuple[bool, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_pause(_user, _session, _now: datetime, *, set_hold: bool, end_reason: str) -> str:
        pauses.append((set_hold, end_reason))
        return "I paused your previous task."

    async def fake_prompt(_user, _session=None) -> str:
        return "Assigned tasks I can see:\n- formalize project tree\n- secondary cleanup"

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._pause_current_task_tracking = fake_pause  # type: ignore[method-assign]
    runtime._task_selection_prompt = fake_prompt  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    handled = asyncio.run(
        runtime._maybe_handle_intern_task_switch_request(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-switch",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:00:00"),
                content="I want to switch tasks",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:00:00"),
        )
    )
    assert handled is True
    assert pauses == [(True, "intern_switch")]
    assert session.stage == "awaiting_task_selection"
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_onboarding"
    assert prompt["source"] == "intern_switch"
    assert prompt["step"] == "select_task"
    assert session.metadata["pending_intern_task_switch"]["previous_task_id"] == "868jun6qg"
    assert "active_clickup_task_id" not in session.metadata
    assert any("Assigned tasks I can see" in item for item in sent)


def test_runtime_intern_switch_request_with_hint_opens_confirmation() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        assert view is not None
        sent.append(content)

    async def fake_pause(_user, _session, _now: datetime, *, set_hold: bool, end_reason: str) -> str:
        assert set_hold is True
        assert end_reason == "intern_switch"
        return "Paused old task."

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._pause_current_task_tracking = fake_pause  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    handled = asyncio.run(
        runtime._maybe_handle_intern_task_switch_request(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-switch-hint",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:05:00"),
                content="switch task to secondary cleanup",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:05:00"),
        )
    )
    assert handled is True
    assert session.stage == "awaiting_task_selection"
    prompt = session.metadata["clickup_prompt"]
    assert prompt["step"] == "confirm_task"
    assert prompt["candidate_task_id"] == "868jun6qh"
    assert prompt["candidate_task_name"] == "secondary cleanup"
    assert any("Is that the task you want to onboard right now?" in item for item in sent)


def test_runtime_cancelled_intern_switch_restores_previous_active_task() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    resumed: list[tuple[str, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_activate(_user, _session, _now: datetime, task_id: str | None, task_name: str | None):
        resumed.append((task_id or "", task_name or ""))
        return _activation_result(task_id or "", task_name or "")

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._activate_clickup_task = fake_activate  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    session.metadata["pending_intern_task_switch"] = {
        "source": "intern_switch",
        "previous_stage": "active",
        "previous_task_id": "868jun6qg",
        "previous_task_name": "formalize project tree",
        "previous_selection_reason": "Confirmed earlier.",
        "started_at": "2026-05-28T11:00:00",
    }
    prompt = {
        "type": "task_onboarding",
        "source": "intern_switch",
        "step": "select_task",
        "reason": "Okay, let's switch tasks.",
        "draft": {},
    }
    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-cancel",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:06:00"),
                content="cancel",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:06:00"),
            prompt,
        )
    )
    assert handled is True
    assert session.stage == "active"
    assert session.metadata["active_clickup_task_id"] == "868jun6qg"
    assert resumed == [("868jun6qg", "formalize project tree")]
    assert "pending_intern_task_switch" not in session.metadata
    assert any("cancelled the task switch and resumed `formalize project tree`" in item for item in sent)


def test_runtime_cancelled_task_creation_from_intern_switch_restores_previous_active_task() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    resumed: list[tuple[str, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_activate(_user, _session, _now: datetime, task_id: str | None, task_name: str | None):
        resumed.append((task_id or "", task_name or ""))
        return _activation_result(task_id or "", task_name or "")

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._activate_clickup_task = fake_activate  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    session.metadata["pending_intern_task_switch"] = {
        "source": "intern_switch",
        "previous_stage": "active",
        "previous_task_id": "868jun6qg",
        "previous_task_name": "formalize project tree",
        "previous_selection_reason": "Confirmed earlier.",
        "started_at": "2026-05-28T11:00:00",
    }
    prompt = {
        "type": "task_creation",
        "source": "intern_switch",
        "step": "title",
        "draft": {},
    }

    handled = asyncio.run(
        runtime._handle_task_creation_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-cancel-create",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:08:00"),
                content="cancel",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:08:00"),
            prompt,
        )
    )

    assert handled is True
    assert session.stage == "active"
    assert session.metadata["active_clickup_task_id"] == "868jun6qg"
    assert resumed == [("868jun6qg", "formalize project tree")]
    assert "pending_intern_task_switch" not in session.metadata
    assert any("cancelled the task switch and resumed `formalize project tree`" in item for item in sent)


def test_runtime_mistaken_task_creation_from_onboarding_returns_to_task_selection() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_prompt(_user, _session=None) -> str:
        return "Assigned tasks I can see:\n- formalize project tree\n- secondary cleanup"

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._task_selection_prompt = fake_prompt  # type: ignore[method-assign]
    runtime.interface_intelligence.resolve_task_draft_intent = (  # type: ignore[attr-defined]
        lambda _text, **_kwargs: asyncio.sleep(
            0,
            result=SimpleNamespace(action="mistaken_task_creation", confidence=0.94),
        )
    )
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    prompt = {
        "type": "task_creation",
        "source": "task_onboarding",
        "step": "title",
        "reason": "Let's figure out the right task.",
        "draft": {
            "parent_task_id": "868jun6qg",
            "parent_task_name": "formalize project tree",
            "list_id": "list-1",
        },
    }

    handled = asyncio.run(
        runtime._handle_task_creation_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-mistake-create",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:08:00"),
                content="actually I meant to choose one of my existing tasks",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:08:00"),
            prompt,
        )
    )

    assert handled is True
    assert session.stage == "awaiting_task_selection"
    restored_prompt = session.metadata["clickup_prompt"]
    assert restored_prompt["type"] == "task_onboarding"
    assert restored_prompt["step"] == "select_task"
    assert restored_prompt["source"] == "task_onboarding"
    assert "pending_intern_task_switch" not in session.metadata
    assert any("won't create a new task" in item.lower() for item in sent)
    assert "Assigned tasks I can see:" in sent[-1]


def test_runtime_mistaken_task_creation_from_review_rework_switch_keeps_switch_state() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_prompt(_user, _session=None) -> str:
        return "Assigned tasks I can see:\n- formalize project tree\n- secondary cleanup"

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._task_selection_prompt = fake_prompt  # type: ignore[method-assign]
    runtime.interface_intelligence.resolve_task_draft_intent = (  # type: ignore[attr-defined]
        lambda _text, **_kwargs: asyncio.sleep(
            0,
            result=SimpleNamespace(action="mistaken_task_creation", confidence=0.93),
        )
    )
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_task_selection")
    session.metadata["pending_intern_task_switch"] = {
        "source": "review_rework_switch",
        "previous_stage": "active",
        "previous_task_id": "868jun6qg",
        "previous_task_name": "formalize project tree",
        "previous_selection_reason": "Confirmed earlier.",
        "started_at": "2026-05-28T11:00:00",
    }
    prompt = {
        "type": "task_creation",
        "source": "review_rework_switch",
        "step": "description",
        "reason": "Let's create what you need for the rework.",
        "draft": {
            "title": "new rework task",
            "list_id": "list-1",
        },
    }

    handled = asyncio.run(
        runtime._handle_task_creation_prompt(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-mistake-review-create",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:08:00"),
                content="I didn't mean make a task, I wanted one from my list",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:08:00"),
            prompt,
        )
    )

    assert handled is True
    assert session.stage == "awaiting_task_selection"
    restored_prompt = session.metadata["clickup_prompt"]
    assert restored_prompt["type"] == "task_onboarding"
    assert restored_prompt["step"] == "select_task"
    assert restored_prompt["source"] == "review_rework_switch"
    assert session.metadata["pending_intern_task_switch"]["source"] == "review_rework_switch"
    assert "active_clickup_task_id" not in session.metadata
    assert any("won't create a new task" in item.lower() for item in sent)


def test_runtime_intern_switch_request_from_awaiting_admin_review_keeps_queue() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_prompt(_user, _session=None) -> str:
        return "Assigned tasks I can see:\n- secondary cleanup"

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._task_selection_prompt = fake_prompt  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_admin_review")
    session.metadata["pending_admin_reviews"] = [
        {"task_id": "868jun6qg", "task_name": "formalize project tree"}
    ]
    handled = asyncio.run(
        runtime._maybe_handle_intern_task_switch_request(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-switch-review",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:10:00"),
                content="show my tasks so I can switch",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:10:00"),
        )
    )
    assert handled is True
    assert session.stage == "awaiting_task_selection"
    assert len(session.metadata["pending_admin_reviews"]) == 1
    assert any("already waiting on admin review" in item for item in sent)


def test_runtime_create_task_request_from_awaiting_admin_review_starts_task_creation() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_admin_review")
    session.metadata["pending_admin_reviews"] = [
        {"task_id": "868jun6qg", "task_name": "formalize project tree"}
    ]

    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-create-review",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:11:00"),
                content="create task",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:11:00"),
        )
    )

    assert session.stage == "awaiting_task_selection"
    assert len(session.metadata["pending_admin_reviews"]) == 1
    assert session.metadata["pending_intern_task_switch"]["previous_stage"] == "awaiting_admin_review"
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_creation"
    assert prompt["step"] == "placement"
    assert prompt["source"] == "intern_switch"
    assert any("let's create a new task" in item.lower() for item in sent)


def test_runtime_create_new_task_request_from_awaiting_admin_review_starts_task_creation() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_admin_review")
    session.metadata["pending_admin_reviews"] = [
        {"task_id": "868jun6qg", "task_name": "formalize project tree"}
    ]

    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-create-new-review",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:12:00"),
                content="create new task",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:12:00"),
        )
    )

    assert session.stage == "awaiting_task_selection"
    assert len(session.metadata["pending_admin_reviews"]) == 1
    assert session.metadata["pending_intern_task_switch"]["previous_stage"] == "awaiting_admin_review"
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_creation"
    assert prompt["step"] == "placement"
    assert prompt["source"] == "intern_switch"
    assert any("let's create a new task" in item.lower() for item in sent)


def test_runtime_cancelled_task_creation_from_awaiting_admin_review_restores_waiting_state() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_admin_review")
    session.metadata["pending_admin_reviews"] = [
        {"task_id": "868jun6qg", "task_name": "formalize project tree"}
    ]

    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-create-review",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:11:00"),
                content="create task",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:11:00"),
        )
    )
    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-cancel-review",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:12:00"),
                content="cancel",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:12:00"),
        )
    )

    assert session.stage == "awaiting_admin_review"
    assert "clickup_prompt" not in session.metadata
    assert "pending_intern_task_switch" not in session.metadata
    assert len(session.metadata["pending_admin_reviews"]) == 1
    assert sent[-1] == "Okay, I cancelled the task switch. Your earlier task is still waiting on admin review."


def test_runtime_mistaken_task_creation_from_awaiting_admin_review_returns_to_task_selection() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_prompt(_user, _session=None) -> str:
        return "Assigned tasks I can see:\n- secondary cleanup"

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._task_selection_prompt = fake_prompt  # type: ignore[method-assign]
    runtime.interface_intelligence.resolve_task_draft_intent = (  # type: ignore[attr-defined]
        lambda _text, **_kwargs: asyncio.sleep(
            0,
            result=SimpleNamespace(action="mistaken_task_creation", confidence=0.91),
        )
    )
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_admin_review")
    session.metadata["pending_admin_reviews"] = [
        {"task_id": "868jun6qg", "task_name": "formalize project tree"}
    ]

    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-create-review",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:11:00"),
                content="create task",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:11:00"),
        )
    )
    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-mistake-review",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:12:00"),
                content="that was a mistake, I want one from my assigned tasks",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:12:00"),
        )
    )

    assert session.stage == "awaiting_task_selection"
    assert len(session.metadata["pending_admin_reviews"]) == 1
    assert session.metadata["pending_intern_task_switch"]["previous_stage"] == "awaiting_admin_review"
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "task_onboarding"
    assert prompt["step"] == "select_task"
    assert prompt["source"] == "intern_switch"
    assert any("won't create a new task" in item.lower() for item in sent)
    assert "Assigned tasks I can see:" in sent[-1]


def test_runtime_non_create_message_from_awaiting_admin_review_keeps_waiting_reply() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_admin_review")
    session.metadata["pending_admin_reviews"] = [
        {"task_id": "868jun6qg", "task_name": "formalize project tree"}
    ]

    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            MessageRecord(
                message_id="msg-waiting-review",
                direction="inbound",
                author_id=1,
                created_at=datetime.fromisoformat("2026-05-28T11:13:00"),
                content="still waiting",
                attachments=[],
            ),
            datetime.fromisoformat("2026-05-28T11:13:00"),
        )
    )

    assert session.stage == "awaiting_admin_review"
    assert "clickup_prompt" not in session.metadata
    assert sent[-1] == "Your task is currently waiting on admin review. I will message you as soon as they respond."


def test_runtime_resolve_admin_review_requires_disambiguation_when_multiple_pending() -> None:
    runtime = _build_runtime()
    runtime.refresh_configuration = lambda force=False: asyncio.sleep(0)  # type: ignore[method-assign]
    runtime.clickup = SimpleNamespace(comment_on_task=lambda *_args, **_kwargs: asyncio.sleep(0))
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="awaiting_admin_review")
    session.metadata["pending_admin_reviews"] = [
        {"task_id": "868jun6qg", "task_name": "formalize project tree"},
        {"task_id": "868jun6qh", "task_name": "secondary cleanup"},
    ]
    result = asyncio.run(
        runtime.resolve_admin_review(
            SimpleNamespace(),
            user,
            session,
            approve_close=True,
            admin_message="Looks good.",
            now=datetime.fromisoformat("2026-05-28T15:30:00"),
        )
    )
    assert "multiple pending reviews" in result
    assert "formalize project tree" in result
    assert "secondary cleanup" in result


def test_runtime_resolve_admin_review_close_keeps_current_active_task() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    states: list[tuple[str | None, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_state(_session, task_id: str | None, state: str) -> None:
        states.append((task_id, state))

    async def fake_comment(_task_id: str, _comment_text: str) -> None:
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._safe_set_task_state = fake_state  # type: ignore[method-assign]
    runtime.clickup = SimpleNamespace(comment_on_task=fake_comment)
    runtime.refresh_configuration = lambda force=False: asyncio.sleep(0)  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qh"
    session.metadata["active_clickup_task_name"] = "secondary cleanup"
    session.metadata["pending_admin_reviews"] = [
        {"task_id": "868jun6qg", "task_name": "formalize project tree"},
    ]
    result = asyncio.run(
        runtime.resolve_admin_review(
            SimpleNamespace(),
            user,
            session,
            approve_close=True,
            admin_message="Looks good, close it.",
            task_id="868jun6qg",
            now=datetime.fromisoformat("2026-05-28T15:30:00"),
        )
    )
    assert "left `secondary cleanup` active" in result
    assert session.stage == "active"
    assert session.metadata["active_clickup_task_id"] == "868jun6qh"
    assert session.metadata.get("pending_admin_reviews") in (None, [])
    assert states == [("868jun6qg", "complete")]
    assert any("left `secondary cleanup` active" in item for item in sent)


def test_runtime_resolve_admin_review_rework_on_older_review_prompts_switch_choice() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    states: list[tuple[str | None, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        assert view is not None
        sent.append(content)

    async def fake_state(_session, task_id: str | None, state: str) -> None:
        states.append((task_id, state))

    async def fake_comment(_task_id: str, _comment_text: str) -> None:
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._safe_set_task_state = fake_state  # type: ignore[method-assign]
    runtime.clickup = SimpleNamespace(comment_on_task=fake_comment)
    runtime.refresh_configuration = lambda force=False: asyncio.sleep(0)  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["active_clickup_task_id"] = "868jun6qh"
    session.metadata["active_clickup_task_name"] = "secondary cleanup"
    session.metadata["pending_admin_reviews"] = [
        {"task_id": "868jun6qg", "task_name": "formalize project tree"},
    ]
    result = asyncio.run(
        runtime.resolve_admin_review(
            SimpleNamespace(),
            user,
            session,
            approve_close=False,
            admin_message="Fix the wiring alignment first.",
            task_id="868jun6qg",
            now=datetime.fromisoformat("2026-05-28T15:45:00"),
        )
    )
    assert "Queued a switch decision" in result
    assert session.stage == "active"
    assert session.metadata["active_clickup_task_id"] == "868jun6qh"
    prompt = session.metadata["clickup_prompt"]
    assert prompt["type"] == "queued_review_rework_decision"
    assert prompt["task_id"] == "868jun6qg"
    assert states == [("868jun6qg", "in_progress")]
    assert any("switch back now" in item.lower() for item in sent)


def test_runtime_task_selection_prompt_hides_recently_closed_tasks() -> None:
    runtime = _build_runtime()

    async def fake_list_assigned_tasks(_user, limit=8):
        return [
            {
                "id": "868jut9fb",
                "name": "Control the Phidgets Motor with CAN through the flipsky controller",
                "status": {"status": "to do"},
            },
            {
                "id": "868jurjm4",
                "name": "see if task priority works",
                "status": {"status": "hold"},
            },
        ]

    runtime.clickup = SimpleNamespace(list_assigned_tasks=fake_list_assigned_tasks)
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["recently_closed_clickup_tasks"] = [
        {
            "task_id": "868jurjm4",
            "task_name": "see if task priority works",
            "closed_at": "2026-05-28T18:41:35",
        }
    ]
    prompt = asyncio.run(runtime._task_selection_prompt(user, session))
    assert "Control the Phidgets Motor with CAN through the flipsky controller" in prompt
    assert "see if task priority works" not in prompt
    assert "already closed today" in prompt


def test_runtime_task_onboarding_rejects_recently_closed_task_choice() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_resolve_task_for_user(_user, _text, include_mission_board=False):
        return {
            "id": "868jurjm4",
            "name": "see if task priority works",
            "status": {"status": "hold"},
        }

    async def fake_list_assigned_tasks(_user, limit=8):
        return [
            {
                "id": "868jut9fb",
                "name": "Control the Phidgets Motor with CAN through the flipsky controller",
                "status": {"status": "to do"},
            },
            {
                "id": "868jurjm4",
                "name": "see if task priority works",
                "status": {"status": "hold"},
            },
        ]

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime.clickup = SimpleNamespace(
        resolve_task_for_user=fake_resolve_task_for_user,
        list_assigned_tasks=fake_list_assigned_tasks,
    )
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(user_key="andrew", session_date="2026-05-28", stage="active")
    session.metadata["recently_closed_clickup_tasks"] = [
        {
            "task_id": "868jurjm4",
            "task_name": "see if task priority works",
            "closed_at": "2026-05-28T18:41:35",
        }
    ]
    prompt = {
        "type": "task_onboarding",
        "step": "select_task",
        "source": "post_review_close",
        "draft": {},
    }
    inbound = MessageRecord(
        message_id="msg-1",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T18:42:00"),
        content="see if task priority works",
        attachments=[],
    )
    handled = asyncio.run(
        runtime._handle_task_onboarding_prompt(
            SimpleNamespace(),
            user,
            session,
            inbound,
            datetime.fromisoformat("2026-05-28T18:42:00"),
            prompt,
        )
    )
    assert handled is True
    assert prompt["step"] == "select_task"
    assert any("was just closed" in item for item in sent)


def test_runtime_auto_clocks_out_after_six_hours_of_inactivity() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    states: list[tuple[str | None, str]] = []

    async def fake_state(_session, task_id: str | None, state: str) -> None:
        states.append((task_id, state))

    runtime._safe_set_task_state = fake_state  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T09:30:00",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["clickup_time_tracking"] = {
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "started_at": "2026-05-28T09:00:00",
        "source": "local",
    }
    session.metadata["auto_clock_out_warning"] = {
        "reference_at": "2026-05-28T09:30:00",
        "warning_sent_at": "2026-05-28T15:15:00",
        "auto_clock_out_at": "2026-05-28T15:30:00",
    }
    changed = asyncio.run(
        runtime._maybe_auto_clock_out_inactive(
            None,
            user,
            session,
            datetime.fromisoformat("2026-05-28T15:31:00"),
        )
    )
    assert changed is True
    assert session.stage == "clocked_out"
    assert session.clocked_out_at == "2026-05-28T15:31:00"
    assert session.metadata["auto_clock_out_reason"] == "No inbound check-in for 6 hours."
    assert "clickup_time_tracking" not in session.metadata
    history = session.metadata["clickup_time_tracking_history"]
    assert states == [("868jun6qg", "hold")]
    assert history[0]["end_reason"] == "auto_clock_out_inactive"
    assert history[0]["duration_seconds"] == 23460


def test_runtime_auto_clock_out_accepts_legacy_naive_last_user_message_at() -> None:
    runtime = _build_runtime()
    runtime.clickup = None

    async def fake_finalize(*_args, **_kwargs):
        return None

    runtime._finalize_clickup_day = fake_finalize  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
        timezone="America/New_York",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T09:30:00",
    )
    session.metadata["auto_clock_out_warning"] = {
        "reference_at": "2026-05-28T09:30:00-04:00",
        "warning_sent_at": "2026-05-28T15:15:00-04:00",
        "auto_clock_out_at": "2026-05-28T15:30:00-04:00",
    }

    changed = asyncio.run(
        runtime._maybe_auto_clock_out_inactive(
            None,
            user,
            session,
            datetime.fromisoformat("2026-05-28T16:00:00-04:00"),
        )
    )

    assert changed is True
    assert session.clocked_out_at == "2026-05-28T16:00:00-04:00"
    assert session.metadata["auto_clock_out_reference_at"] == "2026-05-28T09:30:00-04:00"


def test_runtime_does_not_auto_clock_out_before_threshold() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T10:00:00",
    )
    changed = asyncio.run(
        runtime._maybe_auto_clock_out_inactive(
            None,
            user,
            session,
            datetime.fromisoformat("2026-05-28T15:30:00"),
        )
    )
    assert changed is False
    assert session.stage == "active"
    assert session.clocked_out_at is None


def test_runtime_does_not_auto_clock_out_during_lunch_break() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="on_lunch_break",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T09:30:00",
    )
    changed = asyncio.run(
        runtime._maybe_auto_clock_out_inactive(
            None,
            user,
            session,
            datetime.fromisoformat("2026-05-28T15:31:00"),
        )
    )
    assert changed is False
    assert session.stage == "on_lunch_break"
    assert session.clocked_out_at is None


def test_runtime_sends_auto_clock_out_warning_once_per_inactivity_window() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T10:00:00",
    )

    first = asyncio.run(
        runtime._maybe_send_auto_clock_out_warning(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T15:45:00"),
        )
    )
    second = asyncio.run(
        runtime._maybe_send_auto_clock_out_warning(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T15:45:30"),
        )
    )

    assert first is True
    assert second is False
    assert sent == [
        "Quick check-in: I have not seen an update for a while. If you are still working, send a short note about what changed or reply `still working`. If I do not hear back within 15 minutes, I will pause the timer so the record stays accurate."
    ]
    assert session.metadata["auto_clock_out_warning"]["reference_at"] == "2026-05-28T10:00:00"


def test_runtime_sends_delayed_inactivity_warning_with_actual_time_remaining() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(user_key="andrew", display_name="Andrew", discord_user_id=1)
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T10:00:00",
    )

    changed = asyncio.run(
        runtime._maybe_send_auto_clock_out_warning(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T15:52:00"),
        )
    )

    assert changed is True
    assert "within 8 minutes" in sent[0]


def test_runtime_late_scheduler_tick_starts_fresh_warning_grace_period() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)
        return None

    async def fake_finalize(*_args, **_kwargs):
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._finalize_clickup_day = fake_finalize  # type: ignore[method-assign]
    user = UserProfile(user_key="andrew", display_name="Andrew", discord_user_id=1)
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T10:00:00",
    )
    late_tick = datetime.fromisoformat("2026-05-28T16:10:00")

    warned = asyncio.run(
        runtime._maybe_send_auto_clock_out_warning(
            SimpleNamespace(),
            user,
            session,
            late_tick,
        )
    )
    clocked_out_immediately = asyncio.run(
        runtime._maybe_auto_clock_out_inactive(None, user, session, late_tick)
    )
    clocked_out_after_grace = asyncio.run(
        runtime._maybe_auto_clock_out_inactive(
            None,
            user,
            session,
            datetime.fromisoformat("2026-05-28T16:25:00"),
        )
    )

    assert warned is True
    assert "within 15 minutes" in sent[0]
    assert session.metadata["auto_clock_out_warning"]["auto_clock_out_at"] == "2026-05-28T16:25:00"
    assert clocked_out_immediately is False
    assert clocked_out_after_grace is True


def test_runtime_never_auto_clocks_out_without_matching_warning() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    user = UserProfile(user_key="andrew", display_name="Andrew", discord_user_id=1)
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T10:00:00",
    )

    changed = asyncio.run(
        runtime._maybe_auto_clock_out_inactive(
            None,
            user,
            session,
            datetime.fromisoformat("2026-05-28T16:30:00"),
        )
    )

    assert changed is False
    assert session.stage == "active"
    assert session.clocked_out_at is None


def test_runtime_auto_clock_out_warning_resets_after_new_activity() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T10:00:00",
    )

    first = asyncio.run(
        runtime._maybe_send_auto_clock_out_warning(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T15:45:00"),
        )
    )
    runtime._touch_inbound_session(session, datetime.fromisoformat("2026-05-28T15:05:00"))
    second = asyncio.run(
        runtime._maybe_send_auto_clock_out_warning(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T20:50:00"),
        )
    )

    assert first is True
    assert second is True
    assert len(sent) == 2
    assert session.metadata["auto_clock_out_warning"]["reference_at"] == "2026-05-28T15:05:00"


def test_runtime_auto_clock_out_warning_skips_ineligible_stages_but_supports_other_clocked_in_stage() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    lunch_session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="on_lunch_break",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T10:00:00",
    )
    artifacts_session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="awaiting_clock_out_artifacts",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T10:00:00",
    )
    review_session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="awaiting_admin_review",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T10:00:00",
    )

    lunch_changed = asyncio.run(
        runtime._maybe_send_auto_clock_out_warning(
            SimpleNamespace(),
            user,
            lunch_session,
            datetime.fromisoformat("2026-05-28T15:45:00"),
        )
    )
    artifacts_changed = asyncio.run(
        runtime._maybe_send_auto_clock_out_warning(
            SimpleNamespace(),
            user,
            artifacts_session,
            datetime.fromisoformat("2026-05-28T15:45:00"),
        )
    )
    review_changed = asyncio.run(
        runtime._maybe_send_auto_clock_out_warning(
            SimpleNamespace(),
            user,
            review_session,
            datetime.fromisoformat("2026-05-28T15:45:00"),
        )
    )

    assert lunch_changed is False
    assert artifacts_changed is False
    assert review_changed is True
    assert len(sent) == 1


def test_runtime_auto_clock_out_notification_is_sent_when_cutoff_hits() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    sent: list[str] = []

    async def fake_finalize(*_args, **_kwargs):
        return None

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    runtime._finalize_clickup_day = fake_finalize  # type: ignore[method-assign]
    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T09:30:00",
    )
    session.metadata["auto_clock_out_warning"] = {
        "reference_at": "2026-05-28T09:30:00",
        "warning_sent_at": "2026-05-28T15:15:00",
        "auto_clock_out_at": "2026-05-28T15:30:00",
    }

    changed = asyncio.run(
        runtime._maybe_auto_clock_out_inactive(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T15:31:00"),
        )
    )

    assert changed is True
    assert session.stage == "clocked_out"
    assert session.metadata["auto_clock_out_reason"] == "No inbound check-in for 6 hours."
    assert sent == [
        "I paused your work timer after the inactivity window so it does not keep running unattended. If you are continuing work, reply `clock in` or `resume work`. If you were working during the gap, tell me what you completed so the time can be reviewed."
    ]


def test_runtime_auto_clock_out_notification_failure_does_not_block_clock_out(caplog) -> None:
    runtime = _build_runtime()
    runtime.clickup = None

    async def fake_finalize(*_args, **_kwargs):
        return None

    async def fake_send(*_args, **_kwargs):
        raise RuntimeError("discord send failed")

    runtime._finalize_clickup_day = fake_finalize  # type: ignore[method-assign]
    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T09:30:00",
    )
    session.metadata["auto_clock_out_warning"] = {
        "reference_at": "2026-05-28T09:30:00",
        "warning_sent_at": "2026-05-28T15:15:00",
        "auto_clock_out_at": "2026-05-28T15:30:00",
    }

    with caplog.at_level(logging.ERROR):
        changed = asyncio.run(
            runtime._maybe_auto_clock_out_inactive(
                SimpleNamespace(),
                user,
                session,
                datetime.fromisoformat("2026-05-28T15:31:00"),
            )
        )

    assert changed is True
    assert session.stage == "clocked_out"
    assert session.clocked_out_at == "2026-05-28T15:31:00"
    assert "Failed to send auto clock-out notification to user andrew (Andrew)" in caplog.text


def test_runtime_auto_clock_out_warning_supports_one_hour_threshold() -> None:
    runtime = _build_runtime()
    runtime.config.schedule.auto_clock_out_after_hours = 1
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        last_user_message_at="2026-05-28T10:00:00",
    )

    changed = asyncio.run(
        runtime._maybe_send_auto_clock_out_warning(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T10:45:00"),
        )
    )

    assert changed is True
    assert len(sent) == 1
    assert "within 15 minutes" in sent[0]


def test_meal_timer_cannot_restart_before_thirty_minutes() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(user_key="alex", display_name="Alex", discord_user_id=1)
    session = SessionState(
        user_key="alex",
        session_date="2026-05-28",
        stage="on_lunch_break",
        clocked_in_at="2026-05-28T09:00:00-07:00",
        metadata={"lunch_started_at": "2026-05-28T12:00:00-07:00"},
    )

    asyncio.run(
        runtime._end_lunch_break(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T12:10:00-07:00"),
        )
    )

    assert session.stage == "on_lunch_break"
    assert session.metadata.get("lunch_ended_at") is None
    assert "20 more minutes" in sent[0]


def test_overtime_clockout_blocks_same_day_restart_until_approval() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        overtime_approval_required=True,
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-05-28",
        stage="clocked_out",
        clocked_in_at="2026-05-28T09:00:00-07:00",
        clocked_out_at="2026-05-28T17:00:00-07:00",
        metadata={"auto_clock_out_reason": "Configured overtime limit reached."},
    )

    handled = asyncio.run(
        runtime._maybe_resume_same_day_work(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T17:05:00-07:00"),
        )
    )

    assert handled is True
    assert session.stage == "clocked_out"
    assert session.clocked_out_at == "2026-05-28T17:00:00-07:00"
    assert "cannot restart work time" in sent[0]


def test_overtime_approval_notifies_worker_and_other_approver() -> None:
    runtime = _build_runtime(
        admins=[
            AdminProfile(name="Erik", discord_user_id=1),
            AdminProfile(name="George", discord_user_id=2),
        ]
    )
    worker_messages: list[str] = []
    admin_targets: list[list[str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        worker_messages.append(content)
        return None

    async def fake_admin_notice(_client, _content: str, *, target_admins=None, **_kwargs):
        admin_targets.append([admin.name for admin in (target_admins or [])])
        return admin_targets[-1]

    async def fake_persist(*_args, **_kwargs):
        return None

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._send_admin_notice = fake_admin_notice  # type: ignore[method-assign]
    runtime._persist_session_state = fake_persist  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=10,
        overtime_approval_required=True,
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-05-28",
        stage="clocked_out",
        clocked_in_at="2026-05-28T09:00:00-07:00",
        clocked_out_at="2026-05-28T17:00:00-07:00",
        metadata={"auto_clock_out_reason": "Configured overtime limit reached."},
    )

    result = asyncio.run(
        runtime.approve_same_day_overtime(
            SimpleNamespace(),
            user,
            session,
            approved_by="Erik",
            comments="Finish the thermal test.",
            now=datetime.fromisoformat("2026-05-28T17:05:00-07:00"),
        )
    )

    assert session.metadata["overtime_approved_by"] == "Erik"
    assert admin_targets == [["George"]]
    assert "may clock back in" in worker_messages[0]
    assert "Approved same-day overtime" in result


def test_recent_clickup_task_activity_defers_inactivity_clockout() -> None:
    runtime = _build_runtime()
    runtime.config.schedule.auto_clock_out_after_hours = 1
    activity_at = datetime.fromisoformat("2026-05-28T10:30:00-07:00")

    async def fake_get_task(_task_id: str):
        return {"id": "task-1", "date_updated": str(int(activity_at.timestamp() * 1000))}

    runtime.clickup.get_task = fake_get_task  # type: ignore[method-assign]
    user = UserProfile(user_key="alex", display_name="Alex", discord_user_id=1)
    session = SessionState(
        user_key="alex",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00-07:00",
        last_user_message_at="2026-05-28T10:00:00-07:00",
        metadata={"active_clickup_task_id": "task-1"},
    )

    changed = asyncio.run(
        runtime._maybe_auto_clock_out_inactive(
            None,
            user,
            session,
            datetime.fromisoformat("2026-05-28T11:01:00-07:00"),
        )
    )

    assert changed is False
    assert session.stage == "active"
    assert session.metadata["credible_clickup_activity"]["task_id"] == "task-1"


def test_runtime_clock_out_finalize_puts_task_on_hold_not_complete() -> None:
    runtime = _build_runtime()
    runtime.clickup = None
    states: list[tuple[str | None, str]] = []

    async def fake_state(_session, task_id: str | None, state: str) -> None:
        states.append((task_id, state))

    runtime._safe_set_task_state = fake_state  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="awaiting_clock_out_artifacts",
        latest_status="Finished the project tree cleanup for today.",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["clickup_time_tracking"] = {
        "task_id": "868jun6qg",
        "task_name": "formalize project tree",
        "started_at": "2026-05-28T13:00:00",
        "source": "local",
    }
    note = asyncio.run(
        runtime._finalize_clickup_day(
            user,
            session,
            datetime.fromisoformat("2026-05-28T15:00:00"),
        )
    )
    assert note is not None
    assert states == [("868jun6qg", "hold")]
    history = session.metadata["clickup_time_tracking_history"]
    assert history[0]["end_reason"] == "clock_out"


def test_runtime_finish_task_onboarding_reactivates_clocked_out_session() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="clocked_out",
        clocked_out_at="2026-05-28T18:00:00",
        awaiting_clock_out_photo=True,
        awaiting_clock_out_summary=True,
    )
    activated: list[tuple[str, str]] = []

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> TaskActivationResult:
        activated.append((task_id, task_name))
        return _activation_result(task_id, task_name)

    runtime._activate_clickup_task = fake_activate  # type: ignore[method-assign]
    prompt = {
        "type": "task_onboarding",
        "source": "admin_switch",
        "task_id": "868jurjm4",
        "task_name": "see if task priority works",
    }
    now = datetime.fromisoformat("2026-05-28T19:38:26")
    asyncio.run(runtime._finish_task_onboarding(user, session, prompt, now))
    assert session.stage == "active"
    assert session.clocked_out_at is None
    assert session.awaiting_clock_out_photo is False
    assert session.awaiting_clock_out_summary is False
    assert session.last_follow_up_at == "2026-05-28T19:38:26"
    assert activated == [("868jurjm4", "see if task priority works")]


def test_runtime_normalize_session_state_repairs_clocked_out_with_open_tracking() -> None:
    runtime = _build_runtime()
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="clocked_out",
        clocked_out_at="2026-05-28T19:32:16",
    )
    session.metadata["clickup_time_tracking"] = {
        "task_id": "868jurjm4",
        "task_name": "see if task priority works",
        "started_at": "2026-05-28T19:38:26",
        "source": "local",
    }
    changed = runtime._normalize_session_state(session)
    assert changed is True
    assert session.stage == "active"
    assert session.clocked_out_at is None
    assert "session_reactivated_at" in session.metadata


def test_runtime_normalize_session_state_clears_stale_task_onboarding_after_clock_out() -> None:
    runtime = _build_runtime()
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="clocked_out",
        clocked_out_at="2026-05-28T19:32:16",
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "step": "estimated_duration",
        "task_id": "868jurjm4",
        "task_name": "see if task priority works",
    }

    changed = runtime._normalize_session_state(session)

    assert changed is True
    assert session.stage == "clocked_out"
    assert session.clocked_out_at == "2026-05-28T19:32:16"
    assert "clickup_prompt" not in session.metadata
    assert "session_reactivated_at" not in session.metadata


def test_runtime_normalize_session_state_clears_stale_task_creation_after_clock_out() -> None:
    runtime = _build_runtime()
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="clocked_out",
        clocked_out_at="2026-05-28T19:32:16",
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_creation",
        "step": "title",
        "draft": {"parent_task_id": "parent-1"},
    }

    changed = runtime._normalize_session_state(session)

    assert changed is True
    assert session.stage == "clocked_out"
    assert "clickup_prompt" not in session.metadata


def test_runtime_stuck_alert_uses_friendly_pacific_time() -> None:
    runtime = _build_runtime()
    runtime.config.schedule.stuck_alert_after_hours = 0
    notices: list[str] = []

    async def fake_notice(_client, content: str, **_kwargs):
        notices.append(content)
        return ["George"]

    runtime._send_admin_notice = fake_notice  # type: ignore[method-assign]
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=3,
        discord_username="navin",
        storage_folder_name="NavinNagavel",
    )
    session = SessionState(
        user_key="navin",
        session_date="2026-06-04",
        stage="active",
        stuck_since="2026-06-04T13:14:38-07:00",
        latest_blocker="waiting on hardware dimensions",
    )

    alerted = asyncio.run(
        runtime._maybe_alert_admin(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-06-04T13:36:38-07:00"),
        )
    )

    assert alerted is True
    assert notices == [
        "Navin has appeared stuck since today at 1:14 PM PDT (about 22 minutes ago). "
        "Latest blocker: waiting on hardware dimensions"
    ]


def test_runtime_stuck_alert_accepts_legacy_naive_stuck_since() -> None:
    runtime = _build_runtime()
    runtime.config.schedule.stuck_alert_after_hours = 0
    notices: list[str] = []

    async def fake_notice(_client, content: str, **_kwargs):
        notices.append(content)
        return ["George"]

    runtime._send_admin_notice = fake_notice  # type: ignore[method-assign]
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=3,
        discord_username="navin",
        storage_folder_name="NavinNagavel",
    )
    session = SessionState(
        user_key="navin",
        session_date="2026-06-04",
        stage="active",
        stuck_since="2026-06-04T13:14:38",
        latest_blocker="waiting on hardware dimensions",
    )

    alerted = asyncio.run(
        runtime._maybe_alert_admin(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-06-04T13:36:38-07:00"),
        )
    )

    assert alerted is True
    assert session.stuck_alerted_at == "2026-06-04T13:36:38-07:00"
    assert notices == [
        "Navin has appeared stuck since today at 1:14 PM PDT (about 22 minutes ago). "
        "Latest blocker: waiting on hardware dimensions"
    ]


def test_runtime_clock_in_reminder_accepts_legacy_naive_prompt_timestamp() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
        timezone="America/New_York",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-05-28",
        last_clock_in_prompt_at="2026-05-28T09:00:00",
    )

    changed = asyncio.run(
        runtime._maybe_send_clock_in(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T10:05:00-04:00"),
        )
    )

    assert changed is True
    assert sent == ["reminder"]
    assert session.last_clock_in_prompt_at == "2026-05-28T10:05:00-04:00"


def test_runtime_process_inbound_event_normalizes_legacy_timestamps_before_persist() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
        timezone="America/New_York",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        last_clock_in_prompt_at="2026-05-28T09:15:00",
        work_segments=[{"clocked_in_at": "2026-05-28T09:00:00", "clocked_out_at": None}],
    )
    session.metadata["last_task_onboarding_completed_at"] = "2026-05-28T09:20:00"
    session.metadata["pending_follow_up"] = {"sent_at": "2026-05-28T09:30:00"}
    session.metadata["last_admin_review_resolution"] = {"at": "2026-05-28T09:45:00"}
    session.metadata["clickup_time_tracking"] = {
        "task_id": "868jun6qg",
        "started_at": "2026-05-28T09:00:00",
    }
    inbound = MessageRecord(
        message_id="msg-duplicate",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T10:00:00-04:00"),
        content="same message",
        attachments=[],
    )
    runtime.state_store.append_message(user.user_key, session.session_date, inbound)

    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            inbound,
            inbound.created_at,
        )
    )

    assert session.clocked_in_at == "2026-05-28T09:00:00-04:00"
    assert session.intake_completed_at == "2026-05-28T09:20:00-04:00"
    assert session.last_clock_in_prompt_at == "2026-05-28T09:15:00-04:00"
    assert session.work_segments == [{"clocked_in_at": "2026-05-28T09:00:00-04:00", "clocked_out_at": None}]
    assert session.metadata["pending_follow_up"]["sent_at"] == "2026-05-28T09:30:00-04:00"
    assert session.metadata["last_admin_review_resolution"]["at"] == "2026-05-28T09:45:00-04:00"
    assert session.metadata["clickup_time_tracking"]["started_at"] == "2026-05-28T09:00:00-04:00"


def test_runtime_write_dashboard_uses_friendly_pacific_time() -> None:
    runtime = _build_runtime()
    written: list[str] = []

    async def fake_write_dashboard(_filename: str, content: str) -> Path:
        written.append(content)
        return Path("dashboard.md")

    runtime.store.write_dashboard = fake_write_dashboard
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=3,
        discord_username="navin",
        storage_folder_name="NavinNagavel",
        timezone="America/New_York",
    )
    runtime.roster_by_key = {user.user_key: user}
    session = SessionState(
        user_key="navin",
        session_date="2026-06-04",
        stage="on_lunch_break",
        clocked_in_at="2026-06-04T09:00:00-04:00",
        metadata={"lunch_started_at": "2026-06-04T12:15:00-04:00"},
    )
    runtime.get_user_session_for_moment = lambda _user, _moment=None: (
        session,
        datetime.fromisoformat("2026-06-04T13:00:00-04:00"),
    )

    asyncio.run(InternManagementRuntime.write_dashboard(runtime))

    assert written
    content = written[0]
    assert "- Clocked in: " in content
    assert "6:00 AM PDT" in content
    assert "- On lunch break since: " in content
    assert "9:15 AM PDT" in content
    assert "T09:00:00" not in content


def test_runtime_backfill_transcripts_to_pacific_once_rewrites_existing_transcripts(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    user_dir = storage_root / "people" / "Alex"
    daily_dir = user_dir / "2026-06-08"
    daily_dir.mkdir(parents=True)
    (user_dir / "profile.json").write_text(
        json.dumps(
            {
                "user_key": "alex",
                "display_name": "Alex",
                "discord_user_id": 1,
                "discord_username": "alex",
                "storage_folder_name": "Alex",
            }
        ),
        encoding="utf-8",
    )
    (daily_dir / "session.json").write_text(
        json.dumps(
            {
                "user_key": "alex",
                "session_date": "2026-06-08",
                "stage": "active",
            }
        ),
        encoding="utf-8",
    )
    (daily_dir / "transcript.md").write_text("old transcript\n", encoding="utf-8")

    state_store = StateStore(tmp_path / "data" / "agent_state.sqlite3")
    message = MessageRecord(
        message_id="m1",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-08T16:09:32+00:00"),
        content="controller update",
        attachments=[],
    )
    state_store.append_message("alex", "2026-06-08", message)

    runtime = InternManagementRuntime.__new__(InternManagementRuntime)
    runtime.bootstrap = SimpleNamespace(
        storage_root_path=storage_root,
        state_db_path=tmp_path / "data" / "agent_state.sqlite3",
        default_timezone="America/Los_Angeles",
    )
    runtime.state_store = state_store

    rewritten = asyncio.run(InternManagementRuntime.backfill_transcripts_to_pacific_once(runtime))

    assert rewritten == 1
    content = (daily_dir / "transcript.md").read_text(encoding="utf-8")
    assert "### 2026-06-08 09:09:32 - Alex" in content
    marker = tmp_path / "data" / "transcript_pacific_backfill_v1.done"
    assert marker.exists()

    second_run = asyncio.run(InternManagementRuntime.backfill_transcripts_to_pacific_once(runtime))
    assert second_run == 0


def test_runtime_process_inbound_prioritizes_clock_out_artifacts_over_task_onboarding_prompt() -> None:
    runtime = _build_runtime()
    prompt_called = False
    artifacts_called = False

    async def fake_prompt(*_args, **_kwargs):
        nonlocal prompt_called
        prompt_called = True
        return True

    async def fake_artifacts(*_args, **_kwargs):
        nonlocal artifacts_called
        artifacts_called = True
        return None

    runtime._handle_clickup_prompt = fake_prompt  # type: ignore[method-assign]
    runtime._handle_clock_out_artifacts = fake_artifacts  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="awaiting_clock_out_artifacts",
        clocked_in_at="2026-05-28T09:00:00",
    )
    session.metadata["clickup_prompt"] = {
        "type": "task_onboarding",
        "step": "estimated_duration",
        "task_id": "868jurjm4",
        "task_name": "see if task priority works",
    }
    inbound = MessageRecord(
        message_id="msg-artifacts",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T18:16:48"),
        content="Here is what I finished today.",
        attachments=[],
    )

    asyncio.run(
        runtime.process_inbound_event(
            SimpleNamespace(),
            user,
            session,
            inbound,
            inbound.created_at,
        )
    )

    assert artifacts_called is True
    assert prompt_called is False


def test_runtime_self_lookup_hours_request_shows_chooser_and_preserves_prompt_state(tmp_path: Path) -> None:
    runtime = _build_runtime()
    storage_root = tmp_path / "storage"
    runtime.bootstrap.storage_root_path = storage_root
    sent: list[tuple[str, object | None]] = []
    post_route_called = False

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        nonlocal sent
        sent.append((content, view))
        _session.last_outbound_at = _now.isoformat()
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    async def fake_post_route(*_args, **_kwargs) -> None:
        nonlocal post_route_called
        post_route_called = True

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._apply_post_route_clickup_automation = fake_post_route  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
        timezone="America/Los_Angeles",
    )
    _write_archived_session(
        storage_root,
        user,
        SessionState(
            user_key="alex",
            session_date="2026-06-08",
            stage="clocked_out",
            clocked_in_at="2026-06-08T09:00:00-07:00",
            clocked_out_at="2026-06-08T11:00:00-07:00",
            work_segments=[{"clocked_in_at": "2026-06-08T09:00:00-07:00", "clocked_out_at": "2026-06-08T11:00:00-07:00"}],
            metadata={
                "clickup_time_tracking_history": [
                    {
                        "task_id": "task-a",
                        "task_name": "Morning setup",
                        "started_at": "2026-06-08T09:30:00-07:00",
                        "closed_at": "2026-06-08T10:30:00-07:00",
                        "duration_seconds": 3600,
                    }
                ]
            },
        ),
    )
    _write_archived_session(
        storage_root,
        user,
        SessionState(
            user_key="alex",
            session_date="2026-06-09",
            stage="clocked_out",
            clocked_in_at="2026-06-09T13:00:00-07:00",
            clocked_out_at="2026-06-09T16:00:00-07:00",
            work_segments=[{"clocked_in_at": "2026-06-09T13:00:00-07:00", "clocked_out_at": "2026-06-09T16:00:00-07:00"}],
            metadata={
                "clickup_time_tracking_history": [
                    {
                        "task_id": "task-b",
                        "task_name": "Controller wiring",
                        "started_at": "2026-06-09T13:30:00-07:00",
                        "closed_at": "2026-06-09T15:00:00-07:00",
                        "duration_seconds": 5400,
                    }
                ]
            },
        ),
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        clocked_in_at="2026-06-10T09:00:00-07:00",
        latest_status="existing status",
        pending_clickup_sync=False,
        work_segments=[{"clocked_in_at": "2026-06-10T09:00:00-07:00", "clocked_out_at": None}],
        metadata={
            "clickup_prompt": {
                "type": "task_onboarding",
                "step": "select_task",
            },
            "clickup_time_tracking": {
                "task_id": "task-c",
                "task_name": "Lever automation",
                "started_at": "2026-06-10T09:30:00-07:00",
            }
        },
    )
    inbound = MessageRecord(
        message_id="msg-hours",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T11:30:00-07:00"),
        content="how many hours have i clocked in this week",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert post_route_called is False
    assert session.latest_status == "existing status"
    assert session.pending_clickup_sync is False
    assert session.metadata["clickup_prompt"]["step"] == "select_task"
    assert session.metadata["self_lookup_prompt"]["step"] == "chooser"
    assert sent
    assert "Do you want to know about your hours or your status?" in sent[0][0]
    view = sent[0][1]
    assert view is not None
    assert [item.label for item in view.children] == ["No", "Hours", "Status"]


def test_runtime_self_lookup_status_request_shows_chooser_and_preserves_clock_out_requirements(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    sent: list[tuple[str, object | None]] = []
    artifacts_called = False

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        sent.append((content, view))
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    async def fake_artifacts(*_args, **_kwargs) -> None:
        nonlocal artifacts_called
        artifacts_called = True

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._handle_clock_out_artifacts = fake_artifacts  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
        timezone="America/Los_Angeles",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="awaiting_clock_out_artifacts",
        clocked_in_at="2026-06-10T09:00:00-07:00",
        latest_status="wrap-up still pending",
        awaiting_clock_out_photo=True,
        awaiting_clock_out_summary=True,
        work_segments=[{"clocked_in_at": "2026-06-10T09:00:00-07:00", "clocked_out_at": None}],
        metadata={
            "clickup_time_tracking": {
                "task_id": "task-c",
                "task_name": "Lever automation",
                "started_at": "2026-06-10T09:30:00-07:00",
            }
        },
    )
    inbound = MessageRecord(
        message_id="msg-clock-out-hours",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T10:00:00-07:00"),
        content="show my status",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert artifacts_called is False
    assert session.stage == "awaiting_clock_out_artifacts"
    assert session.awaiting_clock_out_photo is True
    assert session.awaiting_clock_out_summary is True
    assert session.latest_status == "wrap-up still pending"
    assert session.metadata["self_lookup_prompt"]["step"] == "chooser"
    assert sent
    assert "Do you want to know about your hours or your status?" in sent[0][0]
    view = sent[0][1]
    assert view is not None
    assert [item.label for item in view.children] == ["No", "Hours", "Status"]


def test_runtime_self_lookup_generic_hours_request_works_while_waiting_for_admin_review(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    sent: list[tuple[str, object | None]] = []
    post_route_called = False

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        sent.append((content, view))
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    async def fake_post_route(*_args, **_kwargs) -> None:
        nonlocal post_route_called
        post_route_called = True

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._apply_post_route_clickup_automation = fake_post_route  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="awaiting_admin_review",
        latest_status="waiting on closeout",
        metadata={
            "pending_admin_reviews": [
                {"task_id": "task-a", "task_name": "Firmware"}
            ]
        },
    )
    inbound = MessageRecord(
        message_id="msg-review-hours",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T16:00:00-07:00"),
        content="how many hours do i have",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert post_route_called is False
    assert session.stage == "awaiting_admin_review"
    assert session.latest_status == "waiting on closeout"
    assert session.metadata["self_lookup_prompt"]["step"] == "chooser"
    assert sent
    assert "Do you want to know about your hours or your status?" in sent[0][0]
    assert "currently waiting on admin review" not in sent[0][0]


def test_runtime_not_working_today_request_shows_confirmation_prompt_and_preserves_state(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    sent: list[tuple[str, object | None]] = []
    post_route_called = False
    runtime.interface_intelligence.resolve_daily_availability_intent = (  # type: ignore[attr-defined]
        lambda _text, **_kwargs: asyncio.sleep(
            0,
            result=SimpleNamespace(action="not_working_today", confidence=0.95),
        )
    )

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        sent.append((content, view))
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    async def fake_post_route(*_args, **_kwargs) -> None:
        nonlocal post_route_called
        post_route_called = True

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._apply_post_route_clickup_automation = fake_post_route  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="awaiting_admin_review",
        latest_status="waiting on closeout",
        metadata={
            "pending_admin_reviews": [
                {"task_id": "task-a", "task_name": "Firmware"}
            ]
        },
    )
    inbound = MessageRecord(
        message_id="msg-off-today",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T08:05:00-07:00"),
        content="I am out today and not coming in",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert post_route_called is False
    assert session.stage == "awaiting_admin_review"
    assert session.latest_status == "waiting on closeout"
    assert session.metadata["day_suppression_prompt"]["type"] == "day_suppression_confirmation"
    assert session.metadata.get("day_suppression") is None
    assert sent
    assert "It sounds like you may not be working today." in sent[0][0]
    view = sent[0][1]
    assert view is not None
    assert [item.label for item in view.children] == ["Yes, pause today", "No, keep messages on"]


def test_runtime_self_lookup_clocked_in_wording_works_while_waiting_for_admin_review(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    sent: list[tuple[str, object | None]] = []
    post_route_called = False

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        sent.append((content, view))
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    async def fake_post_route(*_args, **_kwargs) -> None:
        nonlocal post_route_called
        post_route_called = True

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._apply_post_route_clickup_automation = fake_post_route  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="awaiting_admin_review",
        latest_status="waiting on closeout",
        metadata={
            "pending_admin_reviews": [
                {"task_id": "task-a", "task_name": "Firmware"}
            ]
        },
    )
    inbound = MessageRecord(
        message_id="msg-review-clocked-hours",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T16:05:00-07:00"),
        content="how many hours do i have clocked in",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert post_route_called is False
    assert session.stage == "awaiting_admin_review"
    assert session.latest_status == "waiting on closeout"
    assert session.metadata["self_lookup_prompt"]["step"] == "chooser"
    assert sent
    assert "Do you want to know about your hours or your status?" in sent[0][0]
    assert "currently waiting on admin review" not in sent[0][0]


def test_runtime_self_lookup_generic_hours_request_works_while_clocked_out(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    sent: list[tuple[str, object | None]] = []
    post_route_called = False

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        sent.append((content, view))
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    async def fake_post_route(*_args, **_kwargs) -> None:
        nonlocal post_route_called
        post_route_called = True

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._apply_post_route_clickup_automation = fake_post_route  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
        timezone="America/Los_Angeles",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="clocked_out",
        clocked_in_at="2026-06-10T09:00:00-07:00",
        clocked_out_at="2026-06-10T17:00:00-07:00",
        latest_status="done for today",
        work_segments=[{"clocked_in_at": "2026-06-10T09:00:00-07:00", "clocked_out_at": "2026-06-10T17:00:00-07:00"}],
    )
    inbound = MessageRecord(
        message_id="msg-clocked-out-hours",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T18:00:00-07:00"),
        content="how many hours do i have",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert post_route_called is False
    assert session.stage == "clocked_out"
    assert session.clocked_in_at == "2026-06-10T09:00:00-07:00"
    assert session.clocked_out_at == "2026-06-10T17:00:00-07:00"
    assert session.latest_status == "done for today"
    assert session.metadata["self_lookup_prompt"]["step"] == "chooser"
    assert sent
    assert "Do you want to know about your hours or your status?" in sent[0][0]


def test_runtime_self_lookup_show_my_hours_triggers_chooser(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    sent: list[tuple[str, object | None]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        sent.append((content, view))
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        latest_status="working on firmware",
    )
    inbound = MessageRecord(
        message_id="msg-show-hours",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T14:00:00-07:00"),
        content="show my hours",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert session.metadata["self_lookup_prompt"]["step"] == "chooser"
    assert sent
    assert "Do you want to know about your hours or your status?" in sent[0][0]


def test_runtime_self_lookup_how_many_hours_am_i_at_triggers_chooser_and_clears_follow_up_tracking(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    sent: list[tuple[str, object | None]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        sent.append((content, view))
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        intake_completed_at="2026-06-10T09:15:00-07:00",
        last_follow_up_at="2026-06-10T10:00:00-07:00",
        metadata={
            "pending_follow_up": {
                "message_id": "follow-up-1",
                "question_text": "What changed?",
                "sent_at": "2026-06-10T10:00:00-07:00",
                "awaiting_reply": True,
            },
            "follow_up_response_aggregation": {
                "follow_up_message_id": "follow-up-1",
                "question_text": "What changed?",
                "reply_fragments": ["still working"],
                "reply_message_ids": ["msg-progress"],
                "last_reply_at": "2026-06-10T11:55:00-07:00",
            },
        },
    )
    inbound = MessageRecord(
        message_id="msg-hours-at",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T12:00:00-07:00"),
        content="How many hours am I at",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert session.metadata["self_lookup_prompt"]["step"] == "chooser"
    assert "pending_follow_up" not in session.metadata
    assert "follow_up_response_aggregation" not in session.metadata
    assert sent
    assert "Do you want to know about your hours or your status?" in sent[0][0]

    follow_up_sent = asyncio.run(
        runtime._maybe_send_follow_up(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-06-10T12:01:00-07:00"),
        )
    )

    assert follow_up_sent is False
    assert len(sent) == 1


def test_runtime_self_lookup_request_closes_active_progress_probe(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    sent: list[tuple[str, object | None]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        sent.append((content, view))
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        metadata={
            "clickup_prompt": {
                "type": "progress_probe",
                "probe_id": "probe-1",
                "question_text": "What changed?",
                "original_reply_message_ids": ["msg-progress"],
                "original_reply_text": "still working",
                "probe_exchange": [],
                "subscribed_admin_ids": [],
            },
            "pending_follow_up": {
                "message_id": "follow-up-1",
                "question_text": "What changed?",
                "sent_at": "2026-06-10T10:00:00-07:00",
            },
            "follow_up_response_aggregation": {
                "follow_up_message_id": "follow-up-1",
                "question_text": "What changed?",
                "reply_fragments": ["still working"],
                "reply_message_ids": ["msg-progress"],
                "last_reply_at": "2026-06-10T11:59:00-07:00",
            },
        },
    )
    inbound = MessageRecord(
        message_id="msg-hours-during-probe",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T12:00:00-07:00"),
        content="How many hours am I at",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert session.metadata["self_lookup_prompt"]["step"] == "chooser"
    assert "clickup_prompt" not in session.metadata
    assert "pending_follow_up" not in session.metadata
    assert "follow_up_response_aggregation" not in session.metadata
    assert session.metadata["progress_probe_history"][-1]["closure_reason"] == "interrupted_by_self_lookup"
    assert sent
    assert "Do you want to know about your hours or your status?" in sent[0][0]


def test_runtime_self_lookup_time_tracking_question_triggers_chooser(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    sent: list[tuple[str, object | None]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        sent.append((content, view))
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="awaiting_admin_review",
        latest_status="working through review",
        metadata={
            "pending_admin_reviews": [
                {"task_id": "task-a", "task_name": "Firmware"}
            ]
        },
    )
    inbound = MessageRecord(
        message_id="msg-time-tracking",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T14:30:00-07:00"),
        content="Can you show me my time tracking for this week?",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert session.stage == "awaiting_admin_review"
    assert session.metadata["self_lookup_prompt"]["step"] == "chooser"
    assert sent
    assert "Do you want to know about your hours or your status?" in sent[0][0]


def test_runtime_self_lookup_how_long_clocked_in_question_triggers_chooser(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    sent: list[tuple[str, object | None]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        sent.append((content, view))
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        latest_status="working on firmware",
        clocked_in_at="2026-06-10T09:00:00-07:00",
    )
    inbound = MessageRecord(
        message_id="msg-how-long-clocked",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T14:45:00-07:00"),
        content="How long have I been clocked in?",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert session.metadata["self_lookup_prompt"]["step"] == "chooser"
    assert sent
    assert "Do you want to know about your hours or your status?" in sent[0][0]


def test_runtime_self_lookup_request_does_not_trigger_on_status_update_text(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        latest_status="standing by",
        clocked_in_at="2026-06-10T09:00:00-07:00",
    )
    inbound = MessageRecord(
        message_id="msg-zero-hours",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T09:00:00-07:00"),
        content="Here is my status update: still wiring the controller.",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert session.metadata.get("self_lookup_prompt") is None
    assert not any("Do you want to know about your hours or your status?" in message for message in sent)
    assert session.latest_status == "Here is my status update: still wiring the controller."


def test_runtime_self_lookup_request_does_not_trigger_on_progress_update_that_mentions_hours(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        latest_status="standing by",
        clocked_in_at="2026-06-10T09:00:00-07:00",
    )
    inbound = MessageRecord(
        message_id="msg-hours-progress",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T13:00:00-07:00"),
        content="I worked 2 hours on firmware today.",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert session.metadata.get("self_lookup_prompt") is None
    assert not any("Do you want to know about your hours or your status?" in message for message in sent)
    assert session.latest_status == "I worked 2 hours on firmware today."


def test_runtime_self_lookup_request_does_not_trigger_on_permission_question_about_future_hours(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None):
        del view
        sent.append(content)
        return MessageRecord(
            message_id=f"bot-{len(sent)}",
            direction="outbound",
            author_id=0,
            created_at=_now,
            content=content,
            attachments=[],
        )

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        latest_status="standing by",
        clocked_in_at="2026-06-10T09:00:00-07:00",
    )
    inbound = MessageRecord(
        message_id="msg-extra-hours",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-06-10T13:30:00-07:00"),
        content="Can I work extra hours today?",
        attachments=[],
    )

    asyncio.run(runtime.process_inbound_event(SimpleNamespace(), user, session, inbound, inbound.created_at))

    assert session.metadata.get("self_lookup_prompt") is None
    assert not any("Do you want to know about your hours or your status?" in message for message in sent)
    assert session.latest_status == "Can I work extra hours today?"


def test_runtime_weekly_hours_request_uses_effective_workday_date_before_rollover(tmp_path: Path) -> None:
    runtime = _build_runtime()
    storage_root = tmp_path / "storage"
    runtime.bootstrap.storage_root_path = storage_root
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
        timezone="America/Los_Angeles",
    )
    _write_archived_session(
        storage_root,
        user,
        SessionState(
            user_key="alex",
            session_date="2026-06-08",
            stage="clocked_out",
            work_segments=[{"clocked_in_at": "2026-06-08T09:00:00-07:00", "clocked_out_at": "2026-06-08T10:00:00-07:00"}],
            metadata={
                "clickup_time_tracking_history": [
                    {
                        "task_id": "task-a",
                        "task_name": "Monday work",
                        "started_at": "2026-06-08T09:00:00-07:00",
                        "closed_at": "2026-06-08T10:00:00-07:00",
                        "duration_seconds": 3600,
                    }
                ]
            },
        ),
    )
    _write_archived_session(
        storage_root,
        user,
        SessionState(
            user_key="alex",
            session_date="2026-06-09",
            stage="clocked_out",
            work_segments=[{"clocked_in_at": "2026-06-09T09:00:00-07:00", "clocked_out_at": "2026-06-09T11:00:00-07:00"}],
            metadata={
                "clickup_time_tracking_history": [
                    {
                        "task_id": "task-b",
                        "task_name": "Tuesday work",
                        "started_at": "2026-06-09T09:00:00-07:00",
                        "closed_at": "2026-06-09T11:00:00-07:00",
                        "duration_seconds": 7200,
                    }
                ]
            },
        ),
    )
    _write_archived_session(
        storage_root,
        user,
        SessionState(
            user_key="alex",
            session_date="2026-06-10",
            stage="clocked_out",
            work_segments=[{"clocked_in_at": "2026-06-10T09:00:00-07:00", "clocked_out_at": "2026-06-10T12:00:00-07:00"}],
            metadata={
                "clickup_time_tracking_history": [
                    {
                        "task_id": "task-c",
                        "task_name": "Wednesday work",
                        "started_at": "2026-06-10T09:00:00-07:00",
                        "closed_at": "2026-06-10T12:00:00-07:00",
                        "duration_seconds": 10800,
                    }
                ]
            },
        ),
    )

    reply = asyncio.run(
        runtime._build_self_hours_reply(
            user,
            SessionState(
                user_key="alex",
                session_date="2026-06-09",
                stage="active",
                work_segments=[{"clocked_in_at": "2026-06-09T09:00:00-07:00", "clocked_out_at": "2026-06-09T11:00:00-07:00"}],
                metadata={
                    "clickup_time_tracking_history": [
                        {
                            "task_id": "task-b",
                            "task_name": "Tuesday work",
                            "started_at": "2026-06-09T09:00:00-07:00",
                            "closed_at": "2026-06-09T11:00:00-07:00",
                            "duration_seconds": 7200,
                        }
                    ]
                },
            ),
            datetime.fromisoformat("2026-06-10T02:15:00-07:00"),
        )
    )

    assert "This Week so far (2026-06-08 to 2026-06-09):" in reply
    assert "Clocked-in time: 3h" in reply
    assert "Task-tracked time: 3h" in reply
    assert "2026-06-10" not in reply


class _FakeInteractionResponse:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.views: list[object | None] = []
        self.sent: list[str] = []
        self.deferred = 0
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def edit_message(self, *, content=None, view=None):
        self.edits.append(content or "")
        self.views.append(view)
        self._done = True

    async def send_message(self, content: str):
        self.sent.append(content)
        self._done = True

    async def defer(self) -> None:
        self.deferred += 1
        self._done = True


class _FakeInteractionMessage:
    def __init__(self, message_id: str, *, fail_delete: bool = False) -> None:
        self.id = message_id
        self.fail_delete = fail_delete
        self.deleted = False
        self.edits: list[str] = []
        self.views: list[object | None] = []

    async def delete(self) -> None:
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.deleted = True

    async def edit(self, *, content=None, view=None):
        self.edits.append(content or "")
        self.views.append(view)


def test_runtime_task_confirmation_interaction_is_saved_in_session_history() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    runtime.roster_by_key[user.user_key] = user
    runtime.roster_by_discord_id[user.discord_user_id] = user
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="awaiting_task_selection",
        metadata={
            "clickup_prompt": {
                "type": "task_onboarding",
                "step": "confirm_task",
                "task_id": "868jun6qg",
                "task_name": "formalize project tree",
            }
        },
    )
    runtime.state_store.save_session(session)

    async def fake_handle_task_prompt(_client, _user, current_session, inbound, _now, _prompt):
        current_session.stage = "active"
        current_session.latest_status = f"confirmed via {inbound.content}"

    runtime._handle_task_onboarding_prompt = fake_handle_task_prompt  # type: ignore[method-assign]
    response = _FakeInteractionResponse()
    interaction = SimpleNamespace(
        id="interaction-task-confirm",
        user=SimpleNamespace(id=1),
        created_at=datetime.fromisoformat("2026-06-10T10:10:00-07:00"),
        client=SimpleNamespace(),
        response=response,
        followup=SimpleNamespace(send=lambda _content: asyncio.sleep(0)),
    )

    asyncio.run(
        runtime.handle_task_onboarding_confirmation_interaction(
            interaction,
            user.user_key,
            "confirm",
        )
    )

    messages = runtime.state_store.list_messages(user.user_key, session.session_date)
    assert [message.message_id for message in messages] == [
        "interaction:interaction-task-confirm",
        "interaction-edit:interaction-task-confirm",
    ]
    assert messages[0].content == "yes"
    assert messages[1].content == "Recorded."
    assert response.edits == ["Recorded."]


def test_runtime_self_lookup_hours_button_opens_range_picker(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    runtime.roster_by_key[user.user_key] = user
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        metadata={
            "self_lookup_prompt": {
                "type": "self_lookup",
                "step": "chooser",
                "message_id": "menu-1",
            }
        },
    )
    runtime.state_store.save_session(session)

    async def fake_followup_send(_content: str) -> None:
        return None

    response = _FakeInteractionResponse()
    interaction = SimpleNamespace(
        id="interaction-1",
        user=SimpleNamespace(id=1),
        created_at=datetime.fromisoformat("2026-06-10T10:00:00-07:00"),
        client=SimpleNamespace(),
        message=_FakeInteractionMessage("menu-1"),
        response=response,
        followup=SimpleNamespace(send=fake_followup_send),
    )

    asyncio.run(
        runtime.handle_self_lookup_interaction(
            interaction,
            user.user_key,
            session.session_date,
            "hours",
        )
    )

    assert session.metadata["self_lookup_prompt"]["step"] == "range"
    assert response.deferred == 1
    assert interaction.message.edits[-1] == "What range of hours do you want to know?"
    view = interaction.message.views[-1]
    assert view is not None
    assert [item.label for item in view.children] == ["Today", "This Week", "Last Week", "Whole Summer"]


def test_runtime_self_lookup_status_button_replies_with_compact_summary(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
        timezone="America/Los_Angeles",
    )
    runtime.roster_by_key[user.user_key] = user
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        clocked_in_at="2026-06-10T09:00:00-07:00",
        latest_status="still wiring the controller",
        latest_blocker="waiting on connector",
        metadata={
            "self_lookup_prompt": {
                "type": "self_lookup",
                "step": "chooser",
                "message_id": "menu-2",
            },
            "active_clickup_task_name": "Lever automation",
        },
    )
    runtime.state_store.save_session(session)

    async def fake_followup_send(_content: str) -> None:
        return None

    response = _FakeInteractionResponse()
    interaction = SimpleNamespace(
        id="interaction-2",
        user=SimpleNamespace(id=1),
        created_at=datetime.fromisoformat("2026-06-10T10:05:00-07:00"),
        client=SimpleNamespace(),
        message=_FakeInteractionMessage("menu-2"),
        response=response,
        followup=SimpleNamespace(send=fake_followup_send),
    )

    asyncio.run(
        runtime.handle_self_lookup_interaction(
            interaction,
            user.user_key,
            session.session_date,
            "status",
        )
    )

    assert session.metadata.get("self_lookup_prompt") is None
    assert response.deferred == 1
    assert "Here is your current status:" in interaction.message.edits[-1]
    assert "- Stage: active" in interaction.message.edits[-1]
    assert "- Active task: Lever automation" in interaction.message.edits[-1]
    assert "- Latest status: still wiring the controller" in interaction.message.edits[-1]
    assert "- Latest blocker: waiting on connector" in interaction.message.edits[-1]


def test_runtime_self_lookup_interaction_reply_is_saved_in_session_history(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
        timezone="America/Los_Angeles",
    )
    runtime.roster_by_key[user.user_key] = user
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        clocked_in_at="2026-06-10T09:00:00-07:00",
        latest_status="still wiring the controller",
        metadata={
            "self_lookup_prompt": {
                "type": "self_lookup",
                "step": "chooser",
                "message_id": "menu-history",
            },
            "active_clickup_task_name": "Lever automation",
        },
    )
    runtime.state_store.save_session(session)

    async def fake_followup_send(_content: str) -> None:
        return None

    response = _FakeInteractionResponse()
    interaction = SimpleNamespace(
        id="interaction-history",
        user=SimpleNamespace(id=1),
        created_at=datetime.fromisoformat("2026-06-10T10:05:00-07:00"),
        client=SimpleNamespace(),
        message=_FakeInteractionMessage("menu-history"),
        response=response,
        followup=SimpleNamespace(send=fake_followup_send),
    )

    asyncio.run(
        runtime.handle_self_lookup_interaction(
            interaction,
            user.user_key,
            session.session_date,
            "status",
        )
    )

    messages = runtime.state_store.list_messages(user.user_key, session.session_date)
    assert [message.message_id for message in messages] == [
        "interaction:interaction-history",
        "interaction-edit:interaction-history",
    ]
    assert messages[1].content.startswith("Here is your current status:")
    assert session.last_outbound_at == "2026-06-10T10:05:00-07:00"
    transcript = build_transcript_markdown(user, session, messages)
    assert "### 2026-06-10 10:05:00 - Agent" in transcript
    assert "Here is your current status:" in transcript


def test_runtime_self_lookup_range_button_returns_hours_and_preserves_clock_out_requirements(tmp_path: Path) -> None:
    runtime = _build_runtime()
    storage_root = tmp_path / "storage"
    runtime.bootstrap.storage_root_path = storage_root
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
        timezone="America/Los_Angeles",
    )
    runtime.roster_by_key[user.user_key] = user
    _write_archived_session(
        storage_root,
        user,
        SessionState(
            user_key="alex",
            session_date="2026-06-09",
            stage="clocked_out",
            work_segments=[{"clocked_in_at": "2026-06-09T09:00:00-07:00", "clocked_out_at": "2026-06-09T11:00:00-07:00"}],
            metadata={
                "clickup_time_tracking_history": [
                    {
                        "task_id": "task-b",
                        "task_name": "Controller wiring",
                        "started_at": "2026-06-09T09:30:00-07:00",
                        "closed_at": "2026-06-09T10:30:00-07:00",
                        "duration_seconds": 3600,
                    }
                ]
            },
        ),
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="awaiting_clock_out_artifacts",
        clocked_in_at="2026-06-10T09:00:00-07:00",
        awaiting_clock_out_photo=True,
        awaiting_clock_out_summary=True,
        work_segments=[{"clocked_in_at": "2026-06-10T09:00:00-07:00", "clocked_out_at": None}],
        metadata={
            "self_lookup_prompt": {
                "type": "self_lookup",
                "step": "range",
                "message_id": "menu-3",
            },
            "clickup_time_tracking": {
                "task_id": "task-c",
                "task_name": "Lever automation",
                "started_at": "2026-06-10T09:30:00-07:00",
            },
        },
    )
    runtime.state_store.save_session(session)

    async def fake_followup_send(_content: str) -> None:
        return None

    response = _FakeInteractionResponse()
    interaction = SimpleNamespace(
        id="interaction-3",
        user=SimpleNamespace(id=1),
        created_at=datetime.fromisoformat("2026-06-10T10:00:00-07:00"),
        client=SimpleNamespace(),
        message=_FakeInteractionMessage("menu-3"),
        response=response,
        followup=SimpleNamespace(send=fake_followup_send),
    )

    asyncio.run(
        runtime.handle_self_lookup_interaction(
            interaction,
            user.user_key,
            session.session_date,
            "this_week",
        )
    )

    assert session.metadata.get("self_lookup_prompt") is None
    assert session.stage == "awaiting_clock_out_artifacts"
    assert session.awaiting_clock_out_photo is True
    assert session.awaiting_clock_out_summary is True
    assert response.deferred == 1
    assert "This Week so far (2026-06-08 to 2026-06-10):" in interaction.message.edits[-1]
    assert "Clocked-in time: 3h" in interaction.message.edits[-1]
    assert "Task-tracked time: 1h 30m" in interaction.message.edits[-1]
    assert "I still need the picture and the written wrap-up before I close out today." in interaction.message.edits[-1]


def test_runtime_whole_summer_button_is_acknowledged_before_hours_are_built(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=1,
        discord_username="navin",
        storage_folder_name="Navin",
        timezone="America/Los_Angeles",
    )
    runtime.roster_by_key[user.user_key] = user
    session = SessionState(
        user_key="navin",
        session_date="2026-07-24",
        stage="active",
        metadata={
            "self_lookup_prompt": {
                "type": "self_lookup",
                "step": "range",
                "message_id": "summer-menu",
            }
        },
    )
    runtime.state_store.save_session(session)
    response = _FakeInteractionResponse()
    interaction = SimpleNamespace(
        id="interaction-whole-summer",
        user=SimpleNamespace(id=1),
        created_at=datetime.fromisoformat("2026-07-24T15:34:00-07:00"),
        client=SimpleNamespace(),
        message=_FakeInteractionMessage("summer-menu"),
        response=response,
        followup=SimpleNamespace(send=lambda _content: asyncio.sleep(0)),
    )
    build_started = asyncio.Event()
    release_build = asyncio.Event()

    async def fake_build_hours(_user, _session, _now, range_key: str) -> str:
        assert range_key == "whole_summer"
        build_started.set()
        await release_build.wait()
        return "Whole Summer (2026-06-01 to 2026-07-24):\n- Clocked-in time: 120h"

    runtime._build_self_hours_reply_for_range = fake_build_hours  # type: ignore[method-assign]

    async def run_test() -> None:
        task = asyncio.create_task(
            runtime.handle_self_lookup_interaction(
                interaction,
                user.user_key,
                session.session_date,
                "whole_summer",
            )
        )
        await build_started.wait()
        assert response.deferred == 1
        release_build.set()
        await task

    asyncio.run(run_test())

    assert interaction.message.edits[-1].startswith("Whole Summer")


def test_runtime_self_lookup_dismiss_button_deletes_or_collapses_prompt(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    runtime.roster_by_key[user.user_key] = user

    async def fake_followup_send(_content: str) -> None:
        return None

    delete_response = _FakeInteractionResponse()
    delete_message = _FakeInteractionMessage("menu-4")
    delete_session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        metadata={
            "self_lookup_prompt": {
                "type": "self_lookup",
                "step": "chooser",
                "message_id": "menu-4",
            }
        },
    )
    runtime.state_store.save_session(delete_session)
    delete_interaction = SimpleNamespace(
        id="interaction-4",
        user=SimpleNamespace(id=1),
        created_at=datetime.fromisoformat("2026-06-10T10:10:00-07:00"),
        client=SimpleNamespace(),
        message=delete_message,
        response=delete_response,
        followup=SimpleNamespace(send=fake_followup_send),
    )

    asyncio.run(
        runtime.handle_self_lookup_interaction(
            delete_interaction,
            user.user_key,
            delete_session.session_date,
            "dismiss",
        )
    )

    assert delete_session.metadata.get("self_lookup_prompt") is None
    assert delete_message.deleted is True
    assert delete_response.deferred == 1

    collapse_response = _FakeInteractionResponse()
    collapse_message = _FakeInteractionMessage("menu-5", fail_delete=True)
    collapse_session = SessionState(
        user_key="alex",
        session_date="2026-06-11",
        metadata={
            "self_lookup_prompt": {
                "type": "self_lookup",
                "step": "chooser",
                "message_id": "menu-5",
            }
        },
    )
    runtime.state_store.save_session(collapse_session)
    collapse_interaction = SimpleNamespace(
        id="interaction-5",
        user=SimpleNamespace(id=1),
        created_at=datetime.fromisoformat("2026-06-11T10:10:00-07:00"),
        client=SimpleNamespace(),
        message=collapse_message,
        response=collapse_response,
        followup=SimpleNamespace(send=fake_followup_send),
    )

    asyncio.run(
        runtime.handle_self_lookup_interaction(
            collapse_interaction,
            user.user_key,
            collapse_session.session_date,
            "dismiss",
        )
    )

    assert collapse_session.metadata.get("self_lookup_prompt") is None
    assert collapse_message.deleted is False
    assert collapse_response.deferred == 1
    assert collapse_message.edits[-1] == "Okay, dismissed."


def test_runtime_self_lookup_view_restricts_other_users() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    view = runtime._self_lookup_chooser_view(user, "2026-06-10")

    async def fake_followup_send(_content: str) -> None:
        return None

    response = _FakeInteractionResponse()
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=999),
        response=response,
        followup=SimpleNamespace(send=fake_followup_send),
    )

    allowed = asyncio.run(view.interaction_check(interaction))

    assert allowed is False
    assert response.sent == ["This info prompt is not for you."]


def test_runtime_day_suppression_view_restricts_other_users() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    view = runtime._day_suppression_confirmation_view(user, "2026-06-10")

    async def fake_followup_send(_content: str) -> None:
        return None

    response = _FakeInteractionResponse()
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=999),
        response=response,
        followup=SimpleNamespace(send=fake_followup_send),
    )

    allowed = asyncio.run(view.interaction_check(interaction))

    assert allowed is False
    assert response.sent == ["This schedule pause prompt is not for you."]


def test_runtime_day_suppression_confirm_sets_state_and_scheduler_skips_messages(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    runtime.roster_by_key[user.user_key] = user
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="awaiting_clock_in",
        metadata={
            "day_suppression_prompt": {
                "type": "day_suppression_confirmation",
                "message_id": "pause-1",
                "source_message_id": "msg-off-today",
                "source_excerpt": "I am out today",
            }
        },
    )
    runtime.state_store.save_session(session)

    async def fake_followup_send(_content: str) -> None:
        return None

    response = _FakeInteractionResponse()
    interaction = SimpleNamespace(
        id="interaction-pause-confirm",
        user=SimpleNamespace(id=1),
        created_at=datetime.fromisoformat("2026-06-10T08:06:00-07:00"),
        client=SimpleNamespace(),
        message=_FakeInteractionMessage("pause-1"),
        response=response,
        followup=SimpleNamespace(send=fake_followup_send),
    )

    asyncio.run(
        runtime.handle_day_suppression_interaction(
            interaction,
            user.user_key,
            session.session_date,
            "confirm",
        )
    )

    send_clock_in_called = False
    task_onboarding_called = False
    follow_up_called = False
    lunch_called = False

    async def fake_send_clock_in(*_args, **_kwargs):
        nonlocal send_clock_in_called
        send_clock_in_called = True
        return True

    async def fake_prompt_task_onboarding(*_args, **_kwargs):
        nonlocal task_onboarding_called
        task_onboarding_called = True
        return True

    async def fake_send_follow_up(*_args, **_kwargs):
        nonlocal follow_up_called
        follow_up_called = True
        return True

    async def fake_lunch_check_in(*_args, **_kwargs):
        nonlocal lunch_called
        lunch_called = True
        return True

    runtime._maybe_send_clock_in = fake_send_clock_in  # type: ignore[method-assign]
    runtime._maybe_prompt_task_onboarding = fake_prompt_task_onboarding  # type: ignore[method-assign]
    runtime._maybe_send_follow_up = fake_send_follow_up  # type: ignore[method-assign]
    runtime._maybe_send_lunch_break_check_in = fake_lunch_check_in  # type: ignore[method-assign]

    asyncio.run(
        runtime._run_scheduler_for_user(
            SimpleNamespace(),
            user,
            datetime.fromisoformat("2026-06-10T12:30:00-07:00"),
        )
    )

    stored = runtime.state_store.get_session(user.user_key, session.session_date)
    assert response.edits[-1] == "Okay, I will stop reminders and check-ins for the rest of today."
    assert stored.metadata.get("day_suppression_prompt") is None
    assert stored.metadata["day_suppression"]["session_date"] == session.session_date
    assert send_clock_in_called is False
    assert task_onboarding_called is False
    assert follow_up_called is False
    assert lunch_called is False


def test_runtime_day_suppression_cancel_keeps_messages_enabled(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    runtime.roster_by_key[user.user_key] = user
    session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="awaiting_clock_in",
        metadata={
            "day_suppression_prompt": {
                "type": "day_suppression_confirmation",
                "message_id": "pause-2",
            }
        },
    )
    runtime.state_store.save_session(session)

    async def fake_followup_send(_content: str) -> None:
        return None

    response = _FakeInteractionResponse()
    interaction = SimpleNamespace(
        id="interaction-pause-cancel",
        user=SimpleNamespace(id=1),
        created_at=datetime.fromisoformat("2026-06-10T08:06:30-07:00"),
        client=SimpleNamespace(),
        message=_FakeInteractionMessage("pause-2"),
        response=response,
        followup=SimpleNamespace(send=fake_followup_send),
    )

    asyncio.run(
        runtime.handle_day_suppression_interaction(
            interaction,
            user.user_key,
            session.session_date,
            "cancel",
        )
    )

    stored = runtime.state_store.get_session(user.user_key, session.session_date)
    assert stored.metadata.get("day_suppression_prompt") is None
    assert stored.metadata.get("day_suppression") is None
    assert response.edits[-1] == "Okay, I will keep today's normal reminders on."


def test_runtime_self_lookup_stale_interaction_returns_not_active(tmp_path: Path) -> None:
    runtime = _build_runtime()
    runtime.bootstrap.storage_root_path = tmp_path / "storage"
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    runtime.roster_by_key[user.user_key] = user
    session = SessionState(user_key="alex", session_date="2026-06-10", stage="active")
    runtime.state_store.save_session(session)

    async def fake_followup_send(_content: str) -> None:
        return None

    response = _FakeInteractionResponse()
    interaction = SimpleNamespace(
        id="interaction-6",
        user=SimpleNamespace(id=1),
        created_at=datetime.fromisoformat("2026-06-10T10:15:00-07:00"),
        client=SimpleNamespace(),
        message=_FakeInteractionMessage("menu-missing"),
        response=response,
        followup=SimpleNamespace(send=fake_followup_send),
    )

    asyncio.run(
        runtime.handle_self_lookup_interaction(
            interaction,
            user.user_key,
            session.session_date,
            "hours",
        )
    )

    assert response.deferred == 1
    assert interaction.message.edits[-1] == "That info request is no longer active."


def test_runtime_self_lookup_hours_ranges_cover_today_last_week_whole_summer_and_zero(tmp_path: Path) -> None:
    runtime = _build_runtime()
    storage_root = tmp_path / "storage"
    runtime.bootstrap.storage_root_path = storage_root
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
        timezone="America/Los_Angeles",
    )
    _write_archived_session(
        storage_root,
        user,
        SessionState(
            user_key="alex",
            session_date="2026-06-01",
            stage="clocked_out",
            work_segments=[{"clocked_in_at": "2026-06-01T09:00:00-07:00", "clocked_out_at": "2026-06-01T11:00:00-07:00"}],
            metadata={
                "clickup_time_tracking_history": [
                    {
                        "task_id": "task-a",
                        "task_name": "Monday work",
                        "started_at": "2026-06-01T09:00:00-07:00",
                        "closed_at": "2026-06-01T10:00:00-07:00",
                        "duration_seconds": 3600,
                    }
                ]
            },
        ),
    )
    _write_archived_session(
        storage_root,
        user,
        SessionState(
            user_key="alex",
            session_date="2026-06-03",
            stage="clocked_out",
            work_segments=[{"clocked_in_at": "2026-06-03T09:00:00-07:00", "clocked_out_at": "2026-06-03T12:00:00-07:00"}],
            metadata={
                "clickup_time_tracking_history": [
                    {
                        "task_id": "task-b",
                        "task_name": "Wednesday work",
                        "started_at": "2026-06-03T09:00:00-07:00",
                        "closed_at": "2026-06-03T11:00:00-07:00",
                        "duration_seconds": 7200,
                    }
                ]
            },
        ),
    )
    _write_archived_session(
        storage_root,
        user,
        SessionState(
            user_key="alex",
            session_date="2026-06-08",
            stage="clocked_out",
            work_segments=[{"clocked_in_at": "2026-06-08T09:00:00-07:00", "clocked_out_at": "2026-06-08T10:00:00-07:00"}],
            metadata={
                "clickup_time_tracking_history": [
                    {
                        "task_id": "task-c",
                        "task_name": "This week work",
                        "started_at": "2026-06-08T09:00:00-07:00",
                        "closed_at": "2026-06-08T09:30:00-07:00",
                        "duration_seconds": 1800,
                    }
                ]
            },
        ),
    )
    current_session = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        clocked_in_at="2026-06-10T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-10T09:00:00-07:00", "clocked_out_at": None}],
        metadata={
            "clickup_time_tracking": {
                "task_id": "task-d",
                "task_name": "Current work",
                "started_at": "2026-06-10T10:00:00-07:00",
            }
        },
    )
    reference_now = datetime.fromisoformat("2026-06-10T11:00:00-07:00")

    today_reply = asyncio.run(
        runtime._build_self_hours_reply_for_range(
            user,
            current_session,
            reference_now,
            "today",
        )
    )
    last_week_reply = asyncio.run(
        runtime._build_self_hours_reply_for_range(
            user,
            current_session,
            reference_now,
            "last_week",
        )
    )
    whole_summer_reply = asyncio.run(
        runtime._build_self_hours_reply_for_range(
            user,
            current_session,
            reference_now,
            "whole_summer",
        )
    )
    zero_reply = asyncio.run(
        runtime._build_self_hours_reply_for_range(
            UserProfile(
                user_key="zoe",
                display_name="Zoe",
                discord_user_id=2,
                discord_username="zoe",
                storage_folder_name="ZoeExample",
                timezone="America/Los_Angeles",
            ),
            SessionState(user_key="zoe", session_date="2026-06-10", stage="active"),
            reference_now,
            "last_week",
        )
    )

    assert "Today (2026-06-10):" in today_reply
    assert "Clocked-in time: 2h" in today_reply
    assert "Task-tracked time: 1h" in today_reply
    assert "Last Week (2026-06-01 to 2026-06-07):" in last_week_reply
    assert "Clocked-in time: 5h" in last_week_reply
    assert "Task-tracked time: 3h" in last_week_reply
    assert "Whole Summer (2026-06-01 to 2026-06-10):" in whole_summer_reply
    assert "Clocked-in time: 8h" in whole_summer_reply
    assert "Task-tracked time: 4h 30m" in whole_summer_reply
    assert "Days with logged time: 4" in whole_summer_reply
    assert "Daily breakdown:" not in whole_summer_reply
    assert "I do not have any logged time for you from last week." in zero_reply


def test_runtime_whole_summer_hours_reply_stays_within_discord_message_limit() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=1,
        discord_username="navin",
        storage_folder_name="Navin",
        timezone="America/Los_Angeles",
    )
    session = SessionState(user_key="navin", session_date="2026-08-31", stage="active")
    first_day = datetime.fromisoformat("2026-06-01T00:00:00-07:00")
    rows = [
        {
            "user_key": user.user_key,
            "session_date": (first_day + timedelta(days=offset)).date().isoformat(),
            "clocked_in_total_seconds": 8 * 60 * 60,
            "task_tracked_total_seconds": 7 * 60 * 60,
        }
        for offset in range(92)
    ]

    reply = runtime._build_self_hours_reply_for_range_from_rows(
        user,
        session,
        rows,
        datetime.fromisoformat("2026-08-31T17:00:00-07:00"),
        "whole_summer",
    )

    assert len(reply) <= 2_000
    assert "Days with logged time: 92" in reply
    assert "Clocked-in time: 736h" in reply
    assert "Task-tracked time: 644h" in reply
    assert "Daily breakdown:" not in reply


def test_runtime_write_time_tracking_csv_includes_archived_sessions_and_clamps_stale_open_time(tmp_path: Path) -> None:
    runtime = _build_runtime()
    storage_root = tmp_path / "storage"
    runtime.bootstrap.storage_root_path = storage_root
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
        timezone="America/Los_Angeles",
    )
    session_path = _write_archived_session(
        storage_root,
        user,
        SessionState(
            user_key="alex",
            session_date="2026-06-08",
            stage="active",
            clocked_in_at="2026-06-08T09:00:00",
            last_contact_at="2026-06-08T10:15:00",
            work_segments=[{"clocked_in_at": "2026-06-08T09:00:00", "clocked_out_at": None}],
            metadata={
                "clickup_time_tracking": {
                    "task_id": "task-open",
                    "task_name": "Open wiring",
                    "started_at": "2026-06-08T09:30:00",
                }
            },
        ),
    )

    asyncio.run(runtime._write_time_tracking_csv(now=datetime.fromisoformat("2026-06-14T12:00:00-07:00")))

    report_path = storage_root / "dashboard" / "time_tracking" / "time_tracking.csv"
    dashboard_path = storage_root / "dashboard" / "time_tracking" / "time_tracking_dashboard.html"
    assert report_path.exists()
    assert dashboard_path.exists()
    with report_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    row = rows[0]
    assert row["user_key"] == "alex"
    assert row["clocked_in_total_seconds"] == "4500"
    assert row["clocked_in_total_human"] == "1h 15m"
    assert row["task_tracked_total_seconds"] == "2700"
    assert row["task_tracked_total_human"] == "45m"
    assert row["has_open_work_segment"] == "True"
    assert row["active_task_timer_running"] == "True"
    assert row["review_status"] == "likely_wrong"
    assert "open work segment" in row["review_summary"].lower()
    assert json.loads(row["time_by_task_json"])[0]["seconds"] == 2700
    assert any("open work segment" in reason.lower() for reason in json.loads(row["review_reasons_json"]))
    assert row["session_path"] == str(session_path.resolve())
    dashboard_html = dashboard_path.read_text(encoding="utf-8")
    assert "Hours Rollup" in dashboard_html
    assert "Backfill Audit" in dashboard_html
    assert 'id="intern-filter"' in dashboard_html
    assert 'id="audit-run-filter"' in dashboard_html


def test_runtime_persist_session_state_rebuilds_time_tracking_csv_with_current_session_override(tmp_path: Path) -> None:
    runtime = _build_runtime()
    storage_root = tmp_path / "storage"
    runtime.bootstrap.storage_root_path = storage_root
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="AlexExample",
    )
    previous = SessionState(user_key="alex", session_date="2026-06-10", stage="awaiting_clock_in")
    current = SessionState(
        user_key="alex",
        session_date="2026-06-10",
        stage="active",
        clocked_in_at="2026-06-10T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-10T09:00:00-07:00", "clocked_out_at": "2026-06-10T10:00:00-07:00"}],
    )

    asyncio.run(
        runtime._persist_session_state(
            user,
            current,
            now=datetime.fromisoformat("2026-06-10T10:00:00-07:00"),
            previous_session=previous,
            trigger="unit_test",
            details={"source": "time_tracking_report"},
        )
    )

    report_path = storage_root / "dashboard" / "time_tracking" / "time_tracking.csv"
    assert report_path.exists()
    with report_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["session_date"] == "2026-06-10"
    assert rows[0]["clocked_in_total_seconds"] == "3600"
    assert rows[0]["review_status"] == "in_progress"


def test_runtime_time_tracking_review_flags_long_and_missing_lunch_returns() -> None:
    runtime = _build_runtime()
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        timezone="America/Los_Angeles",
    )
    long_lunch = SessionState(
        user_key="alex",
        session_date="2026-07-23",
        stage="clocked_out",
        clocked_in_at="2026-07-23T09:00:00-07:00",
        clocked_out_at="2026-07-23T17:00:00-07:00",
        work_segments=[
            {
                "clocked_in_at": "2026-07-23T09:00:00-07:00",
                "clocked_out_at": "2026-07-23T17:00:00-07:00",
            }
        ],
        metadata={
            "lunch_started_at": "2026-07-23T12:00:00-07:00",
            "lunch_ended_at": "2026-07-23T13:45:00-07:00",
        },
    )
    runtime._refresh_session_time_summary(
        long_lunch,
        datetime.fromisoformat("2026-07-23T17:00:00-07:00"),
    )

    long_review = runtime._build_time_tracking_review_snapshot(
        user,
        long_lunch,
        now=datetime.fromisoformat("2026-07-28T12:00:00-07:00"),
    )

    assert long_review["status"] == "needs_review"
    assert any("unusually long" in reason.lower() for reason in long_review["reasons"])

    missing_return = runtime._clone_session_state(long_lunch)
    missing_return.metadata.pop("lunch_ended_at")
    missing_review = runtime._build_time_tracking_review_snapshot(
        user,
        missing_return,
        now=datetime.fromisoformat("2026-07-28T12:00:00-07:00"),
    )

    assert missing_review["status"] == "likely_wrong"
    assert any("no recorded return" in reason.lower() for reason in missing_review["reasons"])


def test_runtime_flush_clickup_preserves_confirmed_active_task_when_inferred_task_differs() -> None:
    runtime = _build_runtime()
    posted_task_ids: list[str] = []
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        intake_completed_at="2026-05-28T09:15:00",
        last_user_message_at="2026-05-28T09:50:00",
        pending_clickup_sync=True,
    )
    session.metadata["active_clickup_task_id"] = "868jun6qh"
    session.metadata["active_clickup_task_name"] = "secondary cleanup"
    session.metadata["clickup_selection_reason"] = "Confirmed by intern during task onboarding."
    session.metadata["clickup_time_tracking"] = {
        "task_id": "868jun6qh",
        "task_name": "secondary cleanup",
        "started_at": "2026-05-28T09:20:00",
        "source": "local",
    }
    runtime.state_store.append_message(
        user.user_key,
        session.session_date,
        MessageRecord(
            message_id="msg-progress",
            direction="inbound",
            author_id=1,
            created_at=datetime.fromisoformat("2026-05-28T09:50:00"),
            content="Finished the cleanup pass and moving into validation.",
            attachments=[],
        ),
    )

    async def fake_get_context(_user, _session, _messages, **_kwargs) -> ClickUpContextBundle:
        return ClickUpContextBundle(
            context="Active ClickUp task candidate:\n- formalize project tree | id=868jun6qg | status=in progress | priority=high",
            active_task_id="868jun6qg",
            active_task_name="formalize project tree",
            selection_reason="status=in progress; priority=high",
            candidate_task_ids=["868jun6qg", "868jun6qh"],
        )

    async def fake_post_update(
        _user,
        task_id: str | None,
        _comment_text: str,
        _summary: str,
        _timestamp_ms: int,
        *,
        blocker_text=None,
    ) -> str | None:
        del blocker_text
        posted_task_ids.append(str(task_id))
        return task_id

    runtime._get_clickup_context = fake_get_context  # type: ignore[method-assign]
    runtime.clickup.post_update = fake_post_update  # type: ignore[attr-defined]
    runtime.advisor = SimpleNamespace(
        summarize_updates=lambda *_args, **_kwargs: asyncio.sleep(0, result="Concrete progress update.")
    )

    changed = asyncio.run(
        runtime._flush_clickup_for_session(
            user,
            session,
            datetime.fromisoformat("2026-05-28T10:20:00"),
            force=False,
        )
    )

    assert changed is True
    assert posted_task_ids == ["868jun6qh"]
    assert session.metadata["active_clickup_task_id"] == "868jun6qh"
    assert session.metadata["active_clickup_task_name"] == "secondary cleanup"
    assert session.metadata["clickup_selection_reason"] == "Confirmed by intern during task onboarding."
    assert session.metadata["clickup_candidate_task_ids"] == ["868jun6qg", "868jun6qh"]


def test_runtime_missing_active_task_prompt_self_heals_from_running_timer() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00",
        intake_completed_at="2026-05-28T09:15:00",
    )
    session.metadata["active_clickup_task_id"] = "868jun6qg"
    session.metadata["active_clickup_task_name"] = "formalize project tree"
    session.metadata["clickup_selection_reason"] = "status=in progress; priority=high"
    session.metadata["clickup_time_tracking"] = {
        "task_id": "868jun6qh",
        "task_name": "secondary cleanup",
        "started_at": "2026-05-28T09:20:00",
        "source": "local",
    }

    changed = asyncio.run(
        runtime._maybe_prompt_task_onboarding(
            SimpleNamespace(),
            user,
            session,
            datetime.fromisoformat("2026-05-28T10:00:00"),
        )
    )

    assert changed is False
    assert sent == []
    assert session.metadata["active_clickup_task_id"] == "868jun6qh"
    assert session.metadata["active_clickup_task_name"] == "secondary cleanup"
    assert session.metadata["clickup_selection_reason"] == (
        "Recovered from the running task timer after active task metadata drifted."
    )


def test_runtime_scheduler_keeps_confirmed_task_after_passive_flush() -> None:
    runtime = _build_runtime()
    sent: list[str] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime, *, view=None) -> None:
        del view
        sent.append(content)

    async def fake_false(*_args, **_kwargs) -> bool:
        return False

    async def fake_get_context(_user, _session, _messages, **_kwargs) -> ClickUpContextBundle:
        return ClickUpContextBundle(
            context="Active ClickUp task candidate:\n- formalize project tree | id=868jun6qg | status=in progress | priority=high",
            active_task_id="868jun6qg",
            active_task_name="formalize project tree",
            selection_reason="status=in progress; priority=high",
            candidate_task_ids=["868jun6qg", "868jun6qh"],
        )

    async def fake_post_update(
        _user,
        task_id: str | None,
        _comment_text: str,
        _summary: str,
        _timestamp_ms: int,
        *,
        blocker_text=None,
    ) -> str | None:
        del blocker_text
        return task_id

    runtime._send_dm = fake_send  # type: ignore[method-assign]
    runtime._maybe_send_follow_up = fake_false  # type: ignore[method-assign]
    runtime._maybe_alert_admin = fake_false  # type: ignore[method-assign]
    runtime._maybe_send_lunch_break_check_in = fake_false  # type: ignore[method-assign]
    runtime._maybe_assess_pending_follow_up_probe = fake_false  # type: ignore[method-assign]
    runtime._maybe_timeout_progress_probe = fake_false  # type: ignore[method-assign]
    runtime._get_clickup_context = fake_get_context  # type: ignore[method-assign]
    runtime.clickup.post_update = fake_post_update  # type: ignore[attr-defined]
    runtime.advisor = SimpleNamespace(
        summarize_updates=lambda *_args, **_kwargs: asyncio.sleep(0, result="Concrete progress update.")
    )

    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
        timezone="America/Los_Angeles",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        stage="active",
        clocked_in_at="2026-05-28T09:00:00-07:00",
        intake_completed_at="2026-05-28T09:15:00-07:00",
        last_user_message_at="2026-05-28T09:50:00-07:00",
        last_follow_up_at="2026-05-28T09:50:00-07:00",
        pending_clickup_sync=True,
    )
    session.metadata["active_clickup_task_id"] = "868jun6qh"
    session.metadata["active_clickup_task_name"] = "secondary cleanup"
    session.metadata["clickup_selection_reason"] = "Confirmed by intern during task onboarding."
    session.metadata["clickup_time_tracking"] = {
        "task_id": "868jun6qh",
        "task_name": "secondary cleanup",
        "started_at": "2026-05-28T09:20:00-07:00",
        "source": "local",
    }
    runtime.state_store.save_session(session)
    runtime.state_store.append_message(
        user.user_key,
        session.session_date,
        MessageRecord(
            message_id="msg-progress",
            direction="inbound",
            author_id=1,
            created_at=datetime.fromisoformat("2026-05-28T09:50:00-07:00"),
            content="Finished the cleanup pass and moving into validation.",
            attachments=[],
        ),
    )

    first_tick = datetime.fromisoformat("2026-05-28T10:20:00-07:00")
    second_tick = first_tick + timedelta(minutes=1)

    asyncio.run(runtime._run_scheduler_for_user(SimpleNamespace(), user, first_tick))
    asyncio.run(runtime._run_scheduler_for_user(SimpleNamespace(), user, second_tick))

    assert sent == []
    assert session.metadata["active_clickup_task_id"] == "868jun6qh"
    assert session.metadata["active_clickup_task_name"] == "secondary cleanup"
    assert session.metadata["clickup_selection_reason"] == "Confirmed by intern during task onboarding."
    assert "clickup_prompt" not in session.metadata

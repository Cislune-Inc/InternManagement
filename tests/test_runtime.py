from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import asyncio

from agent.models import AdminProfile, AgentConfig, AttachmentRecord, ClickUpConfig, MessageRecord, PromptConfig, ScheduleConfig, SessionState, UserProfile
from agent.runtime import InternManagementRuntime


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
            },
            {
                "id": "868jun6qh",
                "name": "secondary cleanup",
                "status": {"status": "to do"},
                "priority": {"priority": "normal"},
                "date_created": "200",
            }
        ]

    def pick_highest_priority_task(self, tasks):
        return tasks[0] if tasks else None

    async def resolve_clickup_user_id(self, user):
        return "87438366"

    async def comment_on_task(self, task_id: str, comment_text: str):
        return None

    async def get_task(self, task_id: str):
        for task in await self.list_assigned_tasks(None):
            if task["id"] == task_id:
                return {
                    **task,
                    "description": "Break the project into clear deliverables and align the folder structure.",
                }
        return {
            "id": task_id,
            "name": "unknown",
            "status": {"status": "to do"},
            "description": "",
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
    runtime.roster_by_key = {}
    runtime._config_loaded_at = None
    runtime.state_store = SimpleNamespace(save_session=lambda _session: None, list_messages=lambda *_args, **_kwargs: [])

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

    runtime._archive_session = fake_archive  # type: ignore[method-assign]
    runtime.write_dashboard = fake_dashboard  # type: ignore[method-assign]
    runtime.refresh_configuration = fake_dashboard  # type: ignore[method-assign]
    runtime.advisor = SimpleNamespace(plan_feedback=fake_plan_feedback)
    return runtime


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


def test_finish_task_onboarding_sets_intake_completed_at_when_missing() -> None:
    runtime = _build_runtime()
    activated: list[tuple[str, str]] = []

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> None:
        activated.append((task_id, task_name))

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
                content="yes",
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
    assert any("resumed task tracking" in item.lower() for item in sent)


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


def test_runtime_task_onboarding_accepts_recommended_reply_for_daily_clock_in() -> None:
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
    assert prompt["task_id"] == "868jun6qg"
    assert prompt["step"] == "plan"
    assert session.stage == "awaiting_plan"
    assert any("formalize project tree" in item for item in sent)


def test_runtime_task_onboarding_collects_interactive_plan_before_photo() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    activated: list[tuple[str, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> None:
        activated.append((task_id, task_name))

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

    async def fake_admin_notice(_client, user, _session, _now: datetime) -> None:
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
    review = session.metadata["pending_admin_review"]
    assert review["task_id"] == "868jun6qg"
    assert review["completion_photo_paths"] == [str(image_path)]
    assert admin_notices == ["Andrew"]
    assert any("waiting on admin review" in item for item in sent)
    image_path.unlink(missing_ok=True)


def test_runtime_completion_without_photo_requests_review_image() -> None:
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
    assert prompt["type"] == "task_review_submission"
    assert prompt["summary"] == "I finished formalize project tree."
    assert prompt["needs_summary"] is False
    assert prompt["needs_photo"] is True
    assert any("need a picture" in item for item in sent)


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
    assert prompt["type"] == "task_review_submission"
    assert prompt["summary"] == ""
    assert prompt["needs_summary"] is True
    assert prompt["needs_photo"] is True
    assert any("short summary" in item and "picture" in item for item in sent)


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
    assert prompt["type"] == "stuck_assistance"
    assert any("George" in item for item in sent)
    assert any("unblocker task" in item for item in sent)


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
        "type": "stuck_assistance",
        "step": "offer",
        "draft": {"blocker_text": "controller issue"},
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
        runtime._handle_stuck_assistance_prompt(
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
    assert appended == [
        ("andrew", "2026-05-28", 999, "Test multi-admin fanout"),
        ("andrew", "2026-05-28", 1000, "Test multi-admin fanout"),
    ]


def test_runtime_recovered_signal_resumes_task_and_tracking() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    activated: list[tuple[str, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> None:
        activated.append((task_id, task_name))

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
    assert any("back to `in progress`" in item for item in sent)


def test_runtime_auto_clock_out_activity_resumes_same_day_and_processes_stuck_message() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    activated: list[tuple[str, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
        sent.append(content)

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> None:
        activated.append((task_id, task_name))

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

    assert session.stage == "active"
    assert session.clocked_out_at is None
    assert "auto_clock_out_at" not in session.metadata
    assert activated == [("868jun6qg", "formalize project tree")]
    assert session.latest_status == "im blocked again"
    assert session.latest_blocker == "im blocked again"
    assert any("clocked back in and resumed" in item.lower() for item in sent)
    assert any("specific admin help" in item.lower() for item in sent)


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

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> None:
        activated.append((task_id, task_name))

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
        "type": "stuck_assistance",
        "step": "offer",
        "draft": {"blocker_text": "waiting on pinout"},
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


def test_runtime_resolves_admin_unblocker_assignee_from_clickup_member_lookup() -> None:
    runtime = _build_runtime()

    async def fake_resolve_workspace_member_id(*, name=None, email=None):
        assert name == "George"
        return "198031927"

    runtime.clickup = SimpleNamespace(resolve_workspace_member_id=fake_resolve_workspace_member_id)
    label, assignee_id, note = asyncio.run(runtime._resolve_unblocker_assignee("George"))
    assert label == "George"
    assert assignee_id == "198031927"
    assert note is None


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

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> None:
        activated.append((task_id, task_name))

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
    assert result == "Resumed `formalize project tree` and moved it back to in progress."
    assert session.stuck_since is None
    assert session.latest_blocker is None
    assert activated == [("868jun6qg", "formalize project tree")]
    assert any("resumed task tracking" in item for item in sent)


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


def test_runtime_resolve_admin_review_rework_starts_same_task_onboarding() -> None:
    runtime = _build_runtime()
    sent: list[str] = []
    states: list[tuple[str | None, str]] = []

    async def fake_send(_client, _user, _session, content: str, _now: datetime) -> None:
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
    changed = asyncio.run(
        runtime._maybe_auto_clock_out_inactive(
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
            user,
            session,
            datetime.fromisoformat("2026-05-28T15:31:00"),
        )
    )
    assert changed is False
    assert session.stage == "on_lunch_break"
    assert session.clocked_out_at is None


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

    async def fake_activate(_user, _session, _now, task_id: str, task_name: str) -> None:
        activated.append((task_id, task_name))

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

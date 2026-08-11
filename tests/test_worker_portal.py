from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from agent.models import AdminProfile, MessageRecord, SlackConfig, UserProfile
from agent.state_store import StateStore
from agent.worker_portal import (
    WorkerPortalService,
    build_worker_portal_link,
    validate_meaningful_work_detail,
    validate_work_commitment,
    validate_worker_portal_token,
)


class _FakeSlack:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def post_message(self, channel_id: str, message: str):
        self.messages.append((channel_id, message))
        return {"ok": True, "channel": channel_id, "ts": "1.0"}


class _FakeClickUp:
    def __init__(self) -> None:
        self.assignee_updates: list[tuple[str, list[str]]] = []

    async def list_assigned_tasks(self, _user, limit=35):
        return [
            {
                "id": "assigned",
                "name": "Build test fixture",
                "status": {"status": "in progress"},
                "priority": {"priority": "high"},
                "space": {"name": "Hardware"},
                "list": {"name": "Flight fixture"},
            }
        ][:limit]

    async def list_workspace_tasks(self, limit=100, include_closed=False):
        assert include_closed is False
        assert limit == 500
        return [
            {
                "id": f"workspace-{index}",
                "name": f"Workspace task {index}",
                "status": {"status": "to do"},
                "priority": {"priority": "normal"},
                "space": {"name": "Operations" if index % 2 else "NASA"},
                "folder": {"name": "Internal" if index % 2 else "NASA Award"},
                "list": {"name": "Open work"},
                "assignees": [{"id": 456, "username": "George"}] if index == 1 else [],
                "due_date": str(int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp() * 1000))
                if index == 0
                else None,
            }
            for index in range(12)
        ]

    async def update_task_assignees(self, task_id, *, add_user_ids=None, remove_user_ids=None):
        assert remove_user_ids is None
        self.assignee_updates.append((task_id, list(add_user_ids or [])))
        return {"id": task_id}


class _FakePortalIntelligence:
    enabled = True
    model = "test-model"

    def __init__(self) -> None:
        self.plan_calls: list[dict] = []
        self.checkpoint_calls: list[dict] = []

    def snapshot(self, _slack_user_id: str) -> dict:
        return {
            "enabled": True,
            "request_available": True,
            "model": self.model,
            "daily_token_budget": 100_000,
            "tokens_used_today": 321,
            "tokens_remaining_today": 99_679,
            "worker_calls_today": 2,
            "worker_calls_remaining_today": 28,
        }

    async def coach_plan(self, **kwargs) -> dict:
        self.plan_calls.append(kwargs)
        return {
            "ready_to_use": False,
            "outcome": "A mounted test fixture with the alignment checked and review photos attached",
            "first_step": "Measure the fixture mounting points and mark the bracket hole centers",
            "evidence": "Photos of the mounted fixture and the recorded alignment measurements",
            "estimate": "2 hours",
            "checkpoint": "60 minutes",
            "coaching_note": "This gives the work a visible finish line.",
            "follow_up_question": "Which interface will you use?",
        }

    async def coach_checkpoint(self, **kwargs) -> dict:
        self.checkpoint_calls.append(kwargs)
        return {
            "ready_to_save": False,
            "progress": "Mounted the fixture and verified that all four fasteners seat correctly",
            "evidence": "Four mounting photos and the completed fit-check notes",
            "next_step": "Run the alignment measurement and attach the result to the task",
            "blocker": "",
            "coaching_note": "The update now separates completed work from the next action.",
            "follow_up_question": "Can you add another detail?",
        }


def _runtime(tmp_path):
    admin = AdminProfile(
        name="Erik",
        discord_user_id=999,
        slack_user_id="UERIK",
        clickup_user_id="123",
    )
    slack = _FakeSlack()
    runtime = SimpleNamespace(
        state_store=StateStore(tmp_path / "state.sqlite3"),
        config=SimpleNamespace(
            slack=SlackConfig(
                manager_queue_url="http://192.168.4.87:8765/exceptions",
                worker_portal_beta_slack_user_ids=["UERIK", "UAJ"],
            )
        ),
        clickup=_FakeClickUp(),
        slack=slack,
        admin_profile_by_slack_user_id=lambda slack_user_id: admin if slack_user_id == "UERIK" else None,
        roster_by_slack_id={},
    )
    return runtime, admin, slack


def _token_from_link(link: str) -> str:
    return parse_qs(urlsplit(link).query)["token"][0]


def test_signed_portal_link_is_vpn_scoped_and_expires(tmp_path) -> None:
    runtime, admin, _slack = _runtime(tmp_path)
    now = datetime(2026, 8, 7, 10, tzinfo=timezone.utc)

    link = build_worker_portal_link(runtime, admin, now=now, ttl_hours=2)
    token = _token_from_link(link)

    assert link.startswith("http://192.168.4.87:8765/portal?token=")
    assert validate_worker_portal_token(runtime, token, now=now) == "UERIK"
    with pytest.raises(ValueError, match="expired"):
        validate_worker_portal_token(runtime, token, now=now + timedelta(hours=3))


def test_worker_portal_remains_limited_to_configured_beta_testers(tmp_path) -> None:
    runtime, _admin, _slack = _runtime(tmp_path)
    george = AdminProfile(name="George", discord_user_id=998, slack_user_id="UGEORGE")

    with pytest.raises(ValueError, match="configured beta testers"):
        build_worker_portal_link(runtime, george)


def test_roster_worker_can_use_portal_with_existing_schedule_and_focus(tmp_path) -> None:
    runtime, _admin, _slack = _runtime(tmp_path)
    aj = UserProfile(
        user_key="AJ",
        display_name="AJ Torres",
        slack_user_id="UAJ",
        clickup_user_id="456",
        weekly_target_hours=24,
        regular_workdays=["monday", "wednesday", "friday"],
        typical_start_time="10:00",
        typical_end_time="18:00",
        planned_time_off=["2026-08-14"],
        interests=["Lockheed Bagworm", "LM_Nightjar", "shop organization"],
        skills=["test planning"],
    )
    runtime.roster_by_slack_id["UAJ"] = aj

    token = _token_from_link(build_worker_portal_link(runtime, aj))
    payload = asyncio.run(WorkerPortalService(runtime).build_payload(token))

    assert validate_worker_portal_token(runtime, token) == "UAJ"
    assert payload["actor"] == {"name": "AJ Torres", "slack_user_id": "UAJ"}
    assert payload["profile"]["weekly_target_hours"] == 24
    assert payload["profile"]["regular_workdays"] == ["monday", "wednesday", "friday"]
    assert payload["profile"]["planned_time_off"] == ["2026-08-14"]
    assert payload["profile"]["interests"] == [
        "Lockheed Bagworm",
        "LM_Nightjar",
        "shop organization",
    ]


def test_work_commitment_rejects_vague_and_repeated_answers() -> None:
    issues, fingerprint = validate_work_commitment("make progress", "continue")
    assert len(issues) == 2

    issues, _ = validate_work_commitment(
        "A tested mounting bracket revision with the corrected hole pattern attached",
        "Open revision three and update the sketch constraints from the new measurements",
        previous_fingerprint=fingerprint,
    )
    assert issues == []

    repeated, _ = validate_work_commitment("make progress", "continue", previous_fingerprint=fingerprint)
    assert any("same answer" in issue for issue in repeated)

    copied, _ = validate_meaningful_work_detail(
        "Completed the bracket measurement and uploaded the revised drawing for review",
        purpose="progress update",
        recent_details=[
            "Completed the bracket measurement and uploaded the revised drawing for review"
        ],
    )
    assert copied and "copies a previous submission" in copied


def test_worker_portal_limits_task_options_and_keeps_beta_state_isolated(tmp_path) -> None:
    runtime, admin, _slack = _runtime(tmp_path)
    token = _token_from_link(build_worker_portal_link(runtime, admin))
    service = WorkerPortalService(runtime)

    initial = asyncio.run(service.build_payload(token))

    assert initial["beta"] is True
    assert len(initial["task_options"]) == 5
    assert initial["task_options"][0]["id"] == "assigned"
    assert initial["task_total"] == 13
    assert sum(group["task_count"] for group in initial["task_catalog"]) == 13
    assert {group["name"] for group in initial["task_catalog"]} == {
        "General / overhead",
        "Internal",
        "NASA Award",
    }
    catalog_tasks = [
        task
        for contract in initial["task_catalog"]
        for space in contract["spaces"]
        for task_list in space["lists"]
        for task in task_list["tasks"]
    ]
    due_task = next(task for task in catalog_tasks if task["id"] == "workspace-0")
    other_owned_task = next(task for task in catalog_tasks if task["id"] == "workspace-1")
    assert due_task["due_date"]
    assert "due" in due_task["reason"]
    assert other_owned_task["assignees"] == ["George"]
    assert initial["work"]["status"] == "ready"
    assert len(initial["overhead_lanes"]) == 8


def test_worker_portal_ai_coauthors_but_does_not_start_or_save_work(tmp_path) -> None:
    runtime, admin, _slack = _runtime(tmp_path)
    intelligence = _FakePortalIntelligence()
    runtime.portal_intelligence = intelligence
    token = _token_from_link(build_worker_portal_link(runtime, admin))
    service = WorkerPortalService(runtime)

    asyncio.run(
        service.apply_action(
            token,
            {
                "action": "select_task",
                "task_id": "assigned",
                "task_name": "Build test fixture",
                "task_location": "Hardware / Flight fixture",
            },
        )
    )
    drafted = asyncio.run(
        service.apply_action(
            token,
            {
                "action": "coach_plan",
                "intent": "Mount the fixture, make sure it fits, and leave photos for review.",
                "estimate": "1 hour",
                "checkpoint": "60 minutes",
            },
        )
    )

    assert drafted["work"]["status"] == "ready"
    assert drafted["work"]["estimate"] == "2 hours"
    assert drafted["work"]["outcome"].startswith("A mounted test fixture")
    assert drafted["ai"]["last_plan_ready"] is True
    assert drafted["ai"]["last_plan_question"] == ""
    assert drafted["ai"]["tokens_used_today"] == 321
    assert len(intelligence.plan_calls) == 1

    started = asyncio.run(
        service.apply_action(
            token,
            {
                "action": "start",
                "outcome": drafted["work"]["outcome"],
                "first_step": drafted["work"]["first_step"],
                "evidence": drafted["work"]["evidence"],
                "estimate": drafted["work"]["estimate"],
                "checkpoint": drafted["work"]["checkpoint"],
            },
        )
    )
    checkpoint_draft = asyncio.run(
        service.apply_action(
            token,
            {
                "action": "coach_checkpoint",
                "progress": "I mounted it and all four bolts fit.",
                "blocker": "",
            },
        )
    )

    assert started["work"]["status"] == "active"
    assert checkpoint_draft["work"]["status"] == "active"
    assert "Evidence:" in checkpoint_draft["work"]["latest_progress"]
    assert "Next:" in checkpoint_draft["work"]["latest_progress"]
    assert checkpoint_draft["ai"]["last_checkpoint_ready"] is True
    assert checkpoint_draft["ai"]["last_checkpoint_question"] == ""
    assert checkpoint_draft["history"][0]["action"] == "coach_checkpoint"
    assert len(intelligence.checkpoint_calls) == 1


def test_worker_portal_html_exposes_review_before_submit_ai_controls(tmp_path) -> None:
    runtime, admin, _slack = _runtime(tmp_path)
    token = _token_from_link(build_worker_portal_link(runtime, admin))
    service = WorkerPortalService(runtime)
    html = service.render_html(asyncio.run(service.build_payload(token)))

    assert 'data-action="coach-plan"' in html
    assert 'data-action="coach-checkpoint"' in html
    assert 'data-progress-kind="still_working"' in html
    assert 'id="next-checkpoint"' in html
    assert 'id="checkpoint-panel"' in html
    assert "only Save update changes the work record" in html
    assert "Review / edit the structured work plan" in html


def test_worker_portal_overhead_claim_and_quality_flow(tmp_path) -> None:
    runtime, admin, _slack = _runtime(tmp_path)
    token = _token_from_link(build_worker_portal_link(runtime, admin))
    service = WorkerPortalService(runtime)

    overhead = asyncio.run(
        service.apply_action(
            token,
            {"action": "select_overhead_lane", "lane_id": "shop_facilities_safety"},
        )
    )
    assert overhead["work"]["overhead_lane_name"] == "Shop, facilities & safety"
    assert overhead["work"]["selected_task_id"] == ""

    selected = asyncio.run(
        service.apply_action(
            token,
            {
                "action": "select_task",
                "task_id": "assigned",
                "task_name": "Build test fixture",
                "task_location": "Hardware / Flight fixture",
            },
        )
    )
    assert selected["work"]["selected_task_id"] == "assigned"

    claimed = asyncio.run(
        service.apply_action(token, {"action": "claim_task", "task_id": "workspace-1"})
    )
    assert claimed["work"]["selected_task_id"] == "workspace-1"
    assert runtime.clickup.assignee_updates == [("workspace-1", ["123"])]
    assert "without removing" in claimed["message"]

    weak_payload = {
        "action": "start",
        "outcome": "make progress",
        "first_step": "continue",
        "estimate": "1 hour",
        "checkpoint": "60 minutes",
    }
    with pytest.raises(ValueError, match="more detail"):
        asyncio.run(service.apply_action(token, weak_payload))
    with pytest.raises(ValueError, match="same answer"):
        asyncio.run(service.apply_action(token, weak_payload))
    after_rejections = asyncio.run(service.build_payload(token))
    assert after_rejections["quality"]["weak_attempts"] == 2
    assert after_rejections["history"][0]["action"] == "start_rejected"

    started = asyncio.run(
        service.apply_action(
            token,
            {
                "action": "start",
                "outcome": "A tested mounting bracket revision with the corrected hole pattern attached",
                "first_step": "Open revision three and update the sketch constraints from the new measurements",
                "evidence": "Attach the revised CAD screenshot and record the measured hole spacing in the task",
                "estimate": "1 hour",
                "checkpoint": "60 minutes",
            },
        )
    )
    assert started["work"]["status"] == "active"
    assert runtime.state_store.list_sessions_for_date(datetime.now().date().isoformat()) == []


def test_saved_profile_is_marked_for_compact_rendering(tmp_path) -> None:
    runtime, admin, _slack = _runtime(tmp_path)
    token = _token_from_link(build_worker_portal_link(runtime, admin))
    service = WorkerPortalService(runtime)

    result = asyncio.run(
        service.apply_action(
            token,
            {
                "action": "save_profile",
                "weekly_target_hours": 32,
                "regular_workdays": ["monday", "tuesday", "thursday", "friday"],
                "typical_start_time": "08:30",
                "typical_end_time": "16:30",
                "planned_time_off": "2026-08-21",
                "interests": "program management, flight testing",
                "skills": "planning, review",
            },
        )
    )

    assert result["profile"]["saved_at"]
    assert result["profile"]["weekly_target_hours"] == 32
    html = service.render_html(result)
    assert 'id="profile-details"' in html
    assert 'id="catalog-details"' in html


def test_portal_task_request_and_summary_are_sent_only_to_beta_slack_user(tmp_path) -> None:
    runtime, admin, slack = _runtime(tmp_path)
    token = _token_from_link(build_worker_portal_link(runtime, admin))
    service = WorkerPortalService(runtime)

    result = asyncio.run(
        service.apply_action(
            token,
            {
                "action": "request_task",
                "title": "Label and organize the electronics workbench",
                "task_type": "overhead",
                "reason": "Make tools easy to find and reduce setup time for the next hardware build.",
            },
        )
    )
    assert result["task_requests"][0]["status"] == "pending approval"
    assert slack.messages[0][0] == "UERIK"
    assert "no ClickUp task created" in slack.messages[0][1]

    asyncio.run(service.apply_action(token, {"action": "share_slack"}))
    assert len(slack.messages) == 2
    assert "admin-preview summary" in slack.messages[1][1]


class _LiveRuntime:
    def __init__(self, tmp_path) -> None:
        self.state_store = StateStore(tmp_path / "live-state.sqlite3")
        self.slack = _FakeSlack()
        self.clickup = _FakeClickUp()
        self.config = SimpleNamespace(
            slack=SlackConfig(
                manager_queue_url="http://192.168.4.87:8765/exceptions",
                worker_portal_beta_slack_user_ids=["UAJ"],
            ),
            labor=SimpleNamespace(meal_minimum_minutes=30),
        )
        self.user = UserProfile(
            user_key="AJ",
            display_name="AJ Torres",
            slack_user_id="UAJ",
            clickup_user_id="456",
            preferred_transport="slack",
            worker_type="contractor",
            compensation_plan="cislune_hourly",
        )
        self.roster_by_slack_id = {"UAJ": self.user}
        self.admin_profile_by_slack_user_id = lambda _slack_user_id: None
        self._lock = asyncio.Lock()
        self.dashboard_writes = 0

    def get_user_session_for_moment(self, user):
        now = datetime.now(timezone.utc)
        return self.state_store.get_session(user.user_key, now.date().isoformat()), now

    def _user_session_lock(self, _user_key):
        return self._lock

    @staticmethod
    def _clone_session_state(session):
        return deepcopy(session)

    @staticmethod
    def _active_short_rest_break(session):
        value = session.metadata.get("short_rest_active")
        return value if isinstance(value, dict) else None

    @staticmethod
    def _overtime_restart_blocked(session):
        return bool(session.metadata.get("overtime_blocked"))

    @staticmethod
    def _active_task_id(session):
        return session.metadata.get("active_clickup_task_id")

    @staticmethod
    async def _pause_current_task_tracking(_user, session, now, **_kwargs):
        tracking = session.metadata.pop("clickup_time_tracking", None)
        if tracking:
            tracking["closed_at"] = now.isoformat()
            session.metadata.setdefault("clickup_time_tracking_history", []).append(tracking)

    @staticmethod
    def _clear_clock_out_state(session):
        session.clocked_out_at = None

    @staticmethod
    def _clear_auto_clock_out_metadata(session):
        session.metadata.pop("auto_clock_out_reason", None)

    @staticmethod
    def _start_new_work_segment(session, now):
        if not session.work_segments or session.work_segments[-1].get("clocked_out_at"):
            session.work_segments.append({"clocked_in_at": now.isoformat(), "clocked_out_at": None})

    @staticmethod
    def _close_current_work_segment(session, now):
        if session.work_segments and not session.work_segments[-1].get("clocked_out_at"):
            session.work_segments[-1]["clocked_out_at"] = now.isoformat()

    @staticmethod
    async def _activate_clickup_task(_user, session, now, task_id, task_name):
        session.metadata["active_clickup_task_id"] = task_id
        session.metadata["active_clickup_task_name"] = task_name
        session.metadata.setdefault(
            "clickup_time_tracking",
            {"task_id": task_id, "task_name": task_name, "started_at": now.isoformat()},
        )

    @staticmethod
    def _stable_external_author_id(_value):
        return 42

    @staticmethod
    def _touch_inbound_session(session, now):
        session.last_user_message_at = now.isoformat()
        session.pending_clickup_sync = True

    async def _send_dm(self, _client, user, session, content, now):
        posted = await self.slack.post_message(user.slack_user_id, content)
        return MessageRecord(
            message_id=f"slack:{posted['ts']}",
            direction="outbound",
            author_id=99,
            created_at=now,
            content=content,
        )

    async def _persist_session_state(self, _user, session, **_kwargs):
        self.state_store.save_session(session)
        return True

    @staticmethod
    def _refresh_session_time_summary(session, now):
        def elapsed(started_at, ended_at=None):
            started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            ended = (
                datetime.fromisoformat(str(ended_at).replace("Z", "+00:00"))
                if ended_at
                else now
            )
            return max(0, int((ended - started).total_seconds()))

        gross = sum(
            elapsed(segment["clocked_in_at"], segment.get("clocked_out_at"))
            for segment in session.work_segments
            if isinstance(segment, dict) and segment.get("clocked_in_at")
        )
        tracking_history = list(session.metadata.get("clickup_time_tracking_history") or [])
        current_tracking = session.metadata.get("clickup_time_tracking")
        if isinstance(current_tracking, dict):
            tracking_history.append(current_tracking)
        tracked = sum(
            elapsed(item["started_at"], item.get("closed_at"))
            for item in tracking_history
            if isinstance(item, dict) and item.get("started_at")
        )
        session.time_summary = {
            "gross_clocked_in_total_seconds": gross,
            "unpaid_lunch_deducted_seconds": 0,
            "clocked_in_total_seconds": gross,
            "task_tracked_total_seconds": tracked,
            "has_open_work_segment": any(
                segment.get("clocked_in_at") and not segment.get("clocked_out_at")
                for segment in session.work_segments
                if isinstance(segment, dict)
            ),
            "active_task_timer_running": isinstance(current_tracking, dict),
        }

    async def write_dashboard(self):
        self.dashboard_writes += 1

    @staticmethod
    async def _maybe_check_short_rest_break(_client, _user, _session, _now):
        return False

    @staticmethod
    async def _finalize_clickup_day(_user, session, now, **_kwargs):
        tracking = session.metadata.pop("clickup_time_tracking", None)
        if tracking:
            tracking["closed_at"] = now.isoformat()
            session.metadata.setdefault("clickup_time_tracking_history", []).append(tracking)
        return "Task timer stopped."


def test_roster_worker_portal_uses_one_live_session_and_idempotent_timer(tmp_path) -> None:
    runtime = _LiveRuntime(tmp_path)
    token = _token_from_link(build_worker_portal_link(runtime, runtime.user))
    service = WorkerPortalService(runtime)

    selected = asyncio.run(
        service.apply_action(
            token,
            {
                "action": "select_task",
                "task_id": "assigned",
                "task_name": "Build test fixture",
                "task_location": "Hardware / Flight fixture",
            },
        )
    )
    assert selected["live"] is True

    start = {
        "action": "start",
        "outcome": "A tested fixture assembly with the alignment measurements recorded for review",
        "first_step": "Measure the fixture base and record the first alignment datum in the build sheet",
        "evidence": "Upload the completed build sheet and an assembly photo showing the alignment marks",
        "estimate": "1 hour",
        "checkpoint": "60 minutes",
    }
    first = asyncio.run(service.apply_action(token, start))
    second = asyncio.run(service.apply_action(token, start))

    assert first["work"]["status"] == "active"
    assert second["work"]["status"] == "active"
    assert "no duplicate time" in second["message"]
    assert second["time"]["session_date"]
    assert second["time"]["clocked_in_at"]
    assert second["time"]["work_clock_running"] is True
    assert second["time"]["task_timer_running"] is True
    assert second["time"]["current_task_name"] == "Build test fixture"
    html = service.render_html(second)
    assert 'id="today-time"' in html
    assert 'id="current-task-time"' in html
    assert 'id="tracked-time"' in html
    assert "window.setInterval(renderTimers, 1000)" in html
    assert "const dirtyFields = new Set()" in html
    assert "if (!dirtyFields.has(id))" in html
    assert "clearSubmittedDraft(payload.action)" in html
    assert "const apiAction = action.replaceAll('-','_')" in html
    live_session, _ = runtime.get_user_session_for_moment(runtime.user)
    assert len(live_session.work_segments) == 1
    assert live_session.metadata["clickup_time_tracking"]["task_id"] == "assigned"
    assert live_session.metadata.get("clickup_time_tracking_history", []) == []
    assert live_session.metadata["task_onboarding_estimated_duration"] == "1 hour"
    assert live_session.metadata["worker_checkpoint"]["choice"] == "60 minutes"
    assert live_session.metadata["worker_checkpoint"]["reminder_sent"] is False

    checkpoint = asyncio.run(
        service.apply_action(
            token,
            {
                # Existing open pages used the DOM-style action name. The API
                # intentionally accepts it so a deploy fixes them immediately.
                "action": "check-in",
                "progress_kind": "blocked",
                "progress": "Completed the base measurements and recorded the first alignment datum in the build sheet",
                "blocker": "Waiting for the revised fastener dimensions before final assembly",
                "checkpoint": "2 hours",
            },
        )
    )
    assert "Blocker saved" in checkpoint["message"]
    live_session, _ = runtime.get_user_session_for_moment(runtime.user)
    assert live_session.latest_status.startswith("Completed the base measurements")
    assert live_session.latest_blocker == "Waiting for the revised fastener dimensions before final assembly"
    assert live_session.metadata["checkpoint_status"] == "blocked"
    assert live_session.metadata["worker_checkpoint"]["choice"] == "2 hours"
    assert live_session.metadata["checkpoint_quality_history"][-1]["meaningful"] is True

    clocked_out = asyncio.run(
        service.apply_action(
            token,
            {
                "action": "clock_out",
                "progress": "Completed the fixture assembly and recorded all alignment measurements in the build sheet",
                "blocker": "",
            },
        )
    )
    assert clocked_out["work"]["status"] == "clocked_out"
    assert clocked_out["time"]["work_clock_running"] is False
    assert clocked_out["time"]["task_timer_running"] is False
    live_session, _ = runtime.get_user_session_for_moment(runtime.user)
    assert live_session.work_segments[0]["clocked_out_at"]
    assert len(live_session.metadata["clickup_time_tracking_history"]) == 1
    assert any("Live work started" in message for _, message in runtime.slack.messages)
    assert any("clocked out" in message.lower() for _, message in runtime.slack.messages)
    assert runtime.dashboard_writes == 3


def test_live_portal_allows_one_still_working_ack_between_useful_updates(tmp_path) -> None:
    runtime = _LiveRuntime(tmp_path)
    token = _token_from_link(build_worker_portal_link(runtime, runtime.user))
    service = WorkerPortalService(runtime)
    asyncio.run(
        service.apply_action(
            token,
            {
                "action": "select_task",
                "task_id": "assigned",
                "task_name": "Build test fixture",
                "task_location": "Hardware / Flight fixture",
            },
        )
    )
    asyncio.run(
        service.apply_action(
            token,
            {
                "action": "start",
                "outcome": "A tested fixture base with alignment measurements recorded for review",
                "first_step": "Measure the base and enter each datum in the build sheet",
                "evidence": "Upload the build sheet and a photo of the alignment marks",
                "estimate": "2 hours",
                "checkpoint": "60 minutes",
            },
        )
    )

    first = asyncio.run(
        service.apply_action(
            token,
            {
                "action": "check_in",
                "progress_kind": "still_working",
                "progress": "",
                "blocker": "",
                "checkpoint": "30 minutes",
            },
        )
    )
    assert "no penalty" in first["message"]
    session, _ = runtime.get_user_session_for_moment(runtime.user)
    assert session.metadata["worker_checkpoint"]["choice"] == "30 minutes"
    assert isinstance(session.metadata["still_working_ack"], dict)

    with pytest.raises(ValueError, match="okay once"):
        asyncio.run(
            service.apply_action(
                token,
                {
                    "action": "check_in",
                    "progress_kind": "still_working",
                    "progress": "",
                    "blocker": "",
                    "checkpoint": "30 minutes",
                },
            )
        )

    corrected = asyncio.run(
        service.apply_action(
            token,
            {
                "action": "check_in",
                "progress_kind": "made_progress",
                "progress": "Measured all four fixture corners and recorded the alignment error in the build sheet",
                "blocker": "",
                "checkpoint": "when result is ready",
            },
        )
    )
    assert "Checkpoint saved" in corrected["message"]
    session, _ = runtime.get_user_session_for_moment(runtime.user)
    assert "still_working_ack" not in session.metadata
    assert session.metadata["worker_checkpoint"]["interval_minutes"] == 120


def test_live_portal_repeated_weak_checkpoint_opens_and_good_detail_clears_warning(tmp_path) -> None:
    runtime = _LiveRuntime(tmp_path)
    token = _token_from_link(build_worker_portal_link(runtime, runtime.user))
    service = WorkerPortalService(runtime)
    asyncio.run(
        service.apply_action(
            token,
            {
                "action": "select_task",
                "task_id": "assigned",
                "task_name": "Build test fixture",
                "task_location": "Hardware / Flight fixture",
            },
        )
    )
    asyncio.run(
        service.apply_action(
            token,
            {
                "action": "start",
                "outcome": "A tested fixture base with the alignment measurements recorded for review",
                "first_step": "Measure the fixture base and enter each datum in the build sheet",
                "evidence": "Upload the completed build sheet and a photo of the alignment marks",
                "estimate": "1 hour",
                "checkpoint": "60 minutes",
            },
        )
    )

    weak = {"action": "check_in", "progress": "working on it", "blocker": ""}
    with pytest.raises(ValueError, match="make this checkpoint useful"):
        asyncio.run(service.apply_action(token, weak))
    with pytest.raises(ValueError, match="QUALITY WARNING"):
        asyncio.run(service.apply_action(token, weak))

    warned = asyncio.run(service.build_payload(token))
    assert warned["quality"]["warning_deadline_at"]
    assert "repeats the last rejected answer" in " ".join(warned["quality"]["warning_reasons"])
    assert any("QUALITY WARNING" in message for _, message in runtime.slack.messages)

    corrected = asyncio.run(
        service.apply_action(
            token,
            {
                "action": "check_in",
                "progress": "Measured all four fixture corners and recorded a 0.8 mm alignment error in the build sheet",
                "blocker": "Need the revised shim dimension before final assembly",
            },
        )
    )
    assert corrected["quality"]["warning_deadline_at"] == ""
    session, _ = runtime.get_user_session_for_moment(runtime.user)
    assert "portal_quality_warning" not in session.metadata


def test_admin_portal_tracks_projects_live_but_uses_salary_nonpayroll_identity(tmp_path) -> None:
    runtime = _LiveRuntime(tmp_path)
    admin = AdminProfile(
        name="Erik",
        discord_user_id=999,
        slack_user_id="UERIK",
        clickup_user_id="123",
    )
    runtime.config.slack.worker_portal_beta_slack_user_ids.append("UERIK")
    runtime.admin_profile_by_slack_user_id = (
        lambda slack_user_id: admin if slack_user_id == "UERIK" else None
    )
    token = _token_from_link(build_worker_portal_link(runtime, admin))
    service = WorkerPortalService(runtime)

    initial = asyncio.run(service.build_payload(token))
    assert initial["live"] is True
    live_admin = service._live_user(admin)
    assert live_admin.worker_type == "admin"
    assert live_admin.compensation_plan == "salary"
    assert live_admin.overtime_approval_required is False

    asyncio.run(
        service.apply_action(
            token,
            {
                "action": "select_task",
                "task_id": "assigned",
                "task_name": "Build test fixture",
                "task_location": "Hardware / Flight fixture",
            },
        )
    )
    started = asyncio.run(
        service.apply_action(
            token,
            {
                "action": "start",
                "outcome": "A reviewed fixture delivery plan with owners and the next milestone recorded",
                "first_step": "Open the fixture task and verify its owner, deadline, and acceptance criteria",
                "evidence": "Post the reviewed milestone list with named owners and acceptance criteria in ClickUp",
                "estimate": "1 hour",
                "checkpoint": "60 minutes",
            },
        )
    )

    assert started["work"]["status"] == "active"
    session, _ = runtime.get_user_session_for_moment(live_admin)
    assert session.user_key == "portal-admin-erik"
    assert session.metadata["active_clickup_task_id"] == "assigned"

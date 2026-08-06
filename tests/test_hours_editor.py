import asyncio
import csv
import json
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from agent.hours_editor import HoursEditorService, build_request_handler
from agent.local_store import LocalStore
from agent.models import AgentConfig, BootstrapConfig, ClickUpConfig, PromptConfig, ScheduleConfig, SessionState, SlackProjectRoute, UserProfile
from agent.runtime import InternManagementRuntime
from agent.state_store import StateStore
from agent.time_tracking_dashboard import build_time_tracking_dashboard_payload


def _build_editor_runtime(tmp_path: Path, user: UserProfile) -> InternManagementRuntime:
    runtime = InternManagementRuntime.__new__(InternManagementRuntime)
    bootstrap = BootstrapConfig(
        agent_config_path=tmp_path / "config" / "agent.config.json",
        storage_root_path=tmp_path / "storage",
        state_db_path=tmp_path / "data" / "agent_state.sqlite3",
        default_timezone="America/Los_Angeles",
    )
    runtime.bootstrap = bootstrap
    runtime.state_store = StateStore(bootstrap.state_db_path)
    runtime.store = LocalStore(bootstrap)
    runtime.config = AgentConfig(
        timezone="America/Los_Angeles",
        admin_discord_user_id=999,
        roster_file_name="roster.csv",
        dashboard_file_name="dashboard.md",
        schedule=ScheduleConfig(),
        clickup=ClickUpConfig(workspace_id="workspace-1"),
        prompts=PromptConfig(
            clock_in="clock in",
            clock_in_reminder="reminder",
            plan_question="plan",
            start_photo_question="photo",
            risk_question="risk",
            follow_up_questions=["follow up"],
            clock_out_prompt="clock out",
        ),
        admins=[],
    )
    runtime.roster_by_key = {user.user_key: user}
    runtime.roster_by_discord_id = {user.discord_user_id: user}
    runtime.clickup = None
    runtime.advisor = SimpleNamespace()
    runtime.image_intelligence = SimpleNamespace()
    runtime.interface_intelligence = SimpleNamespace()
    runtime.admin_router = SimpleNamespace()
    runtime._config_loaded_at = None

    async def fake_refresh_configuration(force: bool = False) -> None:
        del force
        return None

    async def fake_write_dashboard() -> None:
        return None

    runtime.refresh_configuration = fake_refresh_configuration  # type: ignore[method-assign]
    runtime.write_dashboard = fake_write_dashboard  # type: ignore[method-assign]
    return runtime


def _seed_archived_session(
    runtime: InternManagementRuntime,
    user: UserProfile,
    session: SessionState,
    *,
    report_now: datetime,
) -> None:
    runtime.state_store.save_session(session)
    asyncio.run(runtime._archive_session(user, session))
    runtime._write_time_tracking_csv_sync(report_now)


def _read_json_lines(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_report_rows(storage_root: Path) -> list[dict[str, str]]:
    report_path = storage_root / "dashboard" / "time_tracking" / "time_tracking.csv"
    with report_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_runtime_preview_manual_time_edit_rejects_current_effective_workday(tmp_path: Path) -> None:
    user = UserProfile(
        user_key="alex",
        display_name="Alex Example",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex Example",
        timezone="America/Los_Angeles",
    )
    runtime = _build_editor_runtime(tmp_path, user)
    session = SessionState(
        user_key="alex",
        session_date="2026-06-15",
        stage="active",
        work_segments=[{"clocked_in_at": "2026-06-15T09:00:00-07:00", "clocked_out_at": "2026-06-15T11:00:00-07:00"}],
    )
    runtime.state_store.save_session(session)

    with pytest.raises(ValueError, match="Only past workdays can be edited"):
        asyncio.run(
            runtime.preview_manual_time_edit(
                "alex",
                "2026-06-15",
                [{"start_local": "2026-06-15T09:00", "end_local": "2026-06-15T10:00"}],
                edited_by="Pat Manager",
                reason="Current-day correction",
                now=datetime.fromisoformat("2026-06-15T12:00:00-07:00"),
            )
        )


def test_runtime_apply_manual_time_edit_updates_state_archive_and_reports(tmp_path: Path) -> None:
    user = UserProfile(
        user_key="alex",
        display_name="Alex Example",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex Example",
        timezone="America/Los_Angeles",
    )
    runtime = _build_editor_runtime(tmp_path, user)
    session = SessionState(
        user_key="alex",
        session_date="2026-06-12",
        stage="active",
        clocked_in_at="2026-06-12T09:00:00-07:00",
        clocked_out_at="2026-06-12T17:00:00-07:00",
        awaiting_clock_out_photo=True,
        awaiting_clock_out_summary=True,
        pending_clickup_sync=True,
        work_segments=[{"clocked_in_at": "2026-06-12T09:00:00-07:00", "clocked_out_at": "2026-06-12T17:00:00-07:00"}],
        metadata={
            "auto_clock_out_at": "2026-06-12T17:10:00-07:00",
            "clickup_time_tracking_history": [
                {
                    "task_id": "task-a",
                    "task_name": "Morning setup",
                    "started_at": "2026-06-12T09:30:00-07:00",
                    "closed_at": "2026-06-12T11:30:00-07:00",
                    "duration_seconds": 7200,
                },
                {
                    "task_id": "task-b",
                    "task_name": "Bench testing",
                    "started_at": "2026-06-12T12:00:00-07:00",
                    "closed_at": "2026-06-12T14:00:00-07:00",
                    "duration_seconds": 7200,
                },
            ],
            "clickup_time_tracking": {
                "task_id": "task-c",
                "task_name": "Wrap-up",
                "started_at": "2026-06-12T14:30:00-07:00",
                "source": "local",
            },
        },
    )
    _seed_archived_session(
        runtime,
        user,
        session,
        report_now=datetime.fromisoformat("2026-06-15T09:00:00-07:00"),
    )

    result = asyncio.run(
        runtime.apply_manual_time_edit(
            "alex",
            "2026-06-12",
            [
                {"start_local": "2026-06-12T09:00", "end_local": "2026-06-12T10:00"},
                {"start_local": "2026-06-12T13:00", "end_local": "2026-06-12T15:00"},
            ],
            edited_by="Pat Manager",
            reason="Fix payroll correction",
            now=datetime.fromisoformat("2026-06-15T10:00:00-07:00"),
        )
    )

    assert result["applied"] is True
    assert result["after"]["clocked_in_total_seconds"] == 10800
    assert result["after"]["task_tracked_total_seconds"] == 7200

    saved = runtime.state_store.get_session("alex", "2026-06-12")
    assert saved.stage == "clocked_out"
    assert saved.awaiting_clock_out_photo is False
    assert saved.awaiting_clock_out_summary is False
    assert saved.pending_clickup_sync is False
    assert "clickup_time_tracking" not in saved.metadata
    history = saved.metadata["clickup_time_tracking_history"]
    assert [entry["duration_seconds"] for entry in history] == [1800, 3600, 1800]
    assert saved.metadata["latest_manual_time_edit"]["edited_by"] == "Pat Manager"
    assert saved.metadata["latest_manual_time_edit"]["reason"] == "Fix payroll correction"

    session_path = (
        runtime.bootstrap.storage_root_path
        / "people"
        / user.storage_folder_name
        / "2026-06-12"
        / "session.json"
    )
    session_payload = json.loads(session_path.read_text(encoding="utf-8"))
    assert session_payload["stage"] == "clocked_out"
    assert len(session_payload["work_segments"]) == 2

    manual_log_path = session_path.with_name("manual_time_edits.jsonl")
    assert manual_log_path.exists()
    manual_log_entries = _read_json_lines(manual_log_path)
    assert manual_log_entries[-1]["edited_by"] == "Pat Manager"
    assert manual_log_entries[-1]["reason"] == "Fix payroll correction"

    state_changes_path = session_path.with_name("state_machine_changes.jsonl")
    state_changes = _read_json_lines(state_changes_path)
    assert state_changes[-1]["trigger"] == "manual_time_edit"

    report_rows = _read_report_rows(runtime.bootstrap.storage_root_path)
    assert report_rows[0]["clocked_in_total_seconds"] == "10800"
    assert report_rows[0]["task_tracked_total_seconds"] == "7200"

    dashboard_path = runtime.bootstrap.storage_root_path / "dashboard" / "time_tracking" / "time_tracking_dashboard.html"
    dashboard_html = dashboard_path.read_text(encoding="utf-8")
    assert "Read-only snapshot." in dashboard_html
    assert ".venv/bin/python -m agent.hours_editor" in dashboard_html
    editor_payload = build_time_tracking_dashboard_payload(
        runtime.bootstrap.storage_root_path,
        runtime=runtime,
        reference_now=datetime.fromisoformat("2026-06-15T10:00:00-07:00"),
        editor_mode=True,
    )
    assert editor_payload["hours_rows"][0]["latest_manual_edit"]["edited_by"] == "Pat Manager"


def test_runtime_apply_manual_time_edit_clamps_retro_backfill_task_windows(tmp_path: Path) -> None:
    user = UserProfile(
        user_key="sam",
        display_name="Sam Example",
        discord_user_id=2,
        discord_username="sam",
        storage_folder_name="Sam Example",
        timezone="America/Los_Angeles",
    )
    runtime = _build_editor_runtime(tmp_path, user)
    session = SessionState(
        user_key="sam",
        session_date="2026-06-11",
        stage="clocked_out",
        clocked_in_at="2026-06-11T08:00:00-07:00",
        clocked_out_at="2026-06-11T18:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-11T08:00:00-07:00", "clocked_out_at": "2026-06-11T18:00:00-07:00"}],
        metadata={
            "retro_hours_backfill": {
                "task_windows": [
                    {
                        "task_id": "retro-a",
                        "task_name": "Long retro task",
                        "started_at": "2026-06-11T08:00:00-07:00",
                        "ended_at": "2026-06-11T18:00:00-07:00",
                        "duration_seconds": 36000,
                    },
                    {
                        "task_id": "retro-b",
                        "task_name": "Dropped retro task",
                        "started_at": "2026-06-11T19:00:00-07:00",
                        "ended_at": "2026-06-11T19:30:00-07:00",
                        "duration_seconds": 1800,
                    },
                ]
            }
        },
    )
    _seed_archived_session(
        runtime,
        user,
        session,
        report_now=datetime.fromisoformat("2026-06-15T09:00:00-07:00"),
    )

    result = asyncio.run(
        runtime.apply_manual_time_edit(
            "sam",
            "2026-06-11",
            [
                {"start_local": "2026-06-11T09:00", "end_local": "2026-06-11T10:00"},
                {"start_local": "2026-06-11T13:00", "end_local": "2026-06-11T15:00"},
            ],
            edited_by="Pat Manager",
            reason="Replace inflated retro totals",
            now=datetime.fromisoformat("2026-06-15T10:00:00-07:00"),
        )
    )

    assert any("retro-backfill task window" in warning for warning in result["warnings"])
    saved = runtime.state_store.get_session("sam", "2026-06-11")
    retro_windows = saved.metadata["retro_hours_backfill"]["task_windows"]
    assert len(retro_windows) == 2
    assert [window["duration_seconds"] for window in retro_windows] == [3600, 7200]
    assert saved.time_summary["task_tracked_total_seconds"] == 10800


def test_hours_editor_server_serves_dashboard_and_preview_without_persisting(tmp_path: Path) -> None:
    user = UserProfile(
        user_key="alex",
        display_name="Alex Example",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex Example",
        timezone="America/Los_Angeles",
    )
    runtime = _build_editor_runtime(tmp_path, user)
    session = SessionState(
        user_key="alex",
        session_date="2026-06-12",
        stage="clocked_out",
        clocked_in_at="2026-06-12T09:00:00-07:00",
        clocked_out_at="2026-06-12T17:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-12T09:00:00-07:00", "clocked_out_at": "2026-06-12T17:00:00-07:00"}],
    )
    _seed_archived_session(
        runtime,
        user,
        session,
        report_now=datetime.fromisoformat("2026-06-15T09:00:00-07:00"),
    )
    service = HoursEditorService(runtime)
    service._reference_now = lambda: datetime.fromisoformat("2026-06-15T10:00:00-07:00")  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_request_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        page = requests.get(f"{base_url}/", timeout=5)
        assert page.status_code == 200
        assert 'id="editor-panel"' in page.text
        assert 'id="editor-segments"' in page.text
        assert 'id="editor-edited-by"' in page.text
        assert 'id="editor-reason"' in page.text

        data = requests.get(f"{base_url}/api/dashboard-data", timeout=5)
        assert data.status_code == 200
        payload = data.json()
        row = payload["hours_rows"][0]
        assert row["editable"] is True
        assert row["work_segments"][0]["start_local"] == "2026-06-12T09:00"

        preview = requests.post(
            f"{base_url}/api/edit-preview",
            json={
                "user_key": "alex",
                "session_date": "2026-06-12",
                "segments": [{"start_local": "2026-06-12T09:00", "end_local": "2026-06-12T12:00"}],
                "edited_by": "Pat Manager",
                "reason": "Preview only",
            },
            timeout=5,
        )
        assert preview.status_code == 200
        assert preview.json()["after"]["clocked_in_total_seconds"] == 10800

        saved = runtime.state_store.get_session("alex", "2026-06-12")
        assert saved.work_segments[0]["clocked_out_at"] == "2026-06-12T17:00:00-07:00"
        manual_log_path = (
            runtime.bootstrap.storage_root_path
            / "people"
            / user.storage_folder_name
            / "2026-06-12"
            / "manual_time_edits.jsonl"
        )
        assert manual_log_path.exists() is False
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_hours_editor_server_serves_work_dashboard_and_clickup_task_payload(tmp_path: Path) -> None:
    user = UserProfile(
        user_key="alex",
        display_name="Alex Example",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex Example",
        timezone="America/Los_Angeles",
    )
    runtime = _build_editor_runtime(tmp_path, user)
    session = SessionState(
        user_key="alex",
        session_date="2026-06-12",
        stage="active",
        clocked_in_at="2026-06-12T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-12T09:00:00-07:00", "clocked_out_at": "2026-06-12T18:00:00-07:00"}],
        latest_status="Finished the firmware harness.",
        metadata={"active_clickup_task_id": "task-1", "active_clickup_task_name": "Firmware"},
    )
    runtime.state_store.append_message(
        "alex",
        "2026-06-12",
        SimpleNamespace(
            message_id="m1",
            direction="inbound",
            author_id=1,
            created_at=datetime.fromisoformat("2026-06-12T17:00:00-07:00"),
            content="Finished the firmware harness.",
            attachments=[],
        ),
    )
    _seed_archived_session(
        runtime,
        user,
        session,
        report_now=datetime.fromisoformat("2026-06-15T09:00:00-07:00"),
    )

    class FakeClickUp:
        async def list_assigned_tasks(self, _user: UserProfile, limit: int = 25):
            del limit
            return [
                {
                    "id": "task-next",
                    "name": "Next firmware task",
                    "status": {"status": "to do"},
                    "priority": {"priority": "normal"},
                    "list": {"name": "Intern Board"},
                    "url": "https://example.test/task-next",
                }
            ]

    runtime.clickup = FakeClickUp()
    service = HoursEditorService(runtime)
    service._reference_now = lambda: datetime.fromisoformat("2026-06-12T17:30:00-07:00")  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_request_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        page = requests.get(f"{base_url}/work", timeout=5)
        assert page.status_code == 200
        assert "Work Dashboard" in page.text
        assert "/api/work-dashboard-data" in page.text
        assert "Click for daily log + ClickUp task list" in page.text
        assert "buildOverviewBullets" in page.text

        data = requests.get(f"{base_url}/api/work-dashboard-data", timeout=5)
        assert data.status_code == 200
        payload = data.json()
        person = payload["people"][0]
        assert person["display_name"] == "Alex Example"
        assert person["active_task_name"] == "Firmware"
        assert person["net_clocked_in_seconds_recent"] == 32400
        assert person["unpaid_lunch_deducted_seconds_recent"] == 0
        assert person["available_tasks"][0]["name"] == "Next firmware task"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_hours_editor_server_serves_system_health_and_resolves_issue(tmp_path: Path) -> None:
    user = UserProfile(
        user_key="alex",
        display_name="Alex Example",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex Example",
    )
    runtime = _build_editor_runtime(tmp_path, user)
    runtime.slack = SimpleNamespace()
    runtime.state_store.record_operational_issue(
        fingerprint="route:alex",
        category="slack_route_uncertain",
        severity="warning",
        summary="Slack routing needs review for Alex.",
        details={"user_key": "alex"},
    )
    service = HoursEditorService(runtime)
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_request_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        page = requests.get(f"{base_url}/health", timeout=5)
        assert page.status_code == 200
        assert "System Health" in page.text
        assert "Slack routing needs review for Alex" in page.text

        data = requests.get(f"{base_url}/api/health", timeout=5)
        assert data.status_code == 200
        assert data.json()["open_issue_count"] == 1
        assert data.json()["database"]["schema_version"] == 1

        resolved = requests.post(
            f"{base_url}/api/issues/resolve",
            json={"fingerprint": "route:alex"},
            timeout=5,
        )
        assert resolved.status_code == 200
        assert resolved.json()["resolved"] is True
        assert runtime.state_store.list_operational_issues(status="open") == []
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_hours_editor_resolves_uncertain_slack_route_with_persisted_override(tmp_path: Path) -> None:
    user = UserProfile(
        user_key="alex",
        display_name="Alex Example",
        discord_user_id=1,
        storage_folder_name="Alex Example",
    )
    runtime = _build_editor_runtime(tmp_path, user)
    runtime.config.slack.project_routes = [
        SlackProjectRoute(
            label="CARVE",
            channel_id="CCARVE",
        )
    ]
    runtime.state_store.record_operational_issue(
        fingerprint="route:alex:task-1",
        category="slack_route_uncertain",
        severity="warning",
        summary="Slack routing needs review for Alex.",
        details={
            "user_key": "alex",
            "session_date": "2026-06-12",
            "active_task_id": "task-1",
        },
    )

    result = HoursEditorService(runtime).resolve_slack_route(
        {
            "fingerprint": "route:alex:task-1",
            "channel_id": "CCARVE",
            "resolved_by": "Erik",
        }
    )

    assert result["resolved"] is True
    assert runtime._slack_route_override(
        user_key="alex",
        session_date="2026-06-12",
        task_id="task-1",
    )["channel_id"] == "CCARVE"
    assert runtime.state_store.list_operational_issues(status="open") == []


def test_hours_editor_corrects_active_clickup_task_with_audit(tmp_path: Path) -> None:
    user = UserProfile(
        user_key="alex",
        display_name="Alex Example",
        discord_user_id=1,
        storage_folder_name="Alex Example",
    )
    runtime = _build_editor_runtime(tmp_path, user)
    runtime.config.clickup.create_time_entries = False
    runtime.config.clickup.auto_status_updates = False
    session = SessionState(
        user_key="alex",
        session_date="2026-06-12",
        stage="clocked_out",
        clocked_in_at="2026-06-12T09:00:00-07:00",
        clocked_out_at="2026-06-12T17:00:00-07:00",
        work_segments=[
            {
                "clocked_in_at": "2026-06-12T09:00:00-07:00",
                "clocked_out_at": "2026-06-12T17:00:00-07:00",
            }
        ],
        metadata={
            "active_clickup_task_id": "wrong-task",
            "active_clickup_task_name": "Wrong project",
        },
    )
    runtime.state_store.save_session(session)

    class FakeClickUp:
        async def list_assigned_tasks(self, _user, limit=100):
            del limit
            return [{"id": "right-task", "name": "Correct project task"}]

    runtime.clickup = FakeClickUp()

    result = HoursEditorService(runtime).resolve_work_assignment(
        {
            "user_key": "alex",
            "session_date": "2026-06-12",
            "task_id": "right-task",
            "corrected_by": "Erik",
            "reason": "The update belongs to the rover project.",
        }
    )

    saved = runtime.state_store.get_session("alex", "2026-06-12")
    assert result["task_name"] == "Correct project task"
    assert saved.metadata["active_clickup_task_id"] == "right-task"
    assert saved.metadata["operator_task_corrections"][-1]["corrected_by"] == "Erik"
    changes = _read_json_lines(
        tmp_path
        / "storage"
        / "people"
        / "Alex Example"
        / "2026-06-12"
        / "state_machine_changes.jsonl"
    )
    assert changes[-1]["trigger"] == "operator_task_correction"


def test_hours_editor_serves_manager_exception_queue(tmp_path: Path) -> None:
    user = UserProfile(
        user_key="alex",
        display_name="Alex Example",
        discord_user_id=1,
        storage_folder_name="Alex Example",
    )
    runtime = _build_editor_runtime(tmp_path, user)
    runtime.state_store.save_session(
        SessionState(
            user_key="alex",
            session_date="2026-06-15",
            stage="active",
            clocked_in_at="2026-06-15T09:00:00-07:00",
        )
    )
    service = HoursEditorService(runtime)
    service._reference_now = lambda: datetime.fromisoformat(  # type: ignore[method-assign]
        "2026-06-15T10:00:00-07:00"
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_request_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        page = requests.get(f"{base_url}/exceptions", timeout=5)
        assert page.status_code == 200
        assert "Manager Exception Queue" in page.text

        data = requests.get(f"{base_url}/api/exceptions", timeout=5)
        assert data.status_code == 200
        assert any(
            item["category"] == "missing_active_task"
            for item in data.json()["exceptions"]
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_hours_editor_server_rejects_current_day_edits(tmp_path: Path) -> None:
    user = UserProfile(
        user_key="alex",
        display_name="Alex Example",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex Example",
        timezone="America/Los_Angeles",
    )
    runtime = _build_editor_runtime(tmp_path, user)
    session = SessionState(
        user_key="alex",
        session_date="2026-06-15",
        stage="clocked_out",
        clocked_in_at="2026-06-15T09:00:00-07:00",
        clocked_out_at="2026-06-15T10:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-15T09:00:00-07:00", "clocked_out_at": "2026-06-15T10:00:00-07:00"}],
    )
    _seed_archived_session(
        runtime,
        user,
        session,
        report_now=datetime.fromisoformat("2026-06-15T09:00:00-07:00"),
    )
    service = HoursEditorService(runtime)
    service._reference_now = lambda: datetime.fromisoformat("2026-06-15T10:00:00-07:00")  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_request_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        response = requests.post(
            f"{base_url}/api/edit-preview",
            json={
                "user_key": "alex",
                "session_date": "2026-06-15",
                "segments": [{"start_local": "2026-06-15T09:00", "end_local": "2026-06-15T10:00"}],
                "edited_by": "Pat Manager",
                "reason": "Should fail",
            },
            timeout=5,
        )
        assert response.status_code == 400
        assert "Only past workdays can be edited" in response.json()["error"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_hours_editor_server_routes_payroll_review_resolution() -> None:
    received: list[dict[str, object]] = []

    class Service:
        def resolve_payroll_review(self, payload):
            received.append(payload)
            return {"resolved": True}

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        build_request_handler(Service()),  # type: ignore[arg-type]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = requests.post(
            f"http://127.0.0.1:{server.server_port}/api/payroll-review-resolve",
            json={
                "user_key": "alex",
                "session_date": "2026-06-12",
                "resolved_by": "Pat Manager",
                "note": "Reviewed source evidence.",
                "remember_similar": False,
            },
            timeout=5,
        )
        assert response.status_code == 200
        assert response.json()["resolved"] is True
        assert received[0]["user_key"] == "alex"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

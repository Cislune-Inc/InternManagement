import asyncio
import csv
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from agent.models import (
    AgentConfig,
    ClickUpConfig,
    LaborConfig,
    PromptConfig,
    ScheduleConfig,
    SessionState,
    SlackConfig,
    SlackProjectRoute,
    UserProfile,
)
from agent.payroll_dashboard import (
    build_payroll_dashboard_payload,
    render_payroll_dashboard_html,
)
from agent.payroll_export import PayrollExporter
from agent.payroll_review import record_review_resolution
from agent.runtime import InternManagementRuntime


def _runtime(tmp_path: Path) -> tuple[InternManagementRuntime, UserProfile]:
    runtime = InternManagementRuntime.__new__(InternManagementRuntime)
    runtime.bootstrap = SimpleNamespace(
        storage_root_path=tmp_path / "storage",
        default_timezone="America/Los_Angeles",
    )
    runtime.config = AgentConfig(
        timezone="America/Los_Angeles",
        admin_discord_user_id=1,
        roster_file_name="roster.csv",
        dashboard_file_name="dashboard.md",
        schedule=ScheduleConfig(),
        clickup=ClickUpConfig(workspace_id="workspace"),
        prompts=PromptConfig(
            clock_in="clock in",
            clock_in_reminder="reminder",
            plan_question="plan",
            start_photo_question="photo",
            risk_question="risk",
            follow_up_questions=["follow up"],
            clock_out_prompt="clock out",
        ),
        slack=SlackConfig(
            project_routes=[
                SlackProjectRoute(
                    label="CARVE",
                    channel_id="CCARVE",
                    clickup_task_ids=["task-1"],
                    labor_code="CARVE",
                    budget_hours=100,
                )
            ]
        ),
        labor=LaborConfig(),
    )
    runtime.clickup = SimpleNamespace(
        get_task=lambda task_id: asyncio.sleep(
            0,
            result={"id": task_id, "name": "CARVE controls", "parent": None},
        )
    )
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
        worker_type="employee",
        compensation_plan="cislune_hourly",
        gusto_entity_uuid="gusto-alex",
        labor_cost_rate=50,
    )
    runtime.roster_by_key = {user.user_key: user}
    return runtime, user


def test_payroll_export_writes_review_gusto_project_and_compliance_bundle(tmp_path: Path) -> None:
    runtime, user = _runtime(tmp_path)
    session = SessionState(
        user_key=user.user_key,
        session_date="2026-07-27",
        stage="clocked_out",
        clocked_in_at="2026-07-27T09:00:00-07:00",
        clocked_out_at="2026-07-27T17:00:00-07:00",
        work_segments=[
            {
                "clocked_in_at": "2026-07-27T09:00:00-07:00",
                "clocked_out_at": "2026-07-27T17:00:00-07:00",
            }
        ],
        metadata={
            "active_clickup_task_id": "task-1",
            "active_clickup_task_name": "CARVE controls",
            "clickup_time_tracking_history": [
                {
                    "task_id": "task-1",
                    "task_name": "CARVE controls",
                    "started_at": "2026-07-27T09:00:00-07:00",
                    "closed_at": "2026-07-27T16:00:00-07:00",
                    "duration_seconds": 25200,
                    "source": "local",
                }
            ],
            "compliance_events": [
                {
                    "event_type": "meal_warning",
                    "recorded_at": "2026-07-27T13:30:00-07:00",
                    "worked_seconds": 16200,
                }
            ],
        },
    )
    session_path = (
        runtime.bootstrap.storage_root_path
        / "people"
        / user.storage_folder_name
        / session.session_date
        / "session.json"
    )
    session_path.parent.mkdir(parents=True)
    session_path.write_text(json.dumps(asdict(session)), encoding="utf-8")

    summary = asyncio.run(PayrollExporter(runtime).export(date(2026, 8, 2)))

    output = runtime.bootstrap.storage_root_path / "dashboard" / "payroll" / "2026-08-02"
    assert summary["worker_days"] == 1
    assert (output / "payroll_review.csv").exists()
    assert (output / "gusto_time_sheets.json").exists()
    assert (output / "nasa_project_labor.csv").exists()
    assert (output / "compliance_events.csv").exists()
    gusto = json.loads((output / "gusto_time_sheets.json").read_text(encoding="utf-8"))
    assert gusto["approval_required"] is True
    assert gusto["ready_for_submission"] is False
    assert gusto["time_sheets"][0]["entity_uuid"] == "gusto-alex"
    with (output / "project_summary.csv").open(encoding="utf-8", newline="") as handle:
        projects = list(csv.DictReader(handle))
    assert projects[0]["project"] == "CARVE"
    assert projects[0]["budget_hours"] == "100"

    latest = runtime.bootstrap.storage_root_path / "dashboard" / "payroll" / "latest"
    payload = build_payroll_dashboard_payload(runtime.bootstrap.storage_root_path)
    html = render_payroll_dashboard_html(payload)
    assert (latest / "summary.json").exists()
    assert payload["summary"]["week_ending"] == "2026-08-02"
    assert "Payroll and Project Labor Review" in html
    assert "CARVE" in html
    assert "Resolve payroll review" in html


def test_missing_gusto_is_informational_and_review_resolution_can_learn_variance(
    tmp_path: Path,
) -> None:
    runtime, user = _runtime(tmp_path)
    user.gusto_entity_uuid = None
    session = SessionState(
        user_key=user.user_key,
        session_date="2026-07-27",
        stage="clocked_out",
        clocked_in_at="2026-07-27T09:00:00-07:00",
        clocked_out_at="2026-07-27T17:00:00-07:00",
        work_segments=[
            {
                "clocked_in_at": "2026-07-27T09:00:00-07:00",
                "clocked_out_at": "2026-07-27T17:00:00-07:00",
            }
        ],
        metadata={
            "clickup_time_tracking_history": [
                {
                    "task_id": "task-1",
                    "task_name": "CARVE controls",
                    "started_at": "2026-07-27T09:00:00-07:00",
                    "closed_at": "2026-07-27T16:00:00-07:00",
                    "duration_seconds": 25200,
                    "source": "local",
                }
            ]
        },
    )
    session_path = (
        runtime.bootstrap.storage_root_path
        / "people"
        / user.storage_folder_name
        / session.session_date
        / "session.json"
    )
    session_path.parent.mkdir(parents=True)
    session_path.write_text(json.dumps(asdict(session)), encoding="utf-8")
    exporter = PayrollExporter(runtime)

    asyncio.run(exporter.export(date(2026, 8, 2)))
    payload = build_payroll_dashboard_payload(runtime.bootstrap.storage_root_path)
    row = payload["payroll_rows"][0]
    assert row["review_status"] == "needs_review"
    assert row["review_codes"] == '["task_time_variance"]'
    assert "Not mapped to Gusto" in row["integration_notes"]
    assert "Missing gusto" not in row["warnings"]

    record_review_resolution(
        runtime.bootstrap.storage_root_path,
        week_ending="2026-08-02",
        row=row,
        resolved_by="Pat Manager",
        note="Reviewed the task timer gap against the daily update.",
        remember_similar=True,
    )
    asyncio.run(exporter.export(date(2026, 8, 2)))
    resolved = build_payroll_dashboard_payload(runtime.bootstrap.storage_root_path)
    assert resolved["payroll_rows"][0]["review_status"] == "resolved"
    assert resolved["payroll_rows"][0]["requires_review"] == "False"
    gusto = json.loads(
        (
            runtime.bootstrap.storage_root_path
            / "dashboard"
            / "payroll"
            / "latest"
            / "gusto_time_sheets.json"
        ).read_text(encoding="utf-8")
    )
    assert gusto["time_sheets"] == []
    assert gusto["unmapped_workers"] == ["Alex"]


def test_nasa_stipend_effort_stays_in_projects_but_out_of_hourly_payroll(
    tmp_path: Path,
) -> None:
    runtime, _hourly_user = _runtime(tmp_path)
    stipend_user = UserProfile(
        user_key="nasa-intern",
        display_name="NASA Intern",
        discord_user_id=2,
        storage_folder_name="NASA Intern",
        worker_type="intern",
        compensation_plan="nasa_stipend",
        gusto_entity_uuid="must-not-be-used",
    )
    runtime.roster_by_key = {stipend_user.user_key: stipend_user}
    session = SessionState(
        user_key=stipend_user.user_key,
        session_date="2026-07-27",
        stage="clocked_out",
        clocked_in_at="2026-07-27T09:00:00-07:00",
        clocked_out_at="2026-07-27T13:00:00-07:00",
        work_segments=[
            {
                "clocked_in_at": "2026-07-27T09:00:00-07:00",
                "clocked_out_at": "2026-07-27T13:00:00-07:00",
            }
        ],
        metadata={
            "clickup_time_tracking_history": [
                {
                    "task_id": "task-1",
                    "task_name": "CARVE controls",
                    "started_at": "2026-07-27T09:00:00-07:00",
                    "closed_at": "2026-07-27T13:00:00-07:00",
                    "duration_seconds": 14400,
                    "source": "local",
                }
            ]
        },
    )
    session_path = (
        runtime.bootstrap.storage_root_path
        / "people"
        / stipend_user.storage_folder_name
        / session.session_date
        / "session.json"
    )
    session_path.parent.mkdir(parents=True)
    session_path.write_text(json.dumps(asdict(session)), encoding="utf-8")

    summary = asyncio.run(PayrollExporter(runtime).export(date(2026, 8, 2)))
    latest = runtime.bootstrap.storage_root_path / "dashboard" / "payroll" / "latest"
    gusto = json.loads((latest / "gusto_time_sheets.json").read_text(encoding="utf-8"))
    with (latest / "project_labor.csv").open(encoding="utf-8", newline="") as handle:
        labor_rows = list(csv.DictReader(handle))
    with (latest / "payroll_review.csv").open(encoding="utf-8", newline="") as handle:
        review_rows = list(csv.DictReader(handle))

    assert summary["tracked_hours"] == 4.0
    assert summary["hourly_payroll_hours"] == 0.0
    assert summary["stipend_effort_hours"] == 4.0
    assert gusto["time_sheets"] == []
    assert gusto["unmapped_workers"] == []
    assert gusto["excluded_non_hourly_workers"] == ["NASA Intern"]
    assert labor_rows[0]["compensation_plan"] == "nasa_stipend"
    assert labor_rows[0]["hours"] == "4.0"
    assert review_rows[0]["hourly_payroll_hours"] == "0.0"
    assert "excluded from Cislune hourly payroll" in review_rows[0]["integration_notes"]


def test_payroll_review_flags_long_lunch_and_missing_return(tmp_path: Path) -> None:
    runtime, user = _runtime(tmp_path)
    session = SessionState(
        user_key=user.user_key,
        session_date="2026-07-27",
        stage="clocked_out",
        clocked_in_at="2026-07-27T09:00:00-07:00",
        clocked_out_at="2026-07-27T17:00:00-07:00",
        work_segments=[
            {
                "clocked_in_at": "2026-07-27T09:00:00-07:00",
                "clocked_out_at": "2026-07-27T17:00:00-07:00",
            }
        ],
        metadata={
            "lunch_started_at": "2026-07-27T12:00:00-07:00",
            "lunch_ended_at": "2026-07-27T13:45:00-07:00",
        },
    )
    runtime._refresh_session_time_summary(
        session,
        datetime.fromisoformat("2026-07-27T17:00:00-07:00"),
    )
    exporter = PayrollExporter(runtime)

    row = exporter._build_review_row(user, session, tmp_path / "session.json")

    assert "long_lunch" in json.loads(row["review_codes"])
    assert "exceeds 90 minutes" in row["warnings"]

    session.metadata.pop("lunch_ended_at")
    missing_row = exporter._build_review_row(user, session, tmp_path / "session.json")

    assert "missing_lunch_return" in json.loads(missing_row["review_codes"])

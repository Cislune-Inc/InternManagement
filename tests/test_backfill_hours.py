import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from agent.backfill_hours import RetroHoursBackfiller, _parse_args
from agent.models import AgentConfig, ClickUpConfig, MessageRecord, PromptConfig, ScheduleConfig, SessionState, UserProfile
from agent.runtime import InternManagementRuntime
from agent.state_store import StateStore


def _build_runtime(tmp_path: Path) -> InternManagementRuntime:
    runtime = InternManagementRuntime.__new__(InternManagementRuntime)
    runtime.bootstrap = SimpleNamespace(
        default_timezone="America/Los_Angeles",
        storage_root_path=tmp_path / "storage",
        state_db_path=tmp_path / "data" / "state.sqlite3",
    )
    runtime.config = AgentConfig(
        timezone="America/Los_Angeles",
        admin_discord_user_id=999,
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
        admins=[],
    )
    runtime.state_store = StateStore(runtime.bootstrap.state_db_path)
    runtime.roster_by_key = {}
    runtime.roster_by_discord_id = {}
    runtime.clickup = None
    runtime._config_loaded_at = None
    return runtime


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


def _write_transcript(user: UserProfile, storage_root: Path, session_date: str, sections: list[tuple[str, str, str]]) -> Path:
    user_dir = storage_root / "people" / user.storage_folder_name
    daily_dir = user_dir / session_date
    daily_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {user.display_name} - {session_date}",
        "",
        f"- User key: `{user.user_key}`",
        "",
        "## Transcript",
        "",
    ]
    for timestamp_text, author, content in sections:
        lines.append(f"### {timestamp_text} - {author}")
        lines.append("")
        lines.append(content)
        lines.append("")
    path = daily_dir / "transcript.md"
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return path


def _append_message(runtime: InternManagementRuntime, user: UserProfile, session_date: str, *, message_id: str, created_at: str, content: str, direction: str = "inbound") -> None:
    runtime.state_store.append_message(
        user.user_key,
        session_date,
        MessageRecord(
            message_id=message_id,
            direction=direction,
            author_id=user.discord_user_id if direction == "inbound" else 0,
            created_at=datetime.fromisoformat(created_at),
            content=content,
            attachments=[],
        ),
    )


def _read_time_tracking_rows(storage_root: Path) -> list[dict[str, str]]:
    report_path = storage_root / "dashboard" / "time_tracking" / "time_tracking.csv"
    with report_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _read_time_tracking_dashboard_html(storage_root: Path) -> str:
    dashboard_path = storage_root / "dashboard" / "time_tracking" / "time_tracking_dashboard.html"
    return dashboard_path.read_text(encoding="utf-8")


def test_parse_args_defaults_to_dry_run() -> None:
    args = _parse_args([])
    assert args.apply is False
    assert args.dry_run is True


def test_backfill_apply_uses_explicit_clock_in_text_and_writes_provenance(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    user = UserProfile(
        user_key="aano",
        display_name="Aano",
        discord_user_id=1,
        discord_username="aano",
        storage_folder_name="AanoFolder",
        timezone="America/Los_Angeles",
    )
    session = SessionState(
        user_key="aano",
        session_date="2026-06-10",
        stage="clocked_out",
        clocked_in_at="2026-06-10T09:20:09-07:00",
        clocked_out_at="2026-06-10T16:34:58-07:00",
        work_segments=[{"clocked_in_at": "2026-06-10T09:20:09-07:00", "clocked_out_at": "2026-06-10T16:34:58-07:00"}],
    )
    session_path = _write_archived_session(runtime.bootstrap.storage_root_path, user, session)
    _append_message(runtime, user, session.session_date, message_id="m1", created_at="2026-06-10T09:20:09-07:00", content="I clocked in at 8:30")
    _append_message(runtime, user, session.session_date, message_id="m2", created_at="2026-06-10T16:34:58-07:00", content="im clocking out")

    summary = RetroHoursBackfiller(runtime).run(
        apply=True,
        now=datetime.fromisoformat("2026-06-15T09:00:00-07:00"),
    )

    updated = json.loads(session_path.read_text(encoding="utf-8"))
    explanation = json.loads((session_path.parent / "hours_backfill_explanation.json").read_text(encoding="utf-8"))

    assert summary["changed_days"] == 1
    assert updated["clocked_in_at"] == "2026-06-10T08:30:00-07:00"
    assert updated["metadata"]["retro_hours_backfill"]["clock_in_source"] == "explicit_clock_in_text"
    assert explanation["clock_in_source"] == "explicit_clock_in_text"
    assert explanation["clocked_in_after_seconds"] == 29098
    rows = _read_time_tracking_rows(runtime.bootstrap.storage_root_path)
    assert rows[0]["clocked_in_total_seconds"] == "29098"


def test_backfill_repairs_duplicate_segments_and_records_rebuild_warning(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=2,
        discord_username="andrew",
        storage_folder_name="AndrewFolder",
        timezone="America/Los_Angeles",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-06-04",
        stage="clocked_out",
        clocked_in_at="2026-06-04T09:05:47.921000-07:00",
        clocked_out_at="2026-06-05T03:25:46.102288-07:00",
        work_segments=[
            {"clocked_in_at": "2026-06-04T09:05:47.921000-07:00", "clocked_out_at": "2026-06-04T17:18:21.956000-07:00"},
            {"clocked_in_at": "2026-06-04T09:05:47.921000-07:00", "clocked_out_at": "2026-06-05T00:08:13.372195-07:00"},
            {"clocked_in_at": "2026-06-04T09:05:47.921000-07:00", "clocked_out_at": "2026-06-05T03:25:46.102288-07:00"},
        ],
        last_user_message_at="2026-06-04T17:18:21.956000-07:00",
        last_outbound_at="2026-06-04T23:13:16.790415-07:00",
    )
    session_path = _write_archived_session(runtime.bootstrap.storage_root_path, user, session)
    _append_message(runtime, user, session.session_date, message_id="m1", created_at="2026-06-04T09:05:47.921000-07:00", content="Yes I have don pollo")
    _append_message(runtime, user, session.session_date, message_id="m2", created_at="2026-06-04T17:18:21.956000-07:00", content="Im clocking out")

    RetroHoursBackfiller(runtime).run(
        apply=True,
        now=datetime.fromisoformat("2026-06-15T09:00:00-07:00"),
    )

    updated = json.loads(session_path.read_text(encoding="utf-8"))
    explanation = json.loads((session_path.parent / "hours_backfill_explanation.json").read_text(encoding="utf-8"))

    assert len(updated["work_segments"]) == 1
    assert updated["work_segments"][0]["clocked_out_at"] == "2026-06-04T17:18:21.956000-07:00"
    assert explanation["clock_out_source"] == "inbound_clock_out_intent"
    assert any("Stored work segments were rebuilt" in warning for warning in explanation["warnings"])


def test_backfill_clamps_historical_open_day_to_last_user_activity(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=3,
        discord_username="alex",
        storage_folder_name="AlexFolder",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-03",
        stage="active",
        clocked_in_at="2026-06-03T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-03T09:00:00-07:00", "clocked_out_at": None}],
        last_user_message_at="2026-06-03T15:00:00-07:00",
        last_outbound_at="2026-06-03T17:30:00-07:00",
    )
    session_path = _write_archived_session(runtime.bootstrap.storage_root_path, user, session)
    _append_message(runtime, user, session.session_date, message_id="m1", created_at="2026-06-03T09:00:00-07:00", content="yes")
    _append_message(runtime, user, session.session_date, message_id="m2", created_at="2026-06-03T15:00:00-07:00", content="Still working on the bracket")
    _append_message(runtime, user, session.session_date, message_id="m3", created_at="2026-06-03T17:30:00-07:00", content="follow up", direction="outbound")

    RetroHoursBackfiller(runtime).run(
        apply=True,
        now=datetime.fromisoformat("2026-06-15T09:00:00-07:00"),
    )

    updated = json.loads(session_path.read_text(encoding="utf-8"))
    explanation = json.loads((session_path.parent / "hours_backfill_explanation.json").read_text(encoding="utf-8"))

    assert updated["clocked_out_at"] == "2026-06-03T15:00:00-07:00"
    assert explanation["clock_out_source"] == "clamped_last_user_activity"
    assert any("clamped" in warning.lower() for warning in explanation["warnings"])


def test_backfill_lunch_windows_reduce_task_tracked_and_historical_clocked_hours(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    user = UserProfile(
        user_key="nick",
        display_name="Nick",
        discord_user_id=4,
        discord_username="nick",
        storage_folder_name="NickFolder",
    )
    session = SessionState(
        user_key="nick",
        session_date="2026-06-10",
        stage="clocked_out",
        clocked_in_at="2026-06-10T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-10T09:00:00-07:00", "clocked_out_at": None}],
        metadata={
            "clickup_time_tracking": {
                "task_id": "task-1",
                "task_name": "Build harness",
                "started_at": "2026-06-10T09:00:00-07:00",
            }
        },
    )
    session_path = _write_archived_session(runtime.bootstrap.storage_root_path, user, session)
    _append_message(runtime, user, session.session_date, message_id="m1", created_at="2026-06-10T09:00:00-07:00", content="yes")
    _append_message(runtime, user, session.session_date, message_id="m2", created_at="2026-06-10T12:00:00-07:00", content="taking lunch")
    _append_message(runtime, user, session.session_date, message_id="m3", created_at="2026-06-10T12:30:00-07:00", content="i'm back from lunch")
    _append_message(runtime, user, session.session_date, message_id="m4", created_at="2026-06-10T17:00:00-07:00", content="im clocking out")

    RetroHoursBackfiller(runtime).run(
        apply=True,
        now=datetime.fromisoformat("2026-06-15T09:00:00-07:00"),
    )

    updated = json.loads(session_path.read_text(encoding="utf-8"))
    explanation = json.loads((session_path.parent / "hours_backfill_explanation.json").read_text(encoding="utf-8"))

    assert updated["time_summary"]["gross_clocked_in_total_seconds"] == 28800
    assert updated["time_summary"]["unpaid_lunch_deducted_seconds"] == 1800
    assert updated["time_summary"]["clocked_in_total_seconds"] == 27000
    assert updated["time_summary"]["task_tracked_total_seconds"] == 27000
    assert len(explanation["lunch_windows"]) == 1
    assert len(explanation["task_windows"]) == 2
    rows = _read_time_tracking_rows(runtime.bootstrap.storage_root_path)
    assert rows[0]["task_tracked_total_seconds"] == "27000"


def test_backfill_without_explicit_task_tracking_keeps_task_time_zero(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    user = UserProfile(
        user_key="sam",
        display_name="Sam",
        discord_user_id=5,
        discord_username="sam",
        storage_folder_name="SamFolder",
    )
    session = SessionState(
        user_key="sam",
        session_date="2026-06-05",
        stage="clocked_out",
        clocked_in_at="2026-06-05T09:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-05T09:00:00-07:00", "clocked_out_at": "2026-06-05T12:00:00-07:00"}],
        metadata={
            "active_clickup_task_id": "task-2",
            "active_clickup_task_name": "General cleanup",
        },
    )
    session_path = _write_archived_session(runtime.bootstrap.storage_root_path, user, session)
    _append_message(runtime, user, session.session_date, message_id="m1", created_at="2026-06-05T09:00:00-07:00", content="yes")
    _append_message(runtime, user, session.session_date, message_id="m2", created_at="2026-06-05T12:00:00-07:00", content="im clocking out")

    RetroHoursBackfiller(runtime).run(
        apply=True,
        now=datetime.fromisoformat("2026-06-15T09:00:00-07:00"),
    )

    updated = json.loads(session_path.read_text(encoding="utf-8"))
    explanation = json.loads((session_path.parent / "hours_backfill_explanation.json").read_text(encoding="utf-8"))

    assert updated["time_summary"]["task_tracked_total_seconds"] == 0
    assert explanation["task_time_source"] == "none"
    assert "no explicit timer or task-boundary evidence" in explanation["explanation"]


def test_backfill_can_fall_back_to_transcript_markdown(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    user = UserProfile(
        user_key="ethan",
        display_name="Ethan",
        discord_user_id=6,
        discord_username="ethan",
        storage_folder_name="EthanFolder",
    )
    session = SessionState(
        user_key="ethan",
        session_date="2026-06-08",
        stage="clocked_out",
        clocked_in_at="2026-06-08T09:15:00-07:00",
        clocked_out_at="2026-06-08T16:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-08T09:15:00-07:00", "clocked_out_at": "2026-06-08T16:00:00-07:00"}],
    )
    session_path = _write_archived_session(runtime.bootstrap.storage_root_path, user, session)
    _write_transcript(
        user,
        runtime.bootstrap.storage_root_path,
        session.session_date,
        [
            ("2026-06-08 09:00:00", "Agent", "Good morning. Have you clocked in yet?"),
            ("2026-06-08 09:15:00", "Ethan", "I clocked in at 8:30"),
            ("2026-06-08 16:00:00", "Ethan", "im clocking out"),
        ],
    )

    RetroHoursBackfiller(runtime).run(
        apply=True,
        now=datetime.fromisoformat("2026-06-15T09:00:00-07:00"),
    )

    updated = json.loads(session_path.read_text(encoding="utf-8"))
    explanation = json.loads((session_path.parent / "hours_backfill_explanation.json").read_text(encoding="utf-8"))

    assert updated["clocked_in_at"] == "2026-06-08T08:30:00-07:00"
    assert "transcript_markdown" in explanation["sources_used"]
    assert explanation["clock_in_source"] == "explicit_clock_in_text"


def test_backfill_dry_run_leaves_archived_day_untouched_until_apply(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    user = UserProfile(
        user_key="dry",
        display_name="Dry",
        discord_user_id=7,
        discord_username="dry",
        storage_folder_name="DryFolder",
    )
    session = SessionState(
        user_key="dry",
        session_date="2026-06-09",
        stage="clocked_out",
        clocked_in_at="2026-06-09T09:20:00-07:00",
        clocked_out_at="2026-06-09T16:00:00-07:00",
        work_segments=[{"clocked_in_at": "2026-06-09T09:20:00-07:00", "clocked_out_at": "2026-06-09T16:00:00-07:00"}],
    )
    session_path = _write_archived_session(runtime.bootstrap.storage_root_path, user, session)
    original_text = session_path.read_text(encoding="utf-8")
    _append_message(runtime, user, session.session_date, message_id="m1", created_at="2026-06-09T09:20:00-07:00", content="I clocked in at 8:30")
    _append_message(runtime, user, session.session_date, message_id="m2", created_at="2026-06-09T16:00:00-07:00", content="im clocking out")

    dry_summary = RetroHoursBackfiller(runtime).run(
        apply=False,
        now=datetime.fromisoformat("2026-06-15T09:00:00-07:00"),
    )

    assert session_path.read_text(encoding="utf-8") == original_text
    assert not (session_path.parent / "hours_backfill_explanation.json").exists()
    assert Path(dry_summary["audit_csv_path"]).exists()
    dashboard_path = runtime.bootstrap.storage_root_path / "dashboard" / "time_tracking" / "time_tracking_dashboard.html"
    assert dashboard_path.exists()
    assert dry_summary["run_id"] in _read_time_tracking_dashboard_html(runtime.bootstrap.storage_root_path)

    apply_summary = RetroHoursBackfiller(runtime).run(
        apply=True,
        now=datetime.fromisoformat("2026-06-15T09:05:00-07:00"),
    )

    assert session_path.read_text(encoding="utf-8") != original_text
    assert (session_path.parent / "hours_backfill_explanation.json").exists()
    assert (runtime.bootstrap.storage_root_path / "dashboard" / "time_tracking" / "time_tracking.csv").exists()
    assert dashboard_path.exists()
    assert "Time Tracking Dashboard" in _read_time_tracking_dashboard_html(runtime.bootstrap.storage_root_path)
    assert apply_summary["changed_days"] == 1

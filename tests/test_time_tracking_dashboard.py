import csv
import json
import re
from pathlib import Path

from agent.time_tracking_dashboard import (
    build_time_tracking_dashboard_payload,
    render_time_tracking_dashboard_html,
    write_time_tracking_dashboard,
)


def _write_time_tracking_csv(storage_root: Path, rows: list[dict[str, str]]) -> Path:
    report_path = storage_root / "dashboard" / "time_tracking" / "time_tracking.csv"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "user_key",
                "display_name",
                "timezone",
                "session_date",
                "clocked_in_total_seconds",
                "clocked_in_total_human",
                "task_tracked_total_seconds",
                "task_tracked_total_human",
                "work_segment_count",
                "has_open_work_segment",
                "active_task_timer_running",
                "time_by_task_json",
                "session_path",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return report_path


def _write_audit_run(
    storage_root: Path,
    run_id: str,
    *,
    apply: bool,
    rows: list[dict[str, str]],
    explanations: dict[str, dict[str, object]],
) -> Path:
    run_root = storage_root / "dashboard" / "time_tracking" / "retro_backfill" / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    for explanation_path, payload in explanations.items():
        path = Path(explanation_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    audit_path = run_root / "audit.csv"
    with audit_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "user_key",
                "session_date",
                "changed",
                "confidence",
                "clocked_in_before_seconds",
                "clocked_in_after_seconds",
                "task_tracked_before_seconds",
                "task_tracked_after_seconds",
                "clock_in_source",
                "clock_out_source",
                "task_time_source",
                "explanation_path",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    (run_root / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "apply": apply,
                "processed_days": len(rows),
                "changed_days": len(rows),
                "unchanged_days": 0,
                "unresolved_days": sum(1 for row in rows if row["confidence"] == "unresolved"),
                "warnings_count": sum(len(payload.get("warnings", [])) for payload in explanations.values()),
                "skipped_days": 0,
                "audit_root": str(run_root.resolve()),
                "audit_csv_path": str(audit_path.resolve()),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return run_root


def _extract_payload(html: str) -> dict[str, object]:
    match = re.search(
        r'<script id="time-tracking-dashboard-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def test_dashboard_writer_embeds_hours_and_newest_audit_run(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    session_path = storage_root / "people" / "Alex Example" / "2026-06-11" / "session.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text("{}", encoding="utf-8")
    _write_time_tracking_csv(
        storage_root,
        [
            {
                "user_key": "alex",
                "display_name": "Alex Example",
                "timezone": "America/Los_Angeles",
                "session_date": "2026-06-11",
                "clocked_in_total_seconds": "27000",
                "clocked_in_total_human": "7h 30m",
                "task_tracked_total_seconds": "21600",
                "task_tracked_total_human": "6h",
                "work_segment_count": "1",
                "has_open_work_segment": "False",
                "active_task_timer_running": "False",
                "time_by_task_json": json.dumps(
                    [
                        {
                            "task_id": "task-1",
                            "task_name": "Firmware",
                            "seconds": 21600,
                            "human_duration": "6h",
                        }
                    ]
                ),
                "session_path": str(session_path.resolve()),
            }
        ],
    )
    explanation_path = (
        storage_root
        / "dashboard"
        / "time_tracking"
        / "retro_backfill"
        / "20260615-101700"
        / "day_explanations"
        / "Alex Example"
        / "2026-06-11"
        / "hours_backfill_explanation.json"
    )
    explanation_payload = {
        "display_name": "Alex Example",
        "explanation": "Clocked-in time starts from `explicit_clock_in_text`.",
        "warnings": ["Final open work segment was clamped to the latest reliable same-day activity."],
        "clock_in_at": "2026-06-11T08:30:00-07:00",
        "clock_out_at": "2026-06-11T16:00:00-07:00",
        "lunch_windows": [
            {
                "started_at": "2026-06-11T12:00:00-07:00",
                "ended_at": "2026-06-11T12:30:00-07:00",
                "source": "inbound_lunch_start_signal",
            }
        ],
        "task_windows": [
            {
                "task_id": "task-1",
                "task_name": "Firmware",
                "started_at": "2026-06-11T08:45:00-07:00",
                "ended_at": "2026-06-11T15:45:00-07:00",
                "duration_seconds": 25200,
            }
        ],
    }
    _write_audit_run(
        storage_root,
        "20260615-090000",
        apply=True,
        rows=[
            {
                "user_key": "alex",
                "session_date": "2026-06-10",
                "changed": "True",
                "confidence": "medium",
                "clocked_in_before_seconds": "28800",
                "clocked_in_after_seconds": "27000",
                "task_tracked_before_seconds": "21600",
                "task_tracked_after_seconds": "21600",
                "clock_in_source": "state_machine_clocked_in_at",
                "clock_out_source": "inbound_clock_out_intent",
                "task_time_source": "stored_time_entries",
                "explanation_path": str(
                    (
                        storage_root
                        / "dashboard"
                        / "time_tracking"
                        / "retro_backfill"
                        / "20260615-090000"
                        / "day_explanations"
                        / "Alex Example"
                        / "2026-06-10"
                        / "hours_backfill_explanation.json"
                    ).resolve()
                ),
            }
        ],
        explanations={
            str(
                (
                    storage_root
                    / "dashboard"
                    / "time_tracking"
                    / "retro_backfill"
                    / "20260615-090000"
                    / "day_explanations"
                    / "Alex Example"
                    / "2026-06-10"
                    / "hours_backfill_explanation.json"
                ).resolve()
            ): {
                "display_name": "Alex Example",
                "explanation": "Older run.",
                "warnings": [],
                "clock_in_at": "2026-06-10T09:00:00-07:00",
                "clock_out_at": "2026-06-10T16:00:00-07:00",
                "lunch_windows": [],
                "task_windows": [],
            }
        },
    )
    _write_audit_run(
        storage_root,
        "20260615-101700",
        apply=False,
        rows=[
            {
                "user_key": "alex",
                "session_date": "2026-06-11",
                "changed": "True",
                "confidence": "low",
                "clocked_in_before_seconds": "30000",
                "clocked_in_after_seconds": "27000",
                "task_tracked_before_seconds": "24000",
                "task_tracked_after_seconds": "21600",
                "clock_in_source": "explicit_clock_in_text",
                "clock_out_source": "clamped_last_user_activity",
                "task_time_source": "stored_time_entries",
                "explanation_path": str(explanation_path.resolve()),
            }
        ],
        explanations={str(explanation_path.resolve()): explanation_payload},
    )

    dashboard_path = write_time_tracking_dashboard(storage_root)

    assert dashboard_path is not None
    html = dashboard_path.read_text(encoding="utf-8")
    assert "Hours Rollup" in html
    assert "Backfill Audit" in html
    assert "Read-only snapshot." in html
    assert ".venv/bin/python -m agent.hours_editor" in html
    assert "http://127.0.0.1:8765/" in html
    assert "Static snapshot mode" in html
    assert 'id="intern-filter"' in html
    assert 'id="audit-run-filter"' in html
    assert 'id="confidence-filter"' in html
    assert 'id="review-filter"' in html
    assert 'id="editor-panel"' not in html
    assert 'data-default-audit-run-id="20260615-101700"' in html

    payload = _extract_payload(html)
    assert payload["default_audit_run_id"] == "20260615-101700"
    assert [run["run_id"] for run in payload["audit_runs"]] == ["20260615-101700", "20260615-090000"]
    assert payload["hours_rows"][0]["time_by_task"][0]["task_name"] == "Firmware"
    latest_row = payload["audit_runs"][0]["rows"][0]
    assert latest_row["display_name"] == "Alex Example"
    assert latest_row["warnings"][0].startswith("Final open work segment")
    assert latest_row["task_windows"][0]["task_name"] == "Firmware"
    assert "%20" in latest_row["explanation_uri"]
    assert "%20" in latest_row["session_uri"]
    hours_row = payload["hours_rows"][0]
    assert hours_row["review_status"] == "needs_review"
    assert hours_row["latest_audit_confidence"] == "low"
    assert any("low confidence" in reason.lower() for reason in hours_row["review_reasons"])


def test_editor_mode_html_embeds_live_refresh_metadata(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    _write_time_tracking_csv(
        storage_root,
        [
            {
                "user_key": "alex",
                "display_name": "Alex Example",
                "timezone": "America/Los_Angeles",
                "session_date": "2026-06-11",
                "clocked_in_total_seconds": "27000",
                "clocked_in_total_human": "7h 30m",
                "task_tracked_total_seconds": "21600",
                "task_tracked_total_human": "6h",
                "work_segment_count": "1",
                "has_open_work_segment": "False",
                "active_task_timer_running": "False",
                "time_by_task_json": "[]",
                "session_path": "",
            }
        ],
    )

    payload = build_time_tracking_dashboard_payload(
        storage_root,
        editor_mode=True,
    )
    html = render_time_tracking_dashboard_html(payload, editor_mode=True)

    assert "Local editor server mode." in html
    assert "http://127.0.0.1:8765/" in html
    assert "Live auto-refresh every 10 minutes" in html
    assert "setInterval(async () =>" in html
    assert "Live sync paused while editing" in html

    embedded_payload = _extract_payload(html)
    assert embedded_payload["editor_mode"] is True
    assert embedded_payload["auto_refresh_interval_ms"] == 600000
    assert embedded_payload["live_dashboard_url"] == "http://127.0.0.1:8765/"


def test_dashboard_writer_renders_empty_audit_state_when_no_runs_exist(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"

    dashboard_path = write_time_tracking_dashboard(storage_root)

    assert dashboard_path is not None
    html = dashboard_path.read_text(encoding="utf-8")
    assert "No retro backfill audit runs have been generated yet." in html
    payload = _extract_payload(html)
    assert payload["audit_runs"] == []
    assert payload["default_audit_run_id"] == ""


def test_dashboard_writer_keeps_audit_rows_when_explanations_are_missing(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    missing_path = (
        storage_root
        / "dashboard"
        / "time_tracking"
        / "retro_backfill"
        / "20260615-111500"
        / "day_explanations"
        / "Sam Example"
        / "2026-06-14"
        / "hours_backfill_explanation.json"
    )
    _write_audit_run(
        storage_root,
        "20260615-111500",
        apply=False,
        rows=[
            {
                "user_key": "sam",
                "session_date": "2026-06-14",
                "changed": "True",
                "confidence": "unresolved",
                "clocked_in_before_seconds": "0",
                "clocked_in_after_seconds": "0",
                "task_tracked_before_seconds": "0",
                "task_tracked_after_seconds": "0",
                "clock_in_source": "unresolved",
                "clock_out_source": "unresolved",
                "task_time_source": "none",
                "explanation_path": str(missing_path.resolve()),
            }
        ],
        explanations={},
    )

    dashboard_path = write_time_tracking_dashboard(storage_root)

    assert dashboard_path is not None
    payload = _extract_payload(dashboard_path.read_text(encoding="utf-8"))
    row = payload["audit_runs"][0]["rows"][0]
    assert row["user_key"] == "sam"
    assert row["detail_load_error"] == "Explanation file is missing or unreadable."
    assert row["confidence"] == "unresolved"

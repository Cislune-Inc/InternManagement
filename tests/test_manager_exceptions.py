from __future__ import annotations

from agent.manager_exceptions import _person_exceptions, render_manager_exceptions_html


def test_manager_queue_detects_workflow_task_and_time_exceptions():
    person = {
        "display_name": "Alex Example",
        "user_key": "alex",
        "current_session_date": "2026-07-30",
        "current_stage": "awaiting_clock_out_artifacts",
        "clock_state": "clocked in",
        "latest_blocker": "Waiting for the replacement circuit.",
        "pending_admin_review_count": 1,
        "portal_quality_restart_blocked": {
            "clocked_out_at": "2026-07-30T10:00:00-07:00",
            "status": "manager_approval_required",
            "reasons": ["Repeated checkpoint text"],
        },
        "active_task_id": "task-a",
        "active_task_name": "Firmware",
        "active_timer_task_id": "task-b",
        "active_timer_task_name": "Mechanical",
        "days": [{"clocked_in_total_seconds": 11 * 60 * 60}],
    }

    rows = _person_exceptions(person)
    categories = {row["category"] for row in rows}

    assert categories == {
        "incomplete_clock_out",
        "active_blocker",
        "admin_review",
        "quality_restart_approval",
        "task_timer_mismatch",
        "long_shift",
    }


def test_manager_queue_html_has_filters_and_work_correction_link():
    html = render_manager_exceptions_html(
        {
            "exceptions": [
                {
                    "severity": "warning",
                    "category": "missing_active_task",
                    "summary": "No active task.",
                    "person": "Alex",
                    "user_key": "alex",
                    "session_date": "2026-07-30",
                    "details": {"stage": "active"},
                }
            ]
        }
    )

    assert "Manager Exception Queue" in html
    assert "All categories" in html
    assert "Review worker and correct ClickUp task" in html

from __future__ import annotations

import runpy
from pathlib import Path


MODULE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "ops" / "print-health-summary.py"),
    run_name="health_summary",
)
summarize_health = MODULE["summarize_health"]


def test_health_summary_keeps_only_actionable_operational_fields() -> None:
    summary = summarize_health(
        {
            "overall_status": "warning",
            "open_issue_count": 4,
            "generated_at": "2026-08-06T23:00:00+00:00",
            "bot": {"running": True, "pid": 42, "lock_path": "/secret/path"},
            "dashboard": {"running": True, "url": "http://127.0.0.1:8765"},
            "backup": {"healthy": True, "age_human": "0h old", "path": "/secret"},
            "integration_health": {
                "overall_status": "warning",
                "checks": [
                    {"name": "slack", "status": "ok", "details": {"token": "no"}},
                    {"name": "clickup", "status": "warning", "error": "timeout"},
                ],
            },
            "issues": [{"details": {"private": "not returned"}}],
        }
    )

    assert summary == {
        "overall_status": "warning",
        "bot_running": True,
        "bot_pid": 42,
        "dashboard_running": True,
        "backup_healthy": True,
        "backup_age": "0h old",
        "integrations_status": "warning",
        "failing_checks": ["clickup"],
        "open_issue_count": 4,
        "generated_at": "2026-08-06T23:00:00+00:00",
    }

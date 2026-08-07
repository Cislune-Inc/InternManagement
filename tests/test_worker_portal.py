from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from agent.models import AdminProfile, SlackConfig
from agent.state_store import StateStore
from agent.worker_portal import (
    WorkerPortalService,
    build_worker_portal_link,
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
            }
            for index in range(12)
        ]


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
            slack=SlackConfig(manager_queue_url="http://192.168.4.87:8765/exceptions")
        ),
        clickup=_FakeClickUp(),
        slack=slack,
        admin_profile_by_slack_user_id=lambda slack_user_id: admin if slack_user_id == "UERIK" else None,
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
    assert initial["work"]["status"] == "ready"

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
    assert result["task_requests"][0]["status"] == "beta pending"
    assert slack.messages[0][0] == "UERIK"
    assert "no ClickUp task created" in slack.messages[0][1]

    asyncio.run(service.apply_action(token, {"action": "share_slack"}))
    assert len(slack.messages) == 2
    assert "preview only" in slack.messages[1][1]

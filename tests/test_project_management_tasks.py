from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agent.models import AdminProfile
from agent.project_management_tasks import MANAGEMENT_TASK_TITLE, seed_project_management_tasks


class _ClickUp:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def list_workspace_tasks(self, limit=100, include_closed=False):
        assert limit == 2000
        assert include_closed is False
        return [
            {
                "id": "source-1",
                "name": "Technical work",
                "folder": {"name": "CisluneCUspace"},
                "list": {"id": "list-nightjar", "name": "LM_Nightjar"},
            },
            {
                "id": "source-2",
                "name": MANAGEMENT_TASK_TITLE,
                "folder": {"name": "TREAD"},
                "list": {"id": "list-tread", "name": "TREAD PM"},
            },
        ]

    async def resolve_workspace_member_id(self, *, name=None, email=None):
        return {"Erik": "101", "George": "202"}.get(name)

    async def create_task(self, list_id, **kwargs):
        self.created.append({"list_id": list_id, **kwargs})
        return {"id": "new-task", "url": "https://app.clickup.com/t/new-task"}


def _runtime():
    clickup = _ClickUp()
    admins = [
        AdminProfile(name="Erik", discord_user_id=1, clickup_user_id="101", slack_user_id="U1"),
        AdminProfile(name="George", discord_user_id=2, clickup_user_id="202", slack_user_id="U2"),
    ]
    runtime = SimpleNamespace(
        clickup=clickup,
        config=SimpleNamespace(
            clickup=SimpleNamespace(new_task_approver_names=["Erik", "George"])
        ),
        admin_profiles=lambda: admins,
    )
    return runtime, clickup


def test_management_task_seed_is_deduplicated_and_notifies_before_create() -> None:
    runtime, clickup = _runtime()
    notifications: list[list[dict[str, str]]] = []

    async def notify(planned):
        assert clickup.created == []
        notifications.append(planned)

    result = asyncio.run(
        seed_project_management_tasks(runtime, apply=True, notify_before_apply=notify)
    )

    assert len(notifications) == 1
    assert len(result["created"]) == 1
    assert result["created"][0]["list"] == "LM_Nightjar"
    assert result["skipped"] == [
        {"contract": "TREAD", "list": "TREAD PM", "reason": "existing open task"}
    ]
    assert clickup.created[0]["assignee_ids"] == ["101", "202"]
    assert "billing and labor-allocation support" in clickup.created[0]["description"]


def test_management_task_seed_dry_run_never_writes() -> None:
    runtime, clickup = _runtime()
    result = asyncio.run(seed_project_management_tasks(runtime, apply=False))

    assert result["applied"] is False
    assert len(result["planned"]) == 1
    assert clickup.created == []

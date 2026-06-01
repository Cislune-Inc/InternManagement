import asyncio
from datetime import datetime

from agent.clickup_client import ClickUpClient
from agent.models import (
    AgentConfig,
    ClickUpConfig,
    MessageRecord,
    PromptConfig,
    ScheduleConfig,
    SessionState,
    UserProfile,
)


class FakeClickUpClient(ClickUpClient):
    def __init__(self, responses: dict[tuple[str, str], dict]) -> None:
        super().__init__("token", _build_config())
        self.responses = responses
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []

    def _request(self, method: str, path: str, *, params=None, json=None) -> dict:
        self.calls.append((method, path, params, json))
        return self.responses[(method, path)]


class ParamAwareClickUpClient(FakeClickUpClient):
    def _request(self, method: str, path: str, *, params=None, json=None) -> dict:
        self.calls.append((method, path, params, json))
        if method == "GET" and path == "/team/9011286053/task":
            if params and params.get("assignees[]"):
                return {"tasks": []}
            return {
                "tasks": [
                    {
                        "id": "task-open",
                        "name": "Recovered from workspace scan",
                        "status": {"status": "to do", "type": "open"},
                        "assignees": [{"id": 87439461, "username": "Navin Nagavel"}],
                        "list": {"id": "901113819433"},
                    }
                ]
            }
        return self.responses[(method, path)]


def _build_config() -> AgentConfig:
    return AgentConfig(
        timezone="America/Los_Angeles",
        admin_discord_user_id=1,
        roster_file_name="roster.csv",
        dashboard_file_name="dashboard.md",
        schedule=ScheduleConfig(),
        clickup=ClickUpConfig(workspace_id="9011286053", mission_board_list_id="mission-board"),
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


def test_clickup_context_prefers_matching_assigned_task() -> None:
    client = FakeClickUpClient(
        {
            (
                "GET",
                "/team/9011286053/task",
            ): {
                "tasks": [
                    {
                        "id": "task-1",
                        "name": "Marketing page cleanup",
                        "description": "Polish the landing page",
                        "status": {"status": "to do"},
                        "priority": {"priority": "high"},
                    },
                    {
                        "id": "task-2",
                        "name": "Intake form API integration",
                        "description": "Wire the API, validation, and payload mapping",
                        "status": {"status": "in progress"},
                        "priority": {"priority": "normal"},
                    },
                ]
            }
        }
    )
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
        clickup_user_id="87438366",
    )
    session = SessionState(
        user_key="andrew",
        session_date="2026-05-28",
        latest_plan="Finish the intake form UI and wire the API",
    )
    messages = [
        MessageRecord(
            message_id="1",
            direction="inbound",
            author_id=1,
            created_at=datetime(2026, 5, 28, 9, 0, 0),
            content="I am wiring the API and payload mapping now.",
            attachments=[],
        )
    ]
    bundle = asyncio.run(client.get_context_bundle(user, session, messages))
    assert bundle.active_task_id == "task-2"
    assert bundle.active_task_name == "Intake form API integration"
    assert "Active ClickUp task candidate" in bundle.context


def test_clickup_user_id_can_be_resolved_from_workspace_members() -> None:
    client = FakeClickUpClient(
        {
            (
                "GET",
                "/team",
            ): {
                "teams": [
                    {
                        "id": "9011286053",
                        "members": [
                            {
                                "user": {
                                    "id": 87438366,
                                    "username": "Andrew Ore",
                                    "email": "alore@cpp.edu",
                                }
                            }
                        ],
                    }
                ]
            }
        }
    )
    user = UserProfile(
        user_key="Andrew",
        display_name="Andore482",
        discord_user_id=1,
        discord_username="andore482",
        storage_folder_name="AndrewOre",
        clickup_user_email="alore@cpp.edu",
    )
    resolved = asyncio.run(client.resolve_clickup_user_id(user))
    assert resolved == "87438366"


def test_workspace_member_id_can_be_resolved_from_partial_admin_name() -> None:
    client = FakeClickUpClient(
        {
            (
                "GET",
                "/team",
            ): {
                "teams": [
                    {
                        "id": "9011286053",
                        "members": [
                            {
                                "user": {
                                    "id": 198031927,
                                    "username": "George Ore",
                                    "email": "george@cislune.com",
                                }
                            }
                        ],
                    }
                ]
            }
        }
    )
    resolved = asyncio.run(
        client.resolve_workspace_member_id(
            name="George",
            email="george@cislune.com",
        )
    )
    assert resolved == "198031927"


def test_set_task_state_resolves_list_status_name() -> None:
    client = FakeClickUpClient(
        {
            ("GET", "/task/task-2"): {
                "id": "task-2",
                "name": "Intake form API integration",
                "status": {"status": "to do"},
                "list": {"id": "list-1"},
            },
            ("GET", "/list/list-1"): {
                "statuses": [
                    {"status": "to do"},
                    {"status": "in progress"},
                    {"status": "hold"},
                    {"status": "complete"},
                ]
            },
            ("PUT", "/task/task-2"): {},
        }
    )
    status_name = asyncio.run(client.set_task_state("task-2", "in_progress"))
    assert status_name == "in progress"
    assert ("PUT", "/task/task-2", None, {"status": "in progress"}) in client.calls


def test_post_update_requires_explicit_task_id() -> None:
    client = FakeClickUpClient({})
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
        clickup_user_id="87438366",
    )
    result = asyncio.run(
        client.post_update(
            user,
            None,
            comment_text="Update",
            summary="Summary",
            session_timestamp_ms=1234,
        )
    )
    assert result is None
    assert client.calls == []


def test_create_task_uses_mission_board_payload() -> None:
    client = FakeClickUpClient(
        {
            ("POST", "/list/mission-board/task"): {
                "id": "new-task",
                "name": "Need fixture dimensions from PM",
            }
        }
    )
    created = asyncio.run(
        client.create_task(
            "mission-board",
            name="Need fixture dimensions from PM",
            description="Blocked waiting on measurements.",
            priority="high",
            due_date=1772496000000,
            tags=["blocker", "andrew"],
        )
    )
    assert created["id"] == "new-task"
    assert (
        "POST",
        "/list/mission-board/task",
        None,
        {
            "name": "Need fixture dimensions from PM",
            "description": "Blocked waiting on measurements.",
            "notify_all": False,
            "priority": 2,
            "due_date": 1772496000000,
            "due_date_time": False,
            "tags": ["blocker", "andrew"],
        },
    ) in client.calls


def test_create_task_repairs_missing_assignees_after_create() -> None:
    client = FakeClickUpClient(
        {
            ("POST", "/list/mission-board/task"): {
                "id": "new-task",
                "name": "Need fixture dimensions from PM",
                "assignees": [],
            },
            ("PUT", "/task/new-task"): {},
            ("GET", "/task/new-task"): {
                "id": "new-task",
                "name": "Need fixture dimensions from PM",
                "assignees": [{"id": 198031927}],
            },
        }
    )
    created = asyncio.run(
        client.create_task(
            "mission-board",
            name="Need fixture dimensions from PM",
            description="Blocked waiting on measurements.",
            assignee_ids=["198031927"],
            priority="high",
        )
    )
    assert created["assignees"] == [{"id": 198031927}]
    assert (
        "PUT",
        "/task/new-task",
        None,
        {
            "assignees": {
                "add": [198031927],
                "rem": [],
            }
        },
    ) in client.calls


def test_suggest_next_tasks_prefers_unassigned_priority_matches() -> None:
    client = FakeClickUpClient(
        {
            ("GET", "/list/mission-board/task"): {
                "tasks": [
                    {
                        "id": "task-1",
                        "name": "Fixture documentation cleanup",
                        "description": "",
                        "assignees": [],
                        "status": {"status": "to do", "type": "open"},
                        "priority": {"priority": "high"},
                    },
                    {
                        "id": "task-2",
                        "name": "PERDEX wiring harness validation",
                        "description": "Validate harness routing and measurements",
                        "assignees": [],
                        "status": {"status": "to do", "type": "open"},
                        "priority": {"priority": "urgent"},
                    },
                    {
                        "id": "task-3",
                        "name": "Assigned task already owned",
                        "description": "",
                        "assignees": [{"id": 10}],
                        "status": {"status": "to do", "type": "open"},
                        "priority": {"priority": "urgent"},
                    },
                ]
            }
        }
    )
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
        latest_plan="Finish PERDEX wiring harness validation",
    )
    messages = [
        MessageRecord(
            message_id="1",
            direction="inbound",
            author_id=1,
            created_at=datetime(2026, 5, 28, 17, 0, 0),
            content="I wrapped up the wiring harness work and want a related next task.",
            attachments=[],
        )
    ]
    suggestions = asyncio.run(client.suggest_next_tasks(user, session, messages, limit=2))
    assert [task["id"] for task in suggestions] == ["task-2", "task-1"]


def test_resolve_task_for_user_matches_by_name_and_id() -> None:
    client = FakeClickUpClient(
        {
            ("GET", "/team/9011286053/task"): {
                "tasks": [
                    {
                        "id": "task-1",
                        "name": "Formalize project tree",
                        "description": "",
                        "status": {"status": "to do"},
                        "priority": {"priority": "high"},
                    },
                    {
                        "id": "task-2",
                        "name": "Battery mount fit check",
                        "description": "",
                        "status": {"status": "to do"},
                        "priority": {"priority": "normal"},
                    },
                ]
            }
        }
    )
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
        clickup_user_id="87438366",
    )
    by_name = asyncio.run(client.resolve_task_for_user(user, "formalize project tree"))
    by_id = asyncio.run(client.resolve_task_for_user(user, "task-2"))
    assert by_name is not None
    assert by_name["id"] == "task-1"
    assert by_id is not None
    assert by_id["name"] == "Battery mount fit check"


def test_list_assigned_tasks_filters_closed_tasks() -> None:
    client = FakeClickUpClient(
        {
            ("GET", "/team/9011286053/task"): {
                "tasks": [
                    {
                        "id": "task-open",
                        "name": "Still active",
                        "status": {"status": "to do", "type": "open"},
                    },
                    {
                        "id": "task-closed",
                        "name": "Already closed",
                        "status": {"status": "complete", "type": "closed"},
                    },
                ]
            }
        }
    )
    user = UserProfile(
        user_key="andrew",
        display_name="Andrew",
        discord_user_id=1,
        discord_username="andrew",
        storage_folder_name="AndrewOre",
        clickup_user_id="87438366",
    )
    tasks = asyncio.run(client.list_assigned_tasks(user))
    assert [task["id"] for task in tasks] == ["task-open"]


def test_list_assigned_tasks_falls_back_to_workspace_scan_when_filtered_query_is_empty() -> None:
    client = ParamAwareClickUpClient(
        {
            ("GET", "/team"): {
                "teams": [
                    {
                        "id": "9011286053",
                        "members": [
                            {
                                "user": {
                                    "id": 87439461,
                                    "username": "Navin Nagavel",
                                    "email": "home4nav@gmail.com",
                                }
                            }
                        ],
                    }
                ]
            }
        }
    )
    user = UserProfile(
        user_key="Navin",
        display_name="Navin",
        discord_user_id=1,
        discord_username="thunderlasso648",
        storage_folder_name="NavinNagavel",
        clickup_user_id="87439461",
    )
    tasks = asyncio.run(client.list_assigned_tasks(user))
    assert [task["id"] for task in tasks] == ["task-open"]


def test_update_task_assignees_uses_add_payload() -> None:
    client = FakeClickUpClient(
        {
            ("PUT", "/task/task-1"): {},
        }
    )
    asyncio.run(client.update_task_assignees("task-1", add_user_ids=["87438366"]))
    assert (
        "PUT",
        "/task/task-1",
        None,
        {
            "assignees": {
                "add": [87438366],
                "rem": [],
            }
        },
    ) in client.calls

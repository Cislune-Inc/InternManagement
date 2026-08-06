import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from agent.models import (
    AdminProfile,
    AgentConfig,
    AttachmentRecord,
    ClickUpConfig,
    MessageRecord,
    PromptConfig,
    ScheduleConfig,
    SessionState,
    SlackConfig,
    SlackProjectRoute,
    UserProfile,
)
from agent.runtime import InternManagementRuntime
from agent.slack_update_policy import SlackUpdatePolicy


class _FakeSlack:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str | None]] = []
        self.uploads: list[tuple[str, Path, str, str, str | None]] = []
        self.reactions: dict[tuple[str, str], list[dict[str, object]]] = {}

    async def post_message(
        self,
        channel_id: str,
        message: str,
        *,
        thread_ts: str | None = None,
    ) -> dict[str, object]:
        self.messages.append((channel_id, message, thread_ts))
        return {"ok": True, "ts": f"{len(self.messages)}.000"}

    async def upload_file(
        self,
        channel_id: str,
        file_path: Path,
        *,
        title: str,
        initial_comment: str = "",
        thread_ts: str | None = None,
    ) -> dict[str, object]:
        self.uploads.append((channel_id, file_path, title, initial_comment, thread_ts))
        ts = f"file-{len(self.uploads)}.000"
        return {
            "ok": True,
            "files": [
                {
                    "shares": {
                        "public": {
                            channel_id: [{"ts": ts}]
                        }
                    }
                }
            ],
        }

    async def get_reactions(self, channel_id: str, message_ts: str) -> list[dict[str, object]]:
        return self.reactions.get((channel_id, message_ts), [])


def test_slack_policy_rejects_vague_progress_and_break_commands() -> None:
    policy = SlackUpdatePolicy(lambda _text: None)

    assert policy.is_interesting("Project is going good.") is False
    assert policy.is_interesting("Put me on break pollo") is False
    assert policy.is_interesting("Working on it for the project") is False
    assert policy.is_interesting(
        "Installed the revised sensor bracket and measured a 3 mm clearance."
    ) is True


class _FakeClickUp:
    async def get_task(self, _task_id: str) -> dict[str, object]:
        return {
            "id": "task-1",
            "name": "Solar wifi dongle diagnostics",
            "list": {"id": "list-1", "name": "PERDEX Solar"},
        }


class _FakeAncestryClickUp:
    def __init__(self) -> None:
        self.tasks = {
            "leaf": {
                "id": "leaf",
                "name": "V2 CAD",
                "parent": "carve",
                "list": {"id": "mission-board", "name": "Mission Board"},
                "folder": {"id": "intern-folder", "name": "Summer 2026 Interns"},
            },
            "carve": {
                "id": "carve",
                "name": "CARVE",
                "parent": "grasp",
                "list": {"id": "mission-board", "name": "Mission Board"},
                "folder": {"id": "intern-folder", "name": "Summer 2026 Interns"},
            },
            "grasp": {
                "id": "grasp",
                "name": "GRASP",
                "parent": None,
                "list": {"id": "mission-board", "name": "Mission Board"},
                "folder": {"id": "intern-folder", "name": "Summer 2026 Interns"},
            },
        }

    async def get_task(self, task_id: str) -> dict[str, object]:
        return self.tasks[task_id]


def _build_runtime(tmp_path: Path) -> InternManagementRuntime:
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
        schedule=ScheduleConfig(follow_up_interval_minutes=30),
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
        slack=SlackConfig(
            enabled=True,
            default_channel_id="CDEFAULT",
            unmapped_channel_id="CUNMAPPED",
            post_start_hour=10,
            post_end_hour=17,
            min_post_interval_minutes=25,
            max_images_per_update=2,
            project_routes=[
                SlackProjectRoute(
                    label="PERDEX Solar",
                    channel_id="CPERDEX",
                    clickup_list_ids=["list-1"],
                )
            ],
        ),
        admins=[AdminProfile(name="Admin", discord_user_id=1)],
    )
    runtime.roster_by_key = {}
    runtime.slack = _FakeSlack()
    runtime.clickup = _FakeClickUp()
    runtime.state_store = SimpleNamespace(list_messages=lambda _user_key, _session_date: [])
    return runtime


def test_slack_daily_update_posts_interesting_summary_and_new_image(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    image_path = tmp_path / "solar-progress.jpg"
    image_path.write_bytes(b"fake image")
    user = UserProfile(
        user_key="nick",
        display_name="Nick",
        discord_user_id=1,
        discord_username="nick",
        storage_folder_name="Nick",
        slack_user_id="U123",
    )
    session = SessionState(
        user_key="nick",
        session_date="2026-07-13",
        stage="active",
        clocked_in_at="2026-07-13T09:00:00-07:00",
        latest_status="Nick and Connor got the solar setup talking through the wifi dongle and started comparing daily output.",
        latest_plan="Next is diagnosing how much solar is generated per day under the current setup.",
        metadata={"active_clickup_task_id": "task-1", "active_clickup_task_name": "Solar wifi diagnostics"},
    )
    message = MessageRecord(
        message_id="m1",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-07-13T11:00:00-07:00"),
        content="The wifi dongle is connected and we can start checking solar per day.",
        attachments=[
            AttachmentRecord(
                filename="solar-progress.jpg",
                url="https://example.com/solar-progress.jpg",
                content_type="image/jpeg",
                size=10,
                local_path=str(image_path),
                description="Solar controller with wifi dongle attached and status LEDs visible.",
                tags=["solar", "wifi"],
            )
        ],
    )
    runtime.state_store = SimpleNamespace(list_messages=lambda _user_key, _session_date: [message])

    posted = asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-13T11:15:00-07:00"),
        )
    )

    assert posted is True
    assert runtime.slack.messages[0][0] == "CPERDEX"
    assert "<@U123> update" in runtime.slack.messages[0][1]
    assert "Solar wifi diagnostics" in runtime.slack.messages[0][1]
    assert "https://app.clickup.com/t/task-1" in runtime.slack.messages[0][1]
    assert "What changed" in runtime.slack.messages[0][1]
    assert len(runtime.slack.uploads) == 1
    assert runtime.slack.uploads[0][1] == image_path
    assert runtime.slack.uploads[0][4] == "1.000"
    state = runtime._load_slack_update_state()
    assert state["daily_updates"]["nick"]["2026-07-13"]["channel_id"] == "CPERDEX"
    assert state["posted_images"][0]["message_ts"] == "file-1.000"


def test_slack_daily_update_handles_legacy_naive_message_timestamps(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    user = UserProfile(
        user_key="nick",
        display_name="Nick",
        discord_user_id=1,
        discord_username="nick",
        storage_folder_name="Nick",
        slack_user_id="U123",
    )
    session = SessionState(
        user_key="nick",
        session_date="2026-07-13",
        stage="active",
        clocked_in_at="2026-07-13T09:00:00-07:00",
        latest_status="The solar wifi diagnostic rig is logging useful voltage output now.",
        metadata={"active_clickup_task_id": "task-1", "active_clickup_task_name": "Solar wifi diagnostics"},
    )
    runtime._write_slack_update_state(
        {
            "daily_updates": {
                "nick": {
                    "2026-07-13": {
                        "posted_at": "2026-07-13T10:00:00-07:00",
                    }
                }
            }
        }
    )
    message = MessageRecord(
        message_id="m1",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-07-13T11:00:00"),
        content="The wifi dongle is now sending daily solar output readings.",
    )
    runtime.state_store = SimpleNamespace(list_messages=lambda _user_key, _session_date: [message])

    posted = asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-13T11:30:00-07:00"),
        )
    )

    assert posted is True
    assert runtime.slack.messages[0][0] == "CPERDEX"


def test_slack_daily_update_ignores_invalid_route_regex(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    runtime.config.slack.project_routes = [
        SlackProjectRoute(
            label="Broken",
            channel_id="CBROKEN",
            task_name_patterns=["["],
        )
    ]
    user = UserProfile(
        user_key="nick",
        display_name="Nick",
        discord_user_id=1,
        discord_username="nick",
        storage_folder_name="Nick",
    )
    session = SessionState(
        user_key="nick",
        session_date="2026-07-13",
        stage="active",
        clocked_in_at="2026-07-13T09:00:00-07:00",
        latest_status="The controller enclosure has a cleaner mounting path after today's test fit.",
        metadata={"active_clickup_task_id": "task-1", "active_clickup_task_name": "Controller enclosure"},
    )

    posted = asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-13T12:00:00-07:00"),
        )
    )

    assert posted is False
    assert runtime.slack.messages == []
    state = runtime._load_slack_update_state()
    assert state["unmapped_updates"][0]["active_task_name"] == "Controller enclosure"


def test_slack_route_uses_parent_task_contract_before_broader_ancestor(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    runtime.clickup = _FakeAncestryClickUp()
    runtime.config.slack.project_routes = [
        SlackProjectRoute(
            label="CARVE",
            channel_id="CCARVE",
            task_name_patterns=[r"\bCARVE\b"],
        ),
        SlackProjectRoute(
            label="GRASP",
            channel_id="CGRASP",
            clickup_task_ids=["grasp"],
        ),
    ]
    user = UserProfile(
        user_key="ali",
        display_name="Ali",
        discord_user_id=1,
        discord_username="ali",
        storage_folder_name="Ali",
    )
    session = SessionState(
        user_key="ali",
        session_date="2026-07-13",
        metadata={"active_clickup_task_id": "leaf", "active_clickup_task_name": "V2 CAD"},
    )

    channel_id, label, uncertain = asyncio.run(
        runtime._resolve_slack_daily_channel(user, session)
    )

    assert channel_id == "CCARVE"
    assert label == "CARVE"
    assert uncertain is False


def test_slack_route_uses_strong_message_evidence_over_stale_task_ancestry(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    runtime.clickup = SimpleNamespace(
        get_task=lambda task_id: asyncio.sleep(
            0,
            result={
                "id": task_id,
                "name": "HL SW",
                "parent": None,
                "list": {"id": "mission-board", "name": "Mission Board"},
            },
        )
    )
    runtime.config.slack.project_routes = [
        SlackProjectRoute(
            label="PERDEX",
            channel_id="CPERDEX",
            clickup_task_ids=["perdex-task"],
        ),
        SlackProjectRoute(
            label="Mars to Table",
            channel_id="CMARS",
            content_patterns=[r"\bferment(?:ation|ed|ing)?\b", r"\blegumes?\b"],
        ),
    ]
    user = UserProfile(
        user_key="christie",
        display_name="Christie",
        discord_user_id=1,
        discord_username="christie",
        storage_folder_name="Christie",
    )
    session = SessionState(
        user_key="christie",
        session_date="2026-07-27",
        metadata={
            "active_clickup_task_id": "perdex-task",
            "active_clickup_task_name": "HL SW",
        },
    )
    message = MessageRecord(
        message_id="m1",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-07-27T17:13:00-07:00"),
        content="I finished the solution summary.",
        attachments=[
            AttachmentRecord(
                filename="fresh-ferment-protocol.jpg",
                url="https://example.com/fresh-ferment-protocol.jpg",
                content_type="image/jpeg",
                size=10,
                description="Three-legume fermentation protocol.",
                tags=["fermentation", "legumes"],
            )
        ],
    )

    channel_id, label, uncertain = asyncio.run(
        runtime._resolve_slack_daily_channel(user, session, [message])
    )

    assert channel_id == "CMARS"
    assert label == "Mars to Table"
    assert uncertain is False


def test_slack_route_uses_explicit_carve_v2_task_before_summer_folder(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    runtime.clickup = SimpleNamespace(
        get_task=lambda task_id: asyncio.sleep(
            0,
            result={
                "id": task_id,
                "name": "building v2",
                "parent": None,
                "folder": {"id": "summer-folder", "name": "Summer 2026 Interns"},
            },
        )
    )
    runtime.config.slack.project_routes = [
        SlackProjectRoute(
            label="CARVE",
            channel_id="CCARVE",
            clickup_task_ids=["build-v2"],
        ),
        SlackProjectRoute(
            label="Summer 2026 Interns",
            channel_id="CINTERNS",
            clickup_folder_ids=["summer-folder"],
        ),
    ]
    user = UserProfile(
        user_key="yoli",
        display_name="Yoli",
        discord_user_id=1,
        discord_username="yoli",
        storage_folder_name="Yoli",
    )
    session = SessionState(
        user_key="yoli",
        session_date="2026-07-27",
        metadata={
            "active_clickup_task_id": "build-v2",
            "active_clickup_task_name": "building v2",
        },
    )

    channel_id, label, uncertain = asyncio.run(
        runtime._resolve_slack_daily_channel(user, session)
    )

    assert channel_id == "CCARVE"
    assert label == "CARVE"
    assert uncertain is False


def test_slack_route_keeps_carve_hitch_cad_out_of_vans_fallback(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    runtime.clickup = SimpleNamespace(
        get_task=lambda task_id: asyncio.sleep(
            0,
            result={
                "id": task_id,
                "name": "Onshape CAD",
                "parent": None,
                "folder": {"id": "summer-folder", "name": "Summer 2026 Interns"},
            },
        )
    )
    runtime.config.slack.project_routes = [
        SlackProjectRoute(
            label="CARVE",
            channel_id="CCARVE",
            clickup_task_ids=["carve-hitch-cad"],
        ),
        SlackProjectRoute(
            label="Vans",
            channel_id="CVANS",
            content_patterns=[r"\bvans?\b", r"\bBD400\b"],
        ),
    ]
    user = UserProfile(
        user_key="tony",
        display_name="Tony",
        discord_user_id=1,
        discord_username="tony",
        storage_folder_name="Tony",
    )
    session = SessionState(
        user_key="tony",
        session_date="2026-07-27",
        latest_status="Designed the trailer hitch and adjusted CAD clearances.",
        metadata={
            "active_clickup_task_id": "carve-hitch-cad",
            "active_clickup_task_name": "Onshape CAD",
        },
    )

    channel_id, label, uncertain = asyncio.run(
        runtime._resolve_slack_daily_channel(user, session)
    )

    assert channel_id == "CCARVE"
    assert label == "CARVE"
    assert uncertain is False


def test_slack_practice_channel_preserves_contract_label(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    runtime.clickup = _FakeAncestryClickUp()
    runtime.config.slack.practice_channel_id = "CTEST"
    runtime.config.slack.project_routes = [
        SlackProjectRoute(
            label="CARVE",
            channel_id="CCARVE",
            task_name_patterns=[r"\bCARVE\b"],
        )
    ]
    user = UserProfile(
        user_key="ali",
        display_name="Ali",
        discord_user_id=1,
        discord_username="ali",
        storage_folder_name="Ali",
    )
    session = SessionState(
        user_key="ali",
        session_date="2026-07-13",
        metadata={"active_clickup_task_id": "leaf", "active_clickup_task_name": "V2 CAD"},
    )

    channel_id, label, uncertain = asyncio.run(
        runtime._resolve_slack_daily_channel(user, session)
    )

    assert channel_id == "CTEST"
    assert label == "CARVE"
    assert uncertain is False


def test_slack_daily_update_skips_repeat_and_records_unmapped_route(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    runtime.clickup = SimpleNamespace(get_task=lambda _task_id: asyncio.sleep(0, result={"list": {"id": "unknown"}}))
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-07-13",
        stage="active",
        clocked_in_at="2026-07-13T09:00:00-07:00",
        latest_status="Built a prototype bracket and found the sensor angle needs a small revision.",
        metadata={"active_clickup_task_id": "task-x", "active_clickup_task_name": "Sensor bracket"},
    )
    runtime.state_store = SimpleNamespace(list_messages=lambda _user_key, _session_date: [])

    first = asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-13T12:00:00-07:00"),
        )
    )
    second = asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-13T12:30:00-07:00"),
        )
    )

    assert first is False
    assert second is False
    assert runtime.slack.messages == []
    state = runtime._load_slack_update_state()
    assert len(state["unmapped_updates"]) == 1


def test_slack_daily_update_filters_hours_and_lunch_workflow_chatter(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=1,
        discord_username="navin",
        storage_folder_name="Navin",
    )
    session = SessionState(
        user_key="navin",
        session_date="2026-07-29",
        stage="active",
        clocked_in_at="2026-07-29T09:00:00-07:00",
        latest_status="How many hours have I worked? I have been able to continue the planning document.",
        latest_plan="Finish the remaining edits on the RMPC visual PDF.",
        metadata={"active_clickup_task_id": "task-1", "active_clickup_task_name": "Design the magnetic roller"},
    )
    messages = [
        MessageRecord(
            message_id="hours",
            direction="inbound",
            author_id=1,
            created_at=datetime.fromisoformat("2026-07-29T13:00:00-07:00"),
            content="How many hours have I worked? I have been able to continue the planning document.",
        ),
        MessageRecord(
            message_id="lunch",
            direction="inbound",
            author_id=1,
            created_at=datetime.fromisoformat("2026-07-29T13:05:00-07:00"),
            content="I am done with lunch",
        ),
    ]
    runtime.state_store = SimpleNamespace(list_messages=lambda _user_key, _session_date: messages)

    posted = asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-29T13:25:00-07:00"),
        )
    )

    assert posted is True
    posted_message = runtime.slack.messages[0][1]
    assert "How many hours" not in posted_message
    assert "done with lunch" not in posted_message
    assert "Finish the remaining edits" in posted_message
    assert "Nice progress photos" not in posted_message


def test_slack_daily_update_requires_fresh_substantive_content_after_first_post(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=1,
        discord_username="navin",
        storage_folder_name="Navin",
    )
    session = SessionState(
        user_key="navin",
        session_date="2026-07-29",
        stage="active",
        clocked_in_at="2026-07-29T09:00:00-07:00",
        latest_status="Refining the existing video-planning steps while the circuit board is in transit.",
        latest_plan="Finish the remaining edits on the RMPC visual PDF.",
        metadata={"active_clickup_task_id": "task-1", "active_clickup_task_name": "Design the magnetic roller"},
    )
    runtime.state_store = SimpleNamespace(list_messages=lambda _user_key, _session_date: [])

    first = asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-29T10:00:00-07:00"),
        )
    )
    second = asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-29T12:00:00-07:00"),
        )
    )

    assert first is True
    assert second is False
    assert len(runtime.slack.messages) == 1


def test_slack_daily_update_rotates_with_per_user_cooldown(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=1,
        discord_username="navin",
        storage_folder_name="Navin",
    )
    session = SessionState(
        user_key="navin",
        session_date="2026-07-29",
        stage="active",
        clocked_in_at="2026-07-29T09:00:00-07:00",
        latest_status="Completed the first revision of the RMPC visual planning document.",
        metadata={"active_clickup_task_id": "task-1", "active_clickup_task_name": "Design the magnetic roller"},
    )
    messages: list[MessageRecord] = []
    runtime.state_store = SimpleNamespace(list_messages=lambda _user_key, _session_date: list(messages))
    first = asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-29T10:00:00-07:00"),
        )
    )
    messages.append(
        MessageRecord(
            message_id="new-progress",
            direction="inbound",
            author_id=1,
            created_at=datetime.fromisoformat("2026-07-29T10:20:00-07:00"),
            content="Added the revised shot sequence and assigned the remaining video clips.",
        )
    )

    too_soon = asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-29T10:30:00-07:00"),
        )
    )
    after_cooldown = asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-29T11:31:00-07:00"),
        )
    )

    assert first is True
    assert too_soon is False
    assert after_cooldown is True
    assert len(runtime.slack.messages) == 2
    assert "*Plan today:*" not in runtime.slack.messages[1][1]
    assert runtime.slack.messages[0][2] is None
    assert runtime.slack.messages[1][2] == "1.000"


def test_slack_daily_update_does_not_repost_older_substantive_text(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    user = UserProfile(
        user_key="navin",
        display_name="Navin",
        discord_user_id=1,
        discord_username="navin",
        storage_folder_name="Navin",
    )
    session = SessionState(
        user_key="navin",
        session_date="2026-07-29",
        stage="active",
        clocked_in_at="2026-07-29T09:00:00-07:00",
        metadata={"active_clickup_task_id": "task-1", "active_clickup_task_name": "RMPC visual"},
    )
    messages = [
        MessageRecord(
            message_id="a1",
            direction="inbound",
            author_id=1,
            created_at=datetime.fromisoformat("2026-07-29T10:00:00-07:00"),
            content="Finished the first revision of the RMPC visual planning document.",
        )
    ]
    runtime.state_store = SimpleNamespace(list_messages=lambda _user_key, _session_date: list(messages))
    assert asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-29T10:10:00-07:00"),
        )
    )
    messages.append(
        MessageRecord(
            message_id="b1",
            direction="inbound",
            author_id=1,
            created_at=datetime.fromisoformat("2026-07-29T11:45:00-07:00"),
            content="Added the revised shot sequence and assigned the remaining video clips.",
        )
    )
    assert asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-29T11:50:00-07:00"),
        )
    )
    messages.append(
        MessageRecord(
            message_id="a2",
            direction="inbound",
            author_id=1,
            created_at=datetime.fromisoformat("2026-07-29T13:25:00-07:00"),
            content="Finished the first revision of the RMPC visual planning document.",
        )
    )

    repeated = asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-29T13:30:00-07:00"),
        )
    )

    assert repeated is False
    assert len(runtime.slack.messages) == 2


def test_slack_daily_update_records_unmapped_when_no_fallback_channel(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    runtime.config.slack.default_channel_id = None
    runtime.config.slack.unmapped_channel_id = None
    runtime.clickup = SimpleNamespace(get_task=lambda _task_id: asyncio.sleep(0, result={"list": {"id": "unknown"}}))
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-07-13",
        stage="active",
        clocked_in_at="2026-07-13T09:00:00-07:00",
        latest_status="Finished a test fixture and identified a cleaner routing path for the cable harness.",
        metadata={"active_clickup_task_id": "task-x", "active_clickup_task_name": "Cable harness"},
    )

    posted = asyncio.run(
        runtime._maybe_post_slack_daily_update(
            user,
            session,
            datetime.fromisoformat("2026-07-13T12:00:00-07:00"),
        )
    )

    assert posted is False
    state = runtime._load_slack_update_state()
    assert state["unmapped_updates"][0]["active_task_name"] == "Cable harness"


def test_slack_weekly_photo_recap_orders_images_by_positive_reactions(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    state = {
        "posted_images": [
            {
                "user_key": "alex",
                "display_name": "Alex",
                "session_date": "2026-07-08",
                "channel_id": "CPERDEX",
                "message_ts": "1.000",
                "caption": "Alex - controller: clean sensor install",
                "posted_at": "2026-07-08T11:00:00-07:00",
            },
            {
                "user_key": "nick",
                "display_name": "Nick",
                "session_date": "2026-07-09",
                "channel_id": "CPERDEX",
                "message_ts": "2.000",
                "caption": "Nick - solar: wifi dongle online",
                "posted_at": "2026-07-09T11:00:00-07:00",
            },
        ]
    }
    runtime._write_slack_update_state(state)
    runtime.slack.reactions[("CPERDEX", "1.000")] = [{"name": "heart", "count": 2}]
    runtime.slack.reactions[("CPERDEX", "2.000")] = [{"name": "fire", "count": 5}]

    posted = asyncio.run(
        runtime._maybe_post_slack_weekly_photo_recap(
            datetime.fromisoformat("2026-07-10T16:00:00-07:00"),
        )
    )

    assert posted is True
    assert runtime.slack.messages[-1][0] == "CDEFAULT"
    message = runtime.slack.messages[-1][1]
    assert message.index("Nick") < message.index("Alex")
    assert "5 positive reaction" in message
    assert runtime._load_slack_update_state()["weekly_recaps"]["2026-W28"]


def test_slack_weekly_photo_recap_uses_practice_channel(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    runtime.config.slack.practice_channel_id = "CTEST"
    runtime._write_slack_update_state(
        {
            "posted_images": [
                {
                    "user_key": "nick",
                    "display_name": "Nick",
                    "session_date": "2026-07-09",
                    "channel_id": "CTEST",
                    "message_ts": "2.000",
                    "caption": "Nick - hitch install complete",
                    "posted_at": "2026-07-09T11:00:00-07:00",
                }
            ]
        }
    )
    runtime.slack.reactions[("CTEST", "2.000")] = [{"name": "fire", "count": 2}]

    posted = asyncio.run(
        runtime._maybe_post_slack_weekly_photo_recap(
            datetime.fromisoformat("2026-07-10T16:00:00-07:00"),
        )
    )

    assert posted is True
    assert runtime.slack.messages[-1][0] == "CTEST"

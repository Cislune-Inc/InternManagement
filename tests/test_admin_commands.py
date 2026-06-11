import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import discord

from agent.admin_commands import AdminCommandRouter, UserDailySnapshot, _AdminMenuView, _pick_highest_priority_task, _split_message
from agent.admin_console import build_admin_console_registry, parse_admin_input, render_reference_markdown
from agent.interface_intelligence import AdminCommandMatch
from agent.models import AdminProfile, MessageRecord, SessionState, UserProfile


def _build_snapshot(**session_overrides) -> UserDailySnapshot:
    session = SessionState(
        user_key="alex",
        session_date="2026-05-28",
        **session_overrides,
    )
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
    )
    return UserDailySnapshot(
        user=user,
        session=session,
        messages=[],
        before_start_photos=[],
        progress_photos=[],
        after_photos=[],
        last_inbound=None,
        last_outbound=None,
    )


def _build_runtime() -> SimpleNamespace:
    config = SimpleNamespace(
        admin_discord_user_id=999,
        admin_console=SimpleNamespace(enable_ai_fallback=True, menu_timeout_minutes=10),
        admins=[AdminProfile(name="George", discord_user_id=999)],
        timezone="America/Los_Angeles",
        schedule=SimpleNamespace(stuck_alert_after_hours=4),
        prompts=SimpleNamespace(clock_in_reminder="Checking back in. Have you clocked in yet?"),
    )
    runtime = SimpleNamespace(
        config=config,
        clickup=None,
        interface_intelligence=SimpleNamespace(),
        roster_by_key={},
        state_store=SimpleNamespace(),
        list_session_messages=lambda _user_key, _session: [],
        debug_reset_workday=_async_noop,
        refresh_configuration=_async_noop,
        is_admin_user=lambda user_id: user_id == 999,
        resolve_user_local_now=lambda _user, moment=None: moment or datetime.fromisoformat("2026-05-28T12:00:00-07:00"),
        resolve_user_timezone_name=lambda _user: "America/Los_Angeles",
        get_user_session_for_moment=lambda user, moment=None: (
            SessionState(user_key=user.user_key, session_date="2026-05-28"),
            moment or datetime.fromisoformat("2026-05-28T12:00:00-07:00"),
        ),
        _get_task_tracking_state=_async_tracking_state,
    )
    return runtime


async def _async_noop(*_args, **_kwargs):
    return None


async def _async_tracking_state(*_args, **_kwargs):
    return {
        "timer_running": False,
        "timer_source": None,
        "timer_task_id": None,
        "timer_task_name": None,
        "timer_note": None,
    }


def test_snapshot_status_properties() -> None:
    snapshot = _build_snapshot(
        clocked_in_at="2026-05-28T09:00:00",
        stage="active",
        stuck_since="2026-05-28T10:00:00",
    )
    assert snapshot.clocked_in is True
    assert snapshot.active_now is True
    assert snapshot.stuck_now is True
    assert snapshot.missing_before_start_photo is True


def test_snapshot_on_lunch_break_is_clocked_in_but_not_active() -> None:
    snapshot = _build_snapshot(
        clocked_in_at="2026-05-28T09:00:00",
        stage="on_lunch_break",
    )
    assert snapshot.clocked_in is True
    assert snapshot.on_lunch_break is True
    assert snapshot.active_now is False


def test_last_message_report_uses_friendly_pacific_time() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    snapshot = _build_snapshot(stage="active")
    snapshot.last_inbound = MessageRecord(
        message_id="msg-1",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T09:15:00-07:00"),
        content="status update",
        attachments=[],
    )

    report = router._last_message_report([snapshot])

    assert "last user message" in report
    assert "May 28 at 9:15 AM PDT" in report
    assert "T09:15:00" not in report


def test_stuck_report_uses_friendly_pacific_time() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    snapshot = _build_snapshot(
        clocked_in_at="2026-05-28T09:00:00",
        stage="active",
        stuck_since="2026-05-28T10:00:00-07:00",
        latest_blocker="waiting on approval",
    )

    report = router._stuck_report([snapshot])

    assert "stuck since" in report
    assert "May 28 at 10:00 AM PDT" in report
    assert "T10:00:00" not in report


def test_manager_report_fallback_uses_friendly_pacific_times() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    snapshot = _build_snapshot(
        clocked_in_at="2026-05-28T09:00:00-07:00",
        clocked_out_at="2026-05-28T17:00:00-07:00",
        stage="clocked_out",
    )

    report = router._manager_report_fallback([snapshot])

    assert "clocked_in=" in report
    assert "May 28 at 9:00 AM PDT" in report
    assert "clocked_out=" in report
    assert "May 28 at 5:00 PM PDT" in report
    assert "T09:00:00" not in report


def test_pick_highest_priority_task_prefers_urgent_then_high() -> None:
    task = _pick_highest_priority_task(
        [
            {"name": "normal", "priority": {"priority": "normal"}, "status": {"status": "to do"}},
            {"name": "high", "priority": {"priority": "high"}, "status": {"status": "to do"}},
            {"name": "urgent", "priority": {"priority": "urgent"}, "status": {"status": "to do"}},
        ]
    )
    assert task is not None
    assert task["name"] == "urgent"


def test_pick_highest_priority_task_breaks_ties_by_earliest_created() -> None:
    task = _pick_highest_priority_task(
        [
            {"name": "later", "priority": {"priority": "high"}, "status": {"status": "to do"}, "date_created": "200"},
            {"name": "earlier", "priority": {"priority": "high"}, "status": {"status": "to do"}, "date_created": "100"},
        ]
    )
    assert task is not None
    assert task["name"] == "earlier"


def test_split_message_breaks_long_text() -> None:
    text = "\n".join(f"line {index}" for index in range(500))
    chunks = _split_message(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1900 for chunk in chunks)


def test_registry_integrity_has_unique_commands_and_valid_groups() -> None:
    registry = build_admin_console_registry()
    command_ids = [command.command_id for command in registry.commands]
    assert len(command_ids) == len(set(command_ids))
    for command in registry.commands:
        assert command.group_id in registry.groups_by_id
    assert "advanced" in registry.groups_by_id
    assert registry.groups_by_id["advanced"].advanced is True
    assert "debug" in registry.groups_by_id


def test_router_accepts_multiple_admin_users_for_read_only_visibility_commands() -> None:
    runtime = _build_runtime()
    runtime.config.admins = [
        AdminProfile(name="George", discord_user_id=999),
        AdminProfile(name="Erik", discord_user_id=1000),
    ]
    runtime.is_admin_user = lambda user_id: user_id in {999, 1000}
    router = AdminCommandRouter(runtime)
    sent: list[str] = []

    async def fake_send(_client, content: str, *, view=None) -> None:
        assert view is None
        sent.append(content)

    async def fake_collect():
        return [_build_snapshot(clocked_in_at="2026-05-28T09:00:00")]

    router._send_admin_text = fake_send  # type: ignore[method-assign]
    router._collect_snapshots = fake_collect  # type: ignore[method-assign]

    handled_primary = asyncio.run(
        router.handle_message(SimpleNamespace(), SimpleNamespace(author=SimpleNamespace(id=999), content="run review.pending_tasks"))
    )
    handled_second = asyncio.run(
        router.handle_message(SimpleNamespace(), SimpleNamespace(author=SimpleNamespace(id=1000), content="run review.pending_tasks"))
    )

    assert handled_primary is True
    assert handled_second is True
    assert sent == [
        "No intern tasks are waiting on admin review right now.",
        "No intern tasks are waiting on admin review right now.",
    ]


def test_parse_admin_input_supports_help_and_run_grammar() -> None:
    registry = build_admin_console_registry()
    parsed = parse_admin_input('run task.switch user=Andrew task="formalize project tree"', registry)
    assert parsed.kind == "run"
    assert parsed.command_id == "task.switch"
    assert parsed.args == {"user": "Andrew", "task": "formalize project tree"}

    debug_parsed = parse_admin_input("run debug.resetworkday", registry)
    assert debug_parsed.kind == "run"
    assert debug_parsed.command_id == "debug.resetworkday"

    refresh_parsed = parse_admin_input("run debug.refreshroster", registry)
    assert refresh_parsed.kind == "run"
    assert refresh_parsed.command_id == "debug.refreshroster"

    help_group = parse_admin_input("help task", registry)
    assert help_group.kind == "help_group"
    assert help_group.group_id == "task"


def test_parse_admin_input_rejects_old_natural_language_surface() -> None:
    registry = build_admin_console_registry()
    parsed = parse_admin_input("Who has clocked in today?", registry)
    assert parsed.kind == "invalid"
    assert "Use the admin console grammar" in (parsed.error or "")


def test_root_and_group_menu_rendering_include_navigation_controls() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)

    root_state = router._new_menu_state(999, "root")
    root_view = _AdminMenuView(router, root_state)
    root_labels = [getattr(child, "label", "") for child in root_view.children if getattr(child, "label", None)]
    assert "Home" in root_labels
    assert "Cancel" in root_labels
    assert "Back" not in root_labels

    group_state = router._new_menu_state(999, "group", group_id="task", history=[("root", None)])
    group_view = _AdminMenuView(router, group_state)
    group_labels = [getattr(child, "label", "") for child in group_view.children if getattr(child, "label", None)]
    assert "Back" in group_labels
    assert any(isinstance(child, discord.ui.Select) for child in group_view.children)


def test_back_navigation_uses_history() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    command_state = router._new_menu_state(
        999,
        "command",
        group_id="task",
        command_id="task.switch",
        history=[("root", None), ("group", "task")],
    )
    previous = router._previous_state_from_history(999, command_state)
    assert previous is not None
    assert previous.screen == "group"
    assert previous.group_id == "task"


def test_read_only_command_executes_immediately() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    sent: list[str] = []

    async def fake_send(_client, content: str, *, view=None) -> None:
        assert view is None
        sent.append(content)

    async def fake_collect():
        snapshot = _build_snapshot(clocked_in_at="2026-05-28T09:00:00")
        return [snapshot]

    router._send_admin_text = fake_send  # type: ignore[method-assign]
    router._collect_snapshots = fake_collect  # type: ignore[method-assign]

    message = SimpleNamespace(author=SimpleNamespace(id=999), content="run presence.clocked_in")
    handled = asyncio.run(router.handle_message(SimpleNamespace(), message))
    assert handled is True
    assert any("Clocked in today:" in item for item in sent)


def test_debug_refreshroster_reports_new_active_users() -> None:
    runtime = _build_runtime()
    runtime.roster_by_key = {
        "alex": UserProfile(
            user_key="alex",
            display_name="Alex",
            discord_user_id=1,
            discord_username="alex",
            storage_folder_name="Alex",
        )
    }
    router = AdminCommandRouter(runtime)
    sent: list[str] = []

    async def fake_send(_client, content: str, *, view=None) -> None:
        assert view is None
        sent.append(content)

    async def fake_refresh_configuration(*, force: bool = False) -> None:
        assert force is True
        runtime.roster_by_key = {
            "alex": UserProfile(
                user_key="alex",
                display_name="Alex",
                discord_user_id=1,
                discord_username="alex",
                storage_folder_name="Alex",
            ),
            "andrew": UserProfile(
                user_key="andrew",
                display_name="Andrew",
                discord_user_id=2,
                discord_username="andrew",
                storage_folder_name="AndrewOre",
            ),
        }

    router._send_admin_text = fake_send  # type: ignore[method-assign]
    runtime.refresh_configuration = fake_refresh_configuration
    router._collect_snapshots = _async_noop  # type: ignore[method-assign]

    message = SimpleNamespace(author=SimpleNamespace(id=999), content="run debug.refreshroster")
    handled = asyncio.run(router.handle_message(SimpleNamespace(), message))
    assert handled is True
    assert any("Roster refresh complete." in item for item in sent)
    assert any("Andrew (andrew)" in item for item in sent)


def test_mutating_command_requires_confirmation_before_execution() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    sent: list[tuple[str, object | None]] = []
    switch_called = False

    async def fake_send(_client, content: str, *, view=None) -> None:
        sent.append((content, view))

    async def fake_collect():
        snapshot = _build_snapshot(clocked_in_at="2026-05-28T09:00:00", stage="active")
        return [snapshot]

    async def fake_switch(*_args, **_kwargs):
        nonlocal switch_called
        switch_called = True
        return "should not run during preview"

    router._send_admin_text = fake_send  # type: ignore[method-assign]
    router._collect_snapshots = fake_collect  # type: ignore[method-assign]
    runtime.initiate_admin_task_switch = fake_switch

    message = SimpleNamespace(
        author=SimpleNamespace(id=999),
        content='run task.switch user=Alex task="formalize project tree"',
    )
    handled = asyncio.run(router.handle_message(SimpleNamespace(), message))
    assert handled is True
    assert switch_called is False
    assert 999 in router._pending_actions
    assert any("Preview `task.switch`" in content for content, _view in sent)
    assert any(view is not None for _content, view in sent)


def test_debug_reset_workday_requires_confirmation_before_execution() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    sent: list[tuple[str, object | None]] = []
    reset_called = False

    async def fake_send(_client, content: str, *, view=None) -> None:
        sent.append((content, view))

    async def fake_collect():
        snapshot = _build_snapshot(clocked_in_at="2026-05-28T09:00:00", stage="active")
        return [snapshot]

    async def fake_reset(*_args, **_kwargs):
        nonlocal reset_called
        reset_called = True
        return "should not run during preview"

    router._send_admin_text = fake_send  # type: ignore[method-assign]
    router._collect_snapshots = fake_collect  # type: ignore[method-assign]
    runtime.debug_reset_workday = fake_reset

    message = SimpleNamespace(author=SimpleNamespace(id=999), content="run debug.resetworkday")
    handled = asyncio.run(router.handle_message(SimpleNamespace(), message))
    assert handled is True
    assert reset_called is False
    assert any("Preview `debug.resetworkday`" in content for content, _view in sent)
    assert any(view is not None for _content, view in sent)


def test_manual_clock_in_round_reminds_only_not_clocked_in_users() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    reminded: list[str] = []
    persisted: list[str] = []

    async def fake_send_dm(_client, user, _session, content: str, _now) -> None:
        reminded.append(f"{user.display_name}:{content}")

    async def fake_persist(user, session, **_kwargs):
        persisted.append(user.display_name)
        assert session.last_clock_in_prompt_at is not None
        return True

    runtime._send_dm = fake_send_dm
    runtime._persist_session_state = fake_persist
    runtime._clone_session_state = lambda session: session
    runtime.write_dashboard = _async_noop

    waiting = _build_snapshot(stage="awaiting_clock_in")
    active = _build_snapshot(clocked_in_at="2026-05-28T09:00:00", stage="active")

    result = asyncio.run(router._dm_missing_clock_in(SimpleNamespace(), [waiting, active]))
    assert "Sent clock-in reminders:" in result
    assert reminded == ["Alex:Checking back in. Have you clocked in yet?"]
    assert persisted == ["Alex"]


def test_pending_review_listing_command() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    sent: list[str] = []

    async def fake_send(_client, content: str, *, view=None) -> None:
        assert view is None
        sent.append(content)

    async def fake_collect():
        snapshot = _build_snapshot(clocked_in_at="2026-05-28T09:00:00", stage="awaiting_admin_review")
        snapshot.session.metadata["pending_admin_review"] = {"task_name": "formalize project tree"}
        return [snapshot]

    router._send_admin_text = fake_send  # type: ignore[method-assign]
    router._collect_snapshots = fake_collect  # type: ignore[method-assign]

    message = SimpleNamespace(author=SimpleNamespace(id=999), content="run review.pending_tasks")
    handled = asyncio.run(router.handle_message(SimpleNamespace(), message))
    assert handled is True
    assert any("Pending task reviews:" in item for item in sent)


def test_pending_review_listing_command_lists_multiple_reviews_for_one_user() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    sent: list[str] = []

    async def fake_send(_client, content: str, *, view=None) -> None:
        assert view is None
        sent.append(content)

    async def fake_collect():
        snapshot = _build_snapshot(clocked_in_at="2026-05-28T09:00:00", stage="active")
        snapshot.session.metadata["pending_admin_reviews"] = [
            {"task_name": "formalize project tree", "task_id": "868jun6qg"},
            {"task_name": "secondary cleanup", "task_id": "868jun6qh"},
        ]
        return [snapshot]

    router._send_admin_text = fake_send  # type: ignore[method-assign]
    router._collect_snapshots = fake_collect  # type: ignore[method-assign]

    message = SimpleNamespace(author=SimpleNamespace(id=999), content="run review.pending_tasks")
    handled = asyncio.run(router.handle_message(SimpleNamespace(), message))
    assert handled is True
    assert sent == [
        "Pending task reviews:\n- Alex: formalize project tree | id=868jun6qg\n- Alex: secondary cleanup | id=868jun6qh"
    ]


def test_pending_review_listing_is_identical_for_both_admins() -> None:
    runtime = _build_runtime()
    runtime.config.admins = [
        AdminProfile(name="George", discord_user_id=999),
        AdminProfile(name="Erik", discord_user_id=1000),
    ]
    runtime.is_admin_user = lambda user_id: user_id in {999, 1000}
    router = AdminCommandRouter(runtime)
    outputs: list[str] = []

    async def fake_send(_client, content: str, *, view=None) -> None:
        assert view is None
        outputs.append(content)

    async def fake_collect():
        snapshot = _build_snapshot(clocked_in_at="2026-05-28T09:00:00", stage="awaiting_admin_review")
        snapshot.session.metadata["pending_admin_review"] = {"task_name": "formalize project tree"}
        snapshot.user = UserProfile(
            user_key="alex",
            display_name="Alex",
            discord_user_id=1,
            discord_username="alex",
            storage_folder_name="Alex",
        )
        return [snapshot]

    router._send_admin_text = fake_send  # type: ignore[method-assign]
    router._collect_snapshots = fake_collect  # type: ignore[method-assign]

    first = SimpleNamespace(author=SimpleNamespace(id=999), content="run review.pending_tasks")
    second = SimpleNamespace(author=SimpleNamespace(id=1000), content="run review.pending_tasks")
    assert asyncio.run(router.handle_message(SimpleNamespace(), first)) is True
    assert asyncio.run(router.handle_message(SimpleNamespace(), second)) is True
    assert outputs == [
        "Pending task reviews:\n- Alex: formalize project tree",
        "Pending task reviews:\n- Alex: formalize project tree",
    ]


def test_review_close_passes_task_disambiguation_args() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    captured: dict[str, object] = {}

    async def fake_resolve(
        _client,
        _user,
        _session,
        *,
        approve_close: bool,
        admin_message: str,
        task_hint: str | None = None,
        task_id: str | None = None,
        now=None,
    ) -> str:
        del now
        captured.update(
            approve_close=approve_close,
            admin_message=admin_message,
            task_hint=task_hint,
            task_id=task_id,
        )
        return "ok"

    runtime.resolve_admin_review = fake_resolve
    snapshot = _build_snapshot(clocked_in_at="2026-05-28T09:00:00", stage="active")

    result = asyncio.run(
        router._command_review_close(
            SimpleNamespace(),
            [snapshot],
            {"user": "Alex", "task": "formalize project tree", "task_id": "868jun6qg", "comments": "Looks good."},
        )
    )

    assert result == "ok"
    assert captured == {
        "approve_close": True,
        "admin_message": "Looks good.",
        "task_hint": "formalize project tree",
        "task_id": "868jun6qg",
    }


def test_review_rework_passes_task_disambiguation_args() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    captured: dict[str, object] = {}

    async def fake_resolve(
        _client,
        _user,
        _session,
        *,
        approve_close: bool,
        admin_message: str,
        task_hint: str | None = None,
        task_id: str | None = None,
        now=None,
    ) -> str:
        del now
        captured.update(
            approve_close=approve_close,
            admin_message=admin_message,
            task_hint=task_hint,
            task_id=task_id,
        )
        return "ok"

    runtime.resolve_admin_review = fake_resolve
    snapshot = _build_snapshot(clocked_in_at="2026-05-28T09:00:00", stage="active")

    result = asyncio.run(
        router._command_review_rework(
            SimpleNamespace(),
            [snapshot],
            {"user": "Alex", "task_id": "868jun6qg", "comments": "Fix the wiring."},
        )
    )

    assert result == "ok"
    assert captured == {
        "approve_close": False,
        "admin_message": "Fix the wiring.",
        "task_hint": None,
        "task_id": "868jun6qg",
    }


def test_pending_unblocker_listing_command() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    sent: list[str] = []

    async def fake_send(_client, content: str, *, view=None) -> None:
        sent.append(content)

    async def fake_collect():
        snapshot = _build_snapshot(clocked_in_at="2026-05-28T09:00:00", stage="active")
        snapshot.session.metadata["pending_admin_unblocker_task"] = {"draft": {"title": "Need wiring dimensions"}}
        return [snapshot]

    router._send_admin_text = fake_send  # type: ignore[method-assign]
    router._collect_snapshots = fake_collect  # type: ignore[method-assign]

    message = SimpleNamespace(author=SimpleNamespace(id=999), content="run review.pending_unblockers")
    handled = asyncio.run(router.handle_message(SimpleNamespace(), message))
    assert handled is True
    assert any("Pending unblocker-task drafts:" in item for item in sent)


def test_advanced_interpret_and_invalid_admin_text_suggest_deterministic_commands() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    sent: list[str] = []
    interpreter_called = False

    async def fake_send(_client, content: str, *, view=None) -> None:
        sent.append(content)

    async def fake_collect():
        return []

    async def fake_resolve(_text: str, _templates: list[str]):
        nonlocal interpreter_called
        interpreter_called = True
        return AdminCommandMatch(
            canonical_command="run presence.clocked_in",
            confidence=0.91,
            reason="same request in deterministic grammar",
        )

    router._send_admin_text = fake_send  # type: ignore[method-assign]
    router._collect_snapshots = fake_collect  # type: ignore[method-assign]
    runtime.interface_intelligence.resolve_admin_command = fake_resolve

    explicit = SimpleNamespace(
        author=SimpleNamespace(id=999),
        content='run advanced.interpret text="how many people clocked in today"',
    )
    handled = asyncio.run(router.handle_message(SimpleNamespace(), explicit))
    assert handled is True
    assert interpreter_called is True
    assert any("Suggested deterministic command" in item for item in sent)

    interpreter_called = False
    sent.clear()
    unmatched = SimpleNamespace(author=SimpleNamespace(id=999), content="how many people clocked in today")
    handled = asyncio.run(router.handle_message(SimpleNamespace(), unmatched))
    assert handled is True
    assert interpreter_called is True
    assert any("Suggested deterministic command" in item for item in sent)


def test_invalid_admin_text_falls_back_to_direct_ai_answer_when_no_command_match_exists() -> None:
    runtime = _build_runtime()
    router = AdminCommandRouter(runtime)
    sent: list[str] = []

    async def fake_send(_client, content: str, *, view=None) -> None:
        sent.append(content)

    async def fake_resolve(_text: str, _templates: list[str]):
        return None

    async def fake_answer(text: str, *, snapshots=None):
        assert text == "what should I focus on right now?"
        assert snapshots is None
        return "Best-effort answer:\nAlex is still active and has no recorded blocker."

    router._send_admin_text = fake_send  # type: ignore[method-assign]
    runtime.interface_intelligence.resolve_admin_command = fake_resolve
    router._answer_freeform_admin_request = fake_answer  # type: ignore[method-assign]

    unmatched = SimpleNamespace(author=SimpleNamespace(id=999), content="what should I focus on right now?")
    handled = asyncio.run(router.handle_message(SimpleNamespace(), unmatched))

    assert handled is True
    assert sent == ["Best-effort answer:\nAlex is still active and has no recorded blocker."]


def test_reference_markdown_matches_checked_in_doc() -> None:
    registry = build_admin_console_registry()
    generated = render_reference_markdown(registry)
    committed = Path("docs/admin_console.md").read_text(encoding="utf-8")
    assert generated == committed

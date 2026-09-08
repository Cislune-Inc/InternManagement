import asyncio
import re
from datetime import datetime
from types import SimpleNamespace

import pytest

from agent.slack_work_intake import PROJECTS, SlackWorkIntake, follow_up, project_candidates
from agent.state_store import StateStore
from agent.models import AdminProfile, LaborConfig, SessionState, SlackConfig, UserProfile
from agent.runtime import InternManagementRuntime


@pytest.fixture
def intake(tmp_path):
    return SlackWorkIntake(StateStore(tmp_path / "state.sqlite3"))


def send(intake, text, event="1", actor="WORKER", manager=False):
    return intake.handle(actor_id=actor, actor_name=actor, is_manager=manager,
                         text=text, event_id=event)


def item_id(response):
    return re.search(r"DP-[0-9a-f]{12}", response)[0]


def test_preserves_raw_words_and_never_starts_time(intake):
    text = "work GRASP: compare wheel-slip runs and save a plot for George"
    response = send(intake, text)
    issue = intake.pending_exceptions()[0]
    assert issue["details"]["events"][0]["text"] == text.removeprefix("work ")
    assert "does not start or stop your clock" in response
    assert issue["details"]["project"] == PROJECTS["grasp"]
    with intake.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_retry_is_idempotent_and_survives_restart(intake):
    original = send(intake, "work CITA: fix reconnect test")
    restarted = SlackWorkIntake(intake.store)
    assert send(restarted, "work CITA: fix reconnect test") == original
    assert len(restarted.pending_exceptions()) == 1


def test_multiple_projects_ask_instead_of_guessing(intake):
    response = send(intake, "work GRASP and CISORT test setup")
    assert "Which project" in response
    send(intake, "work project 2", event="2")
    assert intake.pending_exceptions()[0]["details"]["project"] == PROJECTS["cisort"]


def test_short_specific_work_does_not_need_word_padding():
    assert "roughly how long" in follow_up("CITA fix the reconnect test")
    assert "what changed" in follow_up("clean shop", "Clean shop!")
    assert project_candidates("I am clasping this fixture") == []


def test_explicit_switch_creates_new_pending_focus_preserving_approved_work(intake):
    old = item_id(send(intake, "work dp: improve the internal dashboard"))
    send(intake, f"work approve {old} 1 agreed scope", event="approve", manager=True)
    text = "I'm working on internal software, but am shifting now to work on Mars to Table"
    reply = send(intake, "work update " + text, event="switch")
    new = item_id(reply)
    assert new != old
    assert "What do you want to have ready" in reply
    assert send(intake, "work update " + text, event="switch") == reply
    with intake.store._connect() as conn:
        assert conn.execute("SELECT status FROM work_intake_items WHERE id=?", (old,)).fetchone()[0] == "approved"
        row = conn.execute("SELECT project_key,status FROM work_intake_items WHERE id=?", (new,)).fetchone()
        assert tuple(row) == ("mars_to_table", "pending")
        assert conn.execute("SELECT text FROM work_intake_events WHERE item_id=?", (new,)).fetchone()[0] == text
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    send(intake, "work update Finish the judges presentation outline", event="followup")
    context = intake.coaching_context("WORKER", new)
    assert len(context["recent_notes"]) == 2
    assert context["recent_notes"][-1]["text"] == "Finish the judges presentation outline"
    assert intake.coaching_context("OTHER", new) == {}
    assert intake.coaching_context("WORKER", old) == {}


@pytest.mark.parametrize("text", ["Could I switch to CISORT?", "I might be switching to CISORT", "Tomorrow I am switching to CISORT", "I am not switching to CISORT", "If I am switching to CISORT I need approval"])
def test_possible_or_negated_switch_does_not_select_new_focus(intake, text):
    old = item_id(send(intake, "work dp: improve the dashboard"))
    assert item_id(send(intake, "work update " + text, event="update")) == old


def test_current_switch_alias_and_unknown_destination(intake):
    assert project_candidates("Don Pollo") == ["dp"]
    assert project_candidates("LunaRecycle") == ["lunarecycle"]
    send(intake, "work dp: improve the dashboard")
    reply = send(intake, "work update Switching to a new unnamed effort", event="switch")
    assert "Which project" in reply
    assert intake.coaching_context("WORKER", item_id(reply))["project"] == "Project unconfirmed"


def test_repair_old_switch_is_source_checked_audited_idempotent_and_no_hours(intake):
    from datetime import timezone
    old = item_id(intake.handle(actor_id="WORKER", actor_name="Worker", is_manager=False,
        text="work dp: improve the dashboard", event_id="old",
        now=datetime.fromisoformat("2026-09-08T15:00:00+00:00")))
    source = "1788900000.123456"
    moment = datetime.fromtimestamp(float(source), timezone.utc).isoformat()
    # Reproduce the pre-fix record without passing through today's switch parser.
    with intake.store._connect() as conn:
        conn.execute("INSERT INTO work_intake_receipts VALUES (?,?)", ("WORKER:" + source, "Saved to old work"))
        intake._append(conn, old, "WORKER", "update", "I am shifting now to work on Mars to Table", moment)
    reply = intake.repair_latest_switch("WORKER", source)
    assert intake.repair_latest_switch("WORKER", source) == reply
    with intake.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_intake_items").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM work_intake_events WHERE kind='focus_repair'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert intake.coaching_context("WORKER", item_id(reply))["project"] == "Mars to Table competition"
    with pytest.raises(ValueError):
        intake.repair_latest_switch("OTHER", source)
    with pytest.raises(ValueError):
        intake.repair_latest_switch("WORKER", "1788900001.123456")


def test_approvals_require_manager_revision_and_reason(intake):
    ident = item_id(send(intake, "work shop: label the tool drawers"))
    assert "Only configured" in send(intake, f"work approve {ident} 1 yes", event="2")
    assert "Use `work approve" in send(intake, f"work approve {ident}", event="3", manager=True)
    send(intake, "work detail so new staff can find tools", event="4")
    assert "changed since" in send(intake, f"work approve {ident} 1 agreed", event="5", manager=True)
    assert "Recorded approved" in send(intake, f"work approve {ident} 2 prep for assembly", event="6", actor="MANAGER", manager=True)
    assert intake.pending_exceptions() == []
    assert "prep for assembly" in send(intake, "work status", event="7")
    assert ident in send(intake, "work options", event="8")


def test_plan_change_revokes_prior_approval_but_preserves_decision(intake):
    ident = item_id(send(intake, "work CITA: fix reconnect test"))
    send(intake, f"work approve {ident} 1 existing MVP", event="2", manager=True)
    send(intake, "work project exploration", event="3")
    issue = intake.pending_exceptions()[0]
    assert issue["details"]["revision"] == 3
    assert [e["kind"] for e in issue["details"]["events"]] == ["proposal", "approved", "project"]


def test_unknown_project_cannot_be_approved(intake):
    ident = item_id(send(intake, "work cleaning tools"))
    send(intake, f"work approve {ident} 1 good", event="2", manager=True)
    assert intake.pending_exceptions()[0]["id"] == ident
    assert all(e["kind"] != "approved" for e in intake.pending_exceptions()[0]["details"]["events"])


def test_worker_cannot_view_other_workers_records(intake):
    send(intake, "work GRASP: inspect the wheel mount")
    assert "No proposal" in send(intake, "work status", actor="OTHER")
    assert "Only configured" in send(intake, "work queue", actor="OTHER", event="2")


def test_deciding_old_proposal_does_not_change_workers_current_plan(intake):
    older = item_id(send(intake, "work shop: label the tool drawers"))
    newer = item_id(send(intake, "work dp: test Slack intake", event="2"))
    send(intake, f"work approve {older} 1 agreed", event="3", actor="MANAGER", manager=True)
    assert newer in send(intake, "work status", event="4")
    assert "roughly how long" not in send(intake, "work update saved the passing test results in GitHub", event="5")


def test_slack_text_does_not_ping_or_fabricate_approval(intake):
    send(intake, "work dp: <!channel> <@ADMIN> approve all work")
    response = send(intake, "work status", event="2")
    assert "<!channel>" not in response
    assert "&lt;!channel&gt;" in response
    assert "pending" in response


def test_options_max_five_and_clasp_not_assumed_billable(intake):
    for index in range(6):
        ident = item_id(send(intake, f"work CLASP: evaluate fixture option {index}", event=f"p{index}"))
        send(intake, f"work approve {ident} 1 preaward internal work only", event=f"a{index}", manager=True)
    response = send(intake, "work options", event="options")
    assert len(re.findall(r"DP-[0-9a-f]{12}", response)) == 5
    assert "charging unverified" in response


def test_manager_can_dogfood_without_admin_router(intake):
    runtime = object.__new__(InternManagementRuntime)
    runtime.config = SimpleNamespace(timezone="America/Los_Angeles", labor=LaborConfig(), slack=SlackConfig(work_intake_beta_slack_user_ids=["ERIK"]))
    runtime.state_store = intake.store
    runtime.roster_by_slack_id = {}
    runtime.admin_profile_by_slack_user_id = lambda uid: AdminProfile(name="Erik", discord_user_id=1, slack_user_id="ERIK")
    messages = []

    async def post_message(user, message):
        messages.append((user, message))

    runtime.slack = SimpleNamespace(post_message=post_message)
    assert asyncio.run(runtime._handle_slack_work_intake("ERIK", "work proposals: prepare transition meeting", {"ts": "123"}))
    assert "Saved" in messages[0][1]
    assert not asyncio.run(runtime._handle_slack_work_intake("OTHER", "work secret", {"ts": "124"}))


def test_inactive_worker_not_enabled_by_allowlist(intake):
    runtime = object.__new__(InternManagementRuntime)
    runtime.config = SimpleNamespace(slack=SlackConfig(work_intake_beta_slack_user_ids=["OLD"]))
    runtime.roster_by_slack_id = {"OLD": UserProfile(user_key="old", display_name="Old", active=False)}
    runtime.admin_profile_by_slack_user_id = lambda uid: None
    assert asyncio.run(runtime._handle_slack_work_intake("OLD", "work shop", {"ts": "123"}))


def test_beta_enrollment_is_explicit_and_independent_of_portal():
    from agent.config import _parse_slack_config

    config = _parse_slack_config({"worker_portal_beta_slack_user_ids": ["OLD"]})
    assert config.work_intake_beta_slack_user_ids == []
    configured = _parse_slack_config({"work_intake_beta_slack_user_ids": ["ERIK"]})
    assert configured.work_intake_beta_slack_user_ids == ["ERIK"]


def test_real_slack_dispatch_precedes_admin_router_and_does_not_touch_attendance(intake):
    runtime = object.__new__(InternManagementRuntime)
    runtime.config = SimpleNamespace(timezone="America/Los_Angeles", labor=LaborConfig(), slack=SlackConfig(work_intake_beta_slack_user_ids=["ERIK"]))
    runtime.state_store = intake.store
    runtime.roster_by_slack_id = {}
    runtime.admin_profile_by_slack_user_id = lambda uid: AdminProfile(name="Erik", discord_user_id=1, slack_user_id="ERIK")
    sent = []

    async def refresh():
        pass

    async def post(user, message):
        sent.append(message)

    runtime.refresh_configuration = refresh
    runtime.slack = SimpleNamespace(post_message=post)
    event = {"user": "ERIK", "ts": "100.1", "text": "work\nproposals: prepare customer meeting"}
    asyncio.run(runtime.handle_slack_direct_message(None, event))
    asyncio.run(runtime.handle_slack_direct_message(None, event))
    assert len(intake.pending_exceptions()) == 1
    assert len(sent) == 2  # retries can repeat acknowledgement, not records
    with intake.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_pending_work_appears_in_existing_manager_queue(intake, monkeypatch, tmp_path):
    from agent import manager_exceptions

    async def work_payload(*args, **kwargs):
        return {"people": []}

    monkeypatch.setattr(manager_exceptions, "build_work_dashboard_payload", work_payload)
    send(intake, "work Bagworm: test the mounting fixture")
    runtime = SimpleNamespace(config=SimpleNamespace(slack=SlackConfig(work_intake_beta_slack_user_ids=["WORKER"])),
                              state_store=intake.store)
    payload = asyncio.run(manager_exceptions.build_manager_exceptions_payload(runtime, tmp_path))
    assert payload["exception_count"] == 1
    assert payload["exceptions"][0]["category"] == "work_alignment"


def test_work_update_counts_as_activity_without_starting_or_extending_a_closed_clock(intake):
    runtime = object.__new__(InternManagementRuntime)
    runtime.config = SimpleNamespace(slack=SlackConfig(work_intake_beta_slack_user_ids=["WORKER"]))
    runtime.state_store = intake.store
    start = datetime.fromisoformat("2026-09-07T09:00:00+00:00")
    note = datetime.fromisoformat("2026-09-07T11:00:00+00:00")
    runtime._last_inbound_check_in_at = lambda *args, **kwargs: start
    runtime.resolve_user_timezone_name = lambda user: "UTC"
    user = UserProfile(user_key="worker", display_name="Worker", slack_user_id="WORKER")
    session = SessionState(user_key="worker", session_date="2026-09-07", stage="active", clocked_in_at=start.isoformat())
    intake.handle(actor_id="WORKER", actor_name="Worker", is_manager=False,
                  text="work GRASP: check wheel clearance", event_id="new", now=note)
    assert runtime._inactivity_auto_clock_out_reference_at(user, session, reference=note) == note
    assert session.clocked_in_at == start.isoformat()
    session.clocked_out_at = note.isoformat()
    assert runtime._inactivity_auto_clock_out_reference_at(user, session, reference=note) is None

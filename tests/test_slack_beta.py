import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from agent.config import parse_agent_config
from agent.models import UserProfile
from agent.runtime import InternManagementRuntime
from agent.slack_receiver import handle_clock_action
from agent.state_store import StateStore


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runtime = object.__new__(InternManagementRuntime)
    runtime.config = parse_agent_config({"admin_discord_user_id": "1", "admins": [{"name": "Erik", "discord_user_id": "1", "slack_user_id": "ERIK"}], "clickup": {"workspace_id": "unused"},
                                        "slack": {"enabled": True, "work_intake_beta_slack_user_ids": ["WORKER", "ERIK"]}}, "America/Los_Angeles")
    runtime.bootstrap = SimpleNamespace(default_timezone="America/Los_Angeles")
    runtime.state_store = StateStore(tmp_path / "state.sqlite3")
    user = UserProfile(user_key="worker", display_name="Worker", slack_user_id="WORKER")
    runtime.roster_by_slack_id = {"WORKER": user}
    runtime.roster_by_key = {"worker": user}
    runtime.clickup = None
    runtime.test_sent = []
    runtime.test_archives = []

    async def refresh(*args, **kwargs):
        pass

    async def post(channel, text):
        runtime.test_sent.append((channel, text))

    async def archive(user, session):
        runtime.test_archives.append(session.session_date)

    runtime.refresh_configuration = refresh
    runtime._archive_session = archive
    runtime.slack = SimpleNamespace(post_message=post)
    return runtime


def event(text, hour=9, user="WORKER"):
    return {"user": user, "text": text, "ts": str(datetime.fromisoformat(f"2026-09-07T{hour:02d}:00:00-07:00").timestamp())}


def test_quiet_clock_tick_delivers_work_checkin_after_notice_cooldown(runtime):
    from agent.slack_beta import ledger, tick

    runtime.config.slack.progress_checkins_enabled = True
    user = runtime.roster_by_key["worker"]
    start = datetime.fromisoformat("2026-09-08T08:00:00-07:00")
    ledger(runtime).handle(user, "in", "onsite", event_id="start", now=start, kiosk_verified=True)
    asyncio.run(tick(runtime, user, start + timedelta(hours=2)))
    assert len(runtime.test_sent) == 1
    assert "paid rest" in runtime.test_sent[0][1]
    before = runtime.state_store.get_session(user.user_key, "2026-09-08")
    archives = list(runtime.test_archives)
    asyncio.run(tick(runtime, user, start + timedelta(hours=2, minutes=29)))
    assert len(runtime.test_sent) == 1
    # No new clock notice: this was previously never evaluated for progress.
    asyncio.run(tick(runtime, user, start + timedelta(hours=2, minutes=30)))
    assert len(runtime.test_sent) == 2
    assert runtime.test_sent[-1][0] == user.slack_user_id
    assert "Quick check-in" in runtime.test_sent[-1][1]
    asyncio.run(tick(runtime, user, start + timedelta(hours=2, minutes=31)))
    assert len(runtime.test_sent) == 2
    assert runtime.state_store.get_session(user.user_key, "2026-09-08") == before
    assert runtime.test_archives == archives


@pytest.mark.parametrize("mode", ["not_started", "disabled", "handover", "clocked_out"])
def test_quiet_work_checkin_respects_clock_and_rollout_state(runtime, mode):
    from agent.slack_beta import ledger, tick

    runtime.config.slack.progress_checkins_enabled = mode != "disabled"
    user = runtime.roster_by_key["worker"]
    start = datetime.fromisoformat("2026-09-08T08:00:00-07:00")
    if mode != "not_started":
        ledger(runtime).handle(user, "in", "onsite", event_id="start", now=start, kiosk_verified=True)
        ledger(runtime).tick(user, start + timedelta(hours=2))
        for notice in ledger(runtime).pending_notices(user.user_key):
            ledger(runtime).notice_delivered(notice["id"], start + timedelta(hours=2))
        if mode == "clocked_out":
            ledger(runtime).handle(user, "out", "", event_id="out", now=start + timedelta(hours=2))
    if mode == "handover":
        runtime.config.slack.clock_handover_pending_slack_user_ids = [user.slack_user_id]
    with runtime.state_store._connect() as conn:
        before = list(conn.execute("SELECT payload FROM sessions"))
    asyncio.run(tick(runtime, user, start + timedelta(hours=2, minutes=30)))
    assert not runtime.test_sent
    with runtime.state_store._connect() as conn:
        assert list(conn.execute("SELECT payload FROM sessions")) == before


def test_actual_slack_path_clocks_without_clickup_or_openai(runtime):
    from agent.slack_beta import ledger
    asyncio.run(runtime.handle_slack_direct_message(None, event("clock in onsite")))
    assert not runtime.state_store.get_session("worker", "2026-09-07").clocked_in_at
    assert "enter your PIN" in runtime.test_sent[-1][1]
    ledger(runtime).handle(runtime.roster_by_key["worker"], "in", "onsite",
                           event_id="kiosk-confirm", now=datetime.fromisoformat("2026-09-07T09:00:00-07:00"), kiosk_verified=True)
    asyncio.run(runtime.handle_slack_direct_message(None, event("clock out", 11)))
    session = runtime.state_store.get_session("worker", "2026-09-07")
    assert session.time_summary["clocked_in_total_seconds"] == 7200
    assert runtime.test_sent[-1][1].startswith("Clocked out")
    assert runtime.test_archives


def test_worker_cannot_add_hours_or_authorize_remote_work(runtime):
    asyncio.run(runtime.handle_slack_direct_message(None, event("hours add worker 2026-09-07T08:00:00-07:00 2026-09-07T09:00:00-07:00 guessed")))
    assert "Only configured managers" in runtime.test_sent[-1][1]
    asyncio.run(runtime.handle_slack_direct_message(None, event("hours authorize worker remote 2026-09-08T10:00:00-07:00 2026-09-08T12:00:00-07:00 approved")))
    assert "Only Erik" in runtime.test_sent[-1][1]
    assert not runtime.state_store.get_session("worker", "2026-09-07").clocked_in_at


def test_clock_button_uses_server_action_mapping_not_untrusted_value(runtime):
    now = event("unused")["ts"]
    asyncio.run(handle_clock_action(runtime, None, {"user": {"id": "WORKER"}, "actions": [{"action_id": "dp_clock_in", "action_ts": now, "value": "hours authorize all overtime"}]}))
    assert not runtime.state_store.get_session("worker", "2026-09-07").clocked_in_at
    assert "enter your PIN" in runtime.test_sent[-1][1]
    view = runtime.build_slack_app_home_view("WORKER")
    assert view["blocks"][1]["elements"][0]["action_id"] == "dp_clock_in"
    assert all(len(block["elements"]) <= 5 for block in view["blocks"] if block["type"] == "actions")


def test_old_portal_cannot_change_beta_clock(runtime, monkeypatch):
    from agent import worker_portal

    monkeypatch.setattr(worker_portal, "validate_worker_portal_token", lambda *args: "WORKER")
    with pytest.raises(ValueError, match="beta time clock is in Slack"):
        asyncio.run(worker_portal.WorkerPortalService(runtime).apply_action("test", {"action": "start"}))


def test_legacy_portal_reads_cannot_enforce_or_mutate_during_cutover(runtime, monkeypatch):
    from agent import worker_portal
    monkeypatch.setattr(worker_portal, "validate_worker_portal_token", lambda *args: "NOT_ENROLLED")
    with pytest.raises(ValueError, match="legacy portal is paused"):
        asyncio.run(worker_portal.WorkerPortalService(runtime).build_payload("test"))
    with runtime.state_store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_home_status_does_not_create_a_shift(runtime):
    view = runtime.build_slack_app_home_view("WORKER")
    assert "Clocked out" in view["blocks"][2]["text"]["text"]
    assert view["blocks"][0]["text"]["text"] == "Don Pollo · Hours & work"
    action_ids = [e["action_id"] for b in view["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert "dp_clock_pin_setup" in action_ids
    assert "Don Pollo Project Updates" in view["blocks"][4]["text"]["text"]
    with runtime.state_store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_decision_notification_is_durable_and_cohort_scoped(runtime):
    from agent.slack_beta import flush_work_notices
    from agent.slack_work_intake import SlackWorkIntake
    intake = SlackWorkIntake(runtime.state_store)
    with runtime.state_store._connect() as conn:
        conn.execute("INSERT INTO work_decision_notices(owner_id,text) VALUES ('WORKER','Approved plan')")
        conn.execute("INSERT INTO work_decision_notices(owner_id,text) VALUES ('NOT_ENROLLED','Not for pilot')")
    asyncio.run(flush_work_notices(runtime))
    asyncio.run(flush_work_notices(runtime))
    assert runtime.test_sent == [('WORKER', 'Approved plan')]
    assert len(intake.pending_notices()) == 1


def test_slack_only_boot_does_not_require_discord_or_transcript_backfill(runtime, monkeypatch, tmp_path):
    from agent import main, slack_beta

    runtime.bootstrap.state_db_path = tmp_path / "state.sqlite3"
    called = []

    async def run(active):
        called.append(active)

    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(main, "InternManagementRuntime", lambda: runtime)
    monkeypatch.setattr(main, "SingleInstanceLock", lambda *args: nullcontext())
    monkeypatch.setattr(slack_beta, "run_slack_only", run)
    monkeypatch.setattr(main, "load_dotenv", lambda: None)
    main.main()
    assert called == [runtime]

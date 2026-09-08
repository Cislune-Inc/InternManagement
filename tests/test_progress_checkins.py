from copy import deepcopy
from datetime import datetime, timedelta

from agent.models import UserProfile
from agent.progress_checkins import ProgressCheckins
from agent.slack_timekeeping import SlackTimekeeping
from agent.slack_work_intake import SlackWorkIntake
from agent.state_store import StateStore


def setup(tmp_path):
    store = StateStore(tmp_path / "state.db")
    now = datetime.fromisoformat("2026-09-08T08:00:00-07:00")
    _, session = SlackTimekeeping(store).handle(UserProfile(user_key="worker", display_name="Worker"), "in", "onsite", event_id="start", now=now)
    return store, now, session


def test_two_hour_prompt_is_durable_capped_and_never_changes_hours(tmp_path):
    store, start, session = setup(tmp_path)
    service = ProgressCheckins(store)
    original = deepcopy(session)
    assert service.claim("WORKER", session, start + timedelta(minutes=119)) is None
    assert "one concrete sentence" in service.claim("WORKER", session, start + timedelta(hours=2)).lower()
    assert ProgressCheckins(store).claim("WORKER", session, start + timedelta(hours=2)) is None
    assert service.claim("WORKER", session, start + timedelta(hours=4))
    assert service.claim("WORKER", session, start + timedelta(hours=6))
    assert service.claim("WORKER", session, start + timedelta(hours=8)) is None
    assert session == original


def test_snooze_recent_update_and_break_return_suppress(tmp_path):
    store, start, session = setup(tmp_path)
    service = ProgressCheckins(store)
    service.snooze("WORKER", session.session_date, start + timedelta(hours=2))
    assert service.claim("WORKER", session, start + timedelta(hours=2, minutes=59)) is None
    assert service.claim("WORKER", session, start + timedelta(hours=3))
    intake = SlackWorkIntake(store)
    for i, text in enumerate(["work GRASP: review comparison", "work next Review result"]):
        intake.handle(actor_id="WORKER", actor_name="Worker", is_manager=False, text=text,
                      event_id=str(i), now=start + timedelta(hours=4))
    assert service.claim("WORKER", session, start + timedelta(hours=5)) is None
    session.metadata["slack_clock_rest_started_at"] = (start + timedelta(hours=6)).isoformat()
    assert service.claim("WORKER", session, start + timedelta(hours=6)) is None
    del session.metadata["slack_clock_rest_started_at"]
    session.metadata["paid_rest_windows"] = [{"ended_at": (start + timedelta(hours=6, minutes=10)).isoformat()}]
    assert service.claim("WORKER", session, start + timedelta(hours=7)) is None
    session.clocked_out_at = (start + timedelta(hours=7)).isoformat()
    assert service.claim("WORKER", session, start + timedelta(hours=10)) is None


def test_meal_and_not_clocked_in_never_prompt(tmp_path):
    store, start, session = setup(tmp_path)
    service = ProgressCheckins(store)
    session.stage = "on_lunch_break"
    assert service.claim("WORKER", session, start + timedelta(hours=3)) is None
    assert service.claim("WORKER", None, start) is None


def test_work_prompt_does_not_stack_on_clock_notice(tmp_path):
    store, start, session = setup(tmp_path)
    service = ProgressCheckins(store)
    session.metadata["slack_clock_last_notice_at"] = (start + timedelta(hours=2)).isoformat()
    assert service.claim("WORKER", session, start + timedelta(hours=2)) is None
    assert service.claim("WORKER", session, start + timedelta(hours=2, minutes=29)) is None
    assert service.claim("WORKER", session, start + timedelta(hours=2, minutes=30))

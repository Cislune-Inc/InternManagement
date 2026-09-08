from datetime import datetime, timedelta

import pytest

from agent.models import SessionState, UserProfile
from agent.slack_timekeeping import SlackTimekeeping, clock_command, paid_seconds
from agent.state_store import StateStore


def at(hour, minute=0, day=7):
    return datetime.fromisoformat(f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00-07:00")


@pytest.fixture
def clock(tmp_path):
    return SlackTimekeeping(StateStore(tmp_path / "clock.sqlite3"))


@pytest.fixture
def user():
    return UserProfile(user_key="worker", display_name="Worker", slack_user_id="WORKER")


def command(clock, user, name, now, detail="", event=None):
    return clock.handle(user, name, detail, event_id=event or now.isoformat() + name, now=now)


def test_handover_hold_prevents_new_time_not_actual_hours_reports(clock, user):
    pending = SlackTimekeeping(clock.store, handover_pending_user_keys=(user.user_key,))
    for cmd, detail in [("in", "onsite"), ("in", "remote"), ("back", "")]:
        response, session = command(pending, user, cmd, at(9), detail)
        assert "handover is not finished" in response and session is None
    with clock.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM slack_clock_receipts").fetchone()[0] == 0
    assert "exclude your Gusto" in pending.snapshot(user, at(9))
    assert "saved" in command(pending, user, "report", at(9), "Actual earlier work needs review")[0]
    assert len(pending.reports()) == 1
    assert "Clocked in" in command(clock, user, "in", at(9), "onsite")[0]
    assert "Clocked out" in command(pending, user, "out", at(10))[0]
    assert paid_seconds([clock.store.get_session(user.user_key, "2026-09-07")], at(10)) == 3600


def test_clock_records_hours_without_task_or_quality_gate(clock, user):
    response, _ = command(clock, user, "in", at(9), "onsite stuff")
    assert "Clocked in" in response
    _, stopped = command(clock, user, "out", at(11))
    assert paid_seconds([stopped], at(11)) == 7200
    assert stopped.latest_plan == "stuff"
    assert not stopped.awaiting_clock_out_summary
    assert not stopped.metadata.get("compliance_events")


def test_duplicate_start_and_delayed_retry_never_reopen(clock, user):
    original, _ = command(clock, user, "in", at(9), "onsite", event="start")
    assert "already clocked" in command(clock, user, "in", at(9, 1), "onsite")[0]
    command(clock, user, "out", at(10))
    reloaded = SlackTimekeeping(clock.store)
    response, session = command(reloaded, user, "in", at(9), "onsite", event="start")
    assert response == original
    assert session.clocked_out_at
    assert paid_seconds([session], at(12)) == 3600


def test_self_reported_meal_is_deducted_once(clock, user):
    command(clock, user, "in", at(9), "onsite")
    command(clock, user, "lunch", at(12))
    assert "20:00 remaining" in command(clock, user, "back", at(12, 10))[0]
    command(clock, user, "back", at(12, 30))
    _, session = command(clock, user, "out", at(17))
    assert paid_seconds([session], at(17)) == int(7.5 * 3600)
    assert len(session.metadata["lunch_windows"]) == 1


def test_meal_deadline_stops_without_fabricating_unpaid_meal(clock, user):
    command(clock, user, "in", at(9), "onsite")
    notices, session = clock.tick(user, at(14))
    assert "meal due" in notices[0]
    assert not session.metadata.get("lunch_windows")
    assert paid_seconds([session], at(15)) == 5 * 3600
    assert "Take your meal" in command(clock, user, "in", at(14, 1), "onsite")[0]
    command(clock, user, "lunch", at(14, 2))
    _, resumed = command(clock, user, "back", at(14, 32))
    assert resumed.stage == "active"
    assert paid_seconds([resumed], at(15, 32)) == 6 * 3600


def test_short_reported_meal_keeps_time_paid_pending_review(clock, user):
    command(clock, user, "in", at(9), "onsite")
    command(clock, user, "lunch", at(12))
    _, session = command(clock, user, "out", at(12, 12))
    assert paid_seconds([session], at(12, 12)) == 192 * 60
    assert session.metadata["lunch_windows"][0]["paid_pending_review"]


def test_clock_out_finishes_meal_after_automatic_stop_without_restarting(clock, user):
    command(clock, user, "in", at(9), "onsite")
    clock.tick(user, at(14))
    command(clock, user, "lunch", at(14, 1))
    _, session = command(clock, user, "out", at(14, 31))
    assert not session.metadata.get("slack_clock_meal_started_at")
    assert session.stage == "clocked_out"
    assert paid_seconds([session], at(15)) == 5 * 3600


def test_remote_approval_expiring_during_meal_cannot_resume_work(clock, user):
    clock.authorize(user.user_key, "remote", at(9, day=8), at(12, 15, day=8), approver="ERIK", reason="planned remote work", now=at(8, day=7))
    command(clock, user, "in", at(9, day=8), "remote")
    command(clock, user, "lunch", at(12, day=8))
    response, session = command(clock, user, "back", at(12, 30, day=8))
    assert "remote authorization has expired" in response
    assert session.clocked_out_at
    assert paid_seconds([session], at(13, day=8)) == 3 * 3600


def test_paid_rest_timeout_preserves_elapsed_time_and_requires_return(clock, user):
    command(clock, user, "in", at(9), "onsite")
    command(clock, user, "rest", at(11))
    _, session = clock.tick(user, at(11, 11))
    assert paid_seconds([session], at(12)) == 131 * 60
    assert "Reply `back`" in command(clock, user, "in", at(11, 12), "onsite")[0]
    command(clock, user, "back", at(11, 13))
    assert "Clocked in" in command(clock, user, "in", at(11, 14), "onsite")[0]


def test_daily_and_weekly_limits_preserve_overtime_already_worked(clock, user):
    user.meal_tracking_required = False
    command(clock, user, "in", at(9), "onsite")
    _, session = clock.tick(user, at(17, 1))
    assert paid_seconds([session], at(18)) == 481 * 60
    assert session.metadata["slack_clock_stop_reason"] == "hours_limit"
    assert "limit is reached" in command(clock, user, "in", at(17, 2), "onsite")[0]
    for day in range(8, 12):
        command(clock, user, "in", at(9, day=day), "onsite")
        command(clock, user, "out", at(17, day=day))
    assert "limit is reached" in command(clock, user, "in", at(9, day=12), "onsite")[0]


def test_advance_remote_approval_is_scoped_and_expires(clock, user):
    assert "needs Erik" in command(clock, user, "in", at(9), "remote")[0]
    with pytest.raises(ValueError, match="24 hours"):
        clock.authorize(user.user_key, "remote", at(10), at(12), approver="ERIK", reason="test", now=at(9))
    clock.authorize(user.user_key, "remote", at(10, day=8), at(12, day=8), approver="ERIK", reason="approved visit", now=at(9))
    assert "Clocked in" in command(clock, user, "in", at(10, day=8), "remote")[0]
    _, session = clock.tick(user, at(12, day=8))
    assert session.metadata["slack_clock_stop_reason"] == "remote_approval_expired"


def test_inactivity_has_four_hour_ceiling_and_warning(clock, user):
    user.meal_tracking_required = False
    command(clock, user, "in", at(9), "onsite")
    assert not any("four hours" in text for text in clock.tick(user, at(12, 59))[0])
    notices, session = clock.tick(user, at(13))
    assert any("four hours" in text for text in notices)
    assert not session.clocked_out_at
    clock.record_activity(user, "Test saved and fixture still needs adjustment", at(13, 10))
    assert clock.tick(user, at(13, 20))[1] is None


def test_notifications_are_durable_and_retryable(clock, user):
    command(clock, user, "in", at(9), "onsite")
    clock.tick(user, at(14))
    notices = SlackTimekeeping(clock.store).pending_notices(user.user_key)
    assert notices
    clock.notice_delivered(notices[0]["id"], at(14, 1))
    assert clock.pending_notices(user.user_key) == []


def test_hours_report_survives_even_with_conflicting_shifts(clock, user):
    for day in (6, 7):
        clock.store.save_session(SessionState(user_key=user.user_key, session_date=f"2026-09-{day:02d}", clocked_in_at=at(9, day=day).isoformat()))
    response, _ = command(clock, user, "report", at(10), "I worked yesterday 09:00–17:00, lunch 12:00–12:30")
    assert "saved" in response
    assert len(clock.reports()) == 1


def test_manager_added_actual_hours_overlap_once_and_reconcile(clock, user):
    command(clock, user, "in", at(9), "onsite")
    command(clock, user, "out", at(11))
    clock.add_actual_hours(user, at(8), at(10), actor_id="ERIK", event_id="fix", reason="worker reported actual start", now=at(12))
    assert "today 3.00 h" in command(clock, user, "hours", at(12))[0]
    command(clock, user, "report", at(12, 1), "Started at eight", event="report")
    ident = clock.reports()[0]["id"]
    assert "reconciled" in clock.resolve_report(ident, actor_id="ERIK", note="Added 08:00 start, checked breaks", now=at(12, 2))
    assert clock.reports() == []


def test_overlapping_segments_and_dst_count_elapsed_time():
    session = SessionState(user_key="w", session_date="2026-11-01", work_segments=[
        {"clocked_in_at": "2026-11-01T01:30:00-07:00", "clocked_out_at": "2026-11-01T02:30:00-08:00"},
        {"clocked_in_at": "2026-11-01T01:45:00-07:00", "clocked_out_at": "2026-11-01T02:00:00-08:00"},
    ])
    assert paid_seconds([session], datetime.fromisoformat("2026-11-01T03:00:00-08:00")) == 7200


def test_clock_parser_does_not_treat_discussion_as_a_clock_command():
    assert clock_command("clock in onsite GRASP wheel test") == ("in", "onsite GRASP wheel test")
    assert clock_command("I think we should clock out people earlier") is None
    assert clock_command("report hours I missed lunch") == ("report", "I missed lunch")

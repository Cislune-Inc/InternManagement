"""Isolated clocks only: return boundaries and private, durable ready notices."""
import asyncio
from datetime import timedelta

import pytest

from agent.slack_timekeeping import SlackTimekeeping, paid_seconds
from test_slack_timekeeping import at, clock, command, user
from test_slack_beta import runtime


@pytest.mark.parametrize("action,minutes,field", [
    ("rest", 10, "slack_clock_rest_started_at"),
    ("lunch", 30, "slack_clock_meal_started_at"),
])
def test_early_return_blocked_at_server_exact_boundary_allowed(clock, user, action, minutes, field):
    clock.require_kiosk = True
    clock.handle(user, "in", "onsite", event_id="start", now=at(9), kiosk_verified=True)
    command(clock, user, action, at(11))
    before = at(11) + timedelta(minutes=minutes, seconds=-1)
    response, session = clock.handle(user, "back", "", event_id="early", now=before, kiosk_verified=True)
    assert "00:01 remaining" in response
    assert session.metadata[field]
    assert not session.clocked_out_at
    exact = before + timedelta(seconds=1)
    assert command(clock, user, "back", exact)[0].startswith("KIOSK_REQUIRED")
    response, session = clock.handle(user, "back", "", event_id="return", now=exact, kiosk_verified=True)
    assert not session.metadata.get(field)
    assert not session.clocked_out_at
    assert paid_seconds([session], exact) == (130 if action == "rest" else 120) * 60


@pytest.mark.parametrize("action,minutes", [("rest", 10), ("lunch", 30)])
def test_clock_out_and_midnight_do_not_bypass_minimum_or_block_reports(clock, user, action, minutes):
    command(clock, user, "in", at(23), "onsite")
    command(clock, user, action, at(23, 58))
    _, stopped = command(clock, user, "out", at(23, 59))
    assert stopped.clocked_out_at
    assert paid_seconds([stopped], at(0, day=8)) == 59 * 60
    response, _ = command(clock, user, "in", at(0, day=8), "onsite")
    assert "remaining" in response
    assert "saved" in command(clock, user, "report", at(0, 1, day=8), "Actually worked after the timer stopped")[0]
    assert clock.tick(user, at(1, day=8))[0] == []  # Ended day: no ready notice.
    assert "Clocked in" in command(clock, user, "in", at(1, 1, day=8), "onsite")[0]


@pytest.mark.parametrize("action,minutes", [("rest", 10), ("lunch", 30)])
def test_ready_once_per_break_survives_restart_and_does_not_resume(clock, user, action, minutes):
    clock.require_kiosk = True
    clock.handle(user, "in", "onsite", event_id="start", now=at(9), kiosk_verified=True)
    command(clock, user, action, at(11))
    due = at(11) + timedelta(minutes=minutes)
    assert clock.tick(user, due - timedelta(seconds=1))[0] == []
    notices, session = clock.tick(user, due)
    assert len(notices) == 1
    assert "is complete" in notices[0] and "shop Mini" in notices[0]
    assert "No Slack reply needed" in notices[0]
    assert session.metadata.get("slack_clock_rest_started_at" if action == "rest" else "slack_clock_meal_started_at")
    if action == "rest":
        assert session.clocked_out_at
        assert "clock paused" in notices[0]
        assert paid_seconds([session], due + timedelta(hours=1)) == 130 * 60
    else:
        assert session.stage == "on_lunch_break"
        assert session.metadata["lunch_windows"][0]["ended_at"] is None
    reloaded = SlackTimekeeping(clock.store, require_kiosk=True)
    assert reloaded.tick(user, due + timedelta(minutes=1))[0] == []
    assert len(reloaded.pending_notices(user.user_key)) == 1
    reloaded.notice_delivered(reloaded.pending_notices(user.user_key)[0]["id"], due)
    assert reloaded.pending_notices(user.user_key) == []


@pytest.mark.parametrize("action,minutes", [("rest", 10), ("lunch", 30)])
@pytest.mark.parametrize("finish", ["back", "out"])
def test_no_stale_ready_after_return_or_clock_out(clock, user, action, minutes, finish):
    command(clock, user, "in", at(9), "onsite")
    command(clock, user, action, at(11))
    due = at(11) + timedelta(minutes=minutes)
    clock.tick(user, due)
    assert len(clock.pending_notices(user.user_key)) == 1
    command(clock, user, finish, due + timedelta(minutes=1))
    assert clock.pending_notices(user.user_key) == []
    assert not any("is complete" in text for text in clock.tick(user, due + timedelta(minutes=2))[0])


def test_repeated_rest_gets_new_notice_and_old_one_is_suppressed(clock, user):
    command(clock, user, "in", at(9), "onsite")
    command(clock, user, "rest", at(10))
    clock.tick(user, at(10, 10))
    command(clock, user, "back", at(10, 11))
    command(clock, user, "in", at(10, 11), "onsite")
    command(clock, user, "rest", at(11))
    clock.tick(user, at(11, 10))
    assert len(clock.pending_notices(user.user_key)) == 1
    with clock.store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM slack_clock_return_notices").fetchone()[0] == 2


def test_legacy_rest_cross_midnight_retains_actual_return(clock, user):
    command(clock, user, "in", at(23), "onsite")
    _, session = command(clock, user, "rest", at(23, 55))
    session.metadata.pop("slack_clock_return_not_before")
    clock.store.save_session(session)
    assert "remaining" in command(clock, user, "back", at(0, day=8))[0]
    clock.tick(user, at(0, 5, day=8))
    response, returned = command(clock, user, "back", at(0, 6, day=8))
    assert "rest return is recorded" in response
    assert returned.session_date == "2026-09-07"


def test_remote_ready_does_not_require_mini_and_expired_approval_blocks_return(clock, user):
    clock.require_kiosk = True
    clock.authorize(user.user_key, "remote", at(9, day=8), at(11, 11, day=8),
                    approver="ERIK", reason="planned remote", now=at(8))
    command(clock, user, "in", at(9, day=8), "remote")
    command(clock, user, "rest", at(11, day=8))
    ready, _ = clock.tick(user, at(11, 10, day=8))
    assert "Reply `back`" in ready[0] and "shop Mini" not in ready[0]
    response, session = command(clock, user, "back", at(11, 12, day=8))
    assert "remote authorization has expired" in response
    assert session.clocked_out_at


def test_runtime_notice_is_private_retryable_and_not_an_admin_alert(runtime):
    from agent.slack_beta import ledger, tick
    user = runtime.roster_by_key["worker"]
    service = ledger(runtime)
    service.handle(user, "in", "onsite", event_id="start", now=at(9), kiosk_verified=True)
    command(service, user, "lunch", at(11))
    original_post = runtime.slack.post_message

    async def unavailable(*args):
        raise ConnectionError("synthetic Slack outage")

    runtime.slack.post_message = unavailable
    with pytest.raises(ConnectionError):
        asyncio.run(tick(runtime, user, at(11, 30)))
    assert len(service.pending_notices(user.user_key)) == 1
    runtime.slack.post_message = original_post
    asyncio.run(tick(runtime, user, at(11, 31)))
    asyncio.run(tick(runtime, user, at(11, 32)))
    assert len(runtime.test_sent) == 1
    assert runtime.test_sent[0][0] == "WORKER"
    assert "shop Mini" in runtime.test_sent[0][1]
    assert service.pending_notices(user.user_key) == []

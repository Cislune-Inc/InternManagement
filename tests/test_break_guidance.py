"""Isolated-ledger guidance and payment-preservation regressions."""
from dataclasses import asdict
from agent.break_guidance import completed_rests
from agent.slack_timekeeping import paid_seconds, clock_command
from test_slack_timekeeping import at, clock, command, user


def test_extra_pause_has_no_new_minimum_and_does_not_replace_required_rest(clock, user):
    command(clock, user, "in", at(9), "onsite")
    command(clock, user, "rest", at(11))
    command(clock, user, "back", at(11, 10))
    response, _ = command(clock, user, "rest", at(12))
    assert "Extra paid pause" in response
    response, session = command(clock, user, "back", at(12, 2))
    assert "Welcome back" in response
    assert len(completed_rests(session)) == 1
    assert paid_seconds([session], at(12, 2)) == 182 * 60
    assert clock_command("pause") == ("pause", "")
    assert clock_command("required rest") == ("rest", "required")


def test_pause_cannot_shorten_running_required_rest(clock, user):
    command(clock, user, "in", at(9), "onsite")
    command(clock, user, "rest", at(11))
    assert "already running" in command(clock, user, "pause", at(11, 2))[0]
    assert "remaining" in command(clock, user, "back", at(11, 3))[0]
    assert "Welcome back" in command(clock, user, "back", at(11, 10))[0]


def test_long_rest_gets_one_reminder_and_no_automatic_deduction(clock, user):
    command(clock, user, "in", at(9), "onsite")
    command(clock, user, "rest", at(11))
    notices, _ = clock.tick(user, at(11, 10))
    assert len(notices) == 1 and "more personal time, clock out" in notices[0]
    assert clock.tick(user, at(11, 21))[0] == []
    _, session = command(clock, user, "back", at(11, 25))
    assert paid_seconds([session], at(11, 25)) == 145 * 60
    assert session.metadata["paid_rest_windows"][0]["paid_pending_review"]


def test_lunch_runs_to_actual_return_not_thirty_minute_cap(clock, user):
    command(clock, user, "in", at(9), "onsite")
    command(clock, user, "lunch", at(12))
    clock.tick(user, at(12, 30))
    _, session = command(clock, user, "back", at(12, 45))
    assert paid_seconds([session], at(13)) == 195 * 60


def test_summary_read_only_shows_last_rest_and_next_action(clock, user):
    user.worker_type = "admin"
    command(clock, user, "in", at(9), "onsite")
    command(clock, user, "rest", at(11))
    command(clock, user, "back", at(11, 10))
    before = asdict(clock.store.get_session(user.user_key, "2026-09-07"))
    text = clock.snapshot(user, at(12))
    assert "11:00–11:10" in text
    assert "Completed 10-minute rests: 1" in text
    assert "No rest due now" in text
    assert "Owner practice" in text
    assert asdict(clock.store.get_session(user.user_key, "2026-09-07")) == before


def test_second_rest_prompt_uses_paid_hours_after_lunch(clock, user):
    command(clock, user, "in", at(9), "onsite")
    command(clock, user, "rest", at(11))
    command(clock, user, "back", at(11, 10))
    command(clock, user, "lunch", at(12))
    command(clock, user, "back", at(12, 30))
    assert not any("safe stopping point" in n for n in clock.tick(user, at(15))[0])
    notices, _ = clock.tick(user, at(15, 30))
    assert any("safe stopping point" in n for n in notices)
    assert not any("safe stopping point" in n for n in clock.tick(user, at(15, 31))[0])

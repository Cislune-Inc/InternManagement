"""Synthetic clocks only; no production staff data or Slack sends."""
import asyncio
from datetime import timedelta, timezone

import pytest

from agent.meal_corrections import MealCorrections, parse_interval
from agent.slack_timekeeping import paid_seconds
from test_slack_timekeeping import at, clock, command, user
from test_slack_beta import runtime, event


def seeded(clock, user):
    command(clock, user, "in", at(9), "onsite")
    return MealCorrections(clock)


def test_preview_is_not_mutation_confirm_preserves_start_and_retry_deducts_once(clock, user):
    service = seeded(clock, user)
    response, token = service.preview(user.user_key, at(11), at(12), now=at(13))
    assert "4.00 → 3.00" in response
    assert not clock.store.get_session(user.user_key, "2026-09-07").metadata.get("lunch_windows")
    response, session = service.confirm(user.user_key, token, now=at(13, 1))
    assert "correction applied" in response
    assert session.clocked_in_at == at(9).astimezone(timezone.utc).isoformat()
    assert not session.clocked_out_at
    assert paid_seconds([session], at(14)) == 4 * 3600
    assert session.metadata["meal_correction_audit"][0]["before"]["metadata"]["lunch_windows"] is None
    again, _ = service.confirm(user.user_key, token, now=at(13, 2))
    assert "no duplicate" in again
    assert len(clock.store.get_session(user.user_key, "2026-09-07").metadata["lunch_windows"]) == 1


def test_replace_preserves_original_and_short_lunch_remains_paid(clock, user):
    service = seeded(clock, user)
    command(clock, user, "lunch", at(11))
    command(clock, user, "back", at(11, 30))
    _, token = service.preview(user.user_key, at(11), at(11, 15), now=at(12))
    result, session = service.confirm(user.user_key, token, now=at(12, 1))
    assert "paid pending review" in result
    assert paid_seconds([session], at(13)) == 4 * 3600
    assert len(session.metadata["lunch_windows"]) == 1
    assert session.metadata["meal_correction_audit"][0]["before"]["metadata"]["lunch_windows"][0]["ended_at"]


@pytest.mark.parametrize("change", ["clockout", "rest", "cancel", "new_preview", "expired", "other_actor"])
def test_confirmation_boundaries(clock, user, change):
    service = seeded(clock, user)
    _, token = service.preview(user.user_key, at(11), at(12), now=at(13))
    now, actor = at(13, 2), user.user_key
    if change == "clockout":
        command(clock, user, "out", at(13, 1))
    elif change == "rest":
        command(clock, user, "rest", at(13, 1))
    elif change == "cancel":
        service.cancel(user.user_key)
    elif change == "new_preview":
        service.preview(user.user_key, at(11), at(12), now=at(13, 1))
    elif change == "expired":
        now = at(14)
    else:
        actor = "someone-else"
    with pytest.raises(ValueError):
        service.confirm(actor, token, now=now)
    assert not clock.store.get_session(user.user_key, "2026-09-07").metadata.get("lunch_windows")


@pytest.mark.parametrize("start,end", [(at(8), at(10)), (at(12), at(14)), (at(12), at(11)), (at(11), at(11))])
def test_invalid_intervals_do_not_change_hours(clock, user, start, end):
    service = seeded(clock, user)
    with pytest.raises(ValueError):
        service.preview(user.user_key, start, end, now=at(13))


def test_gap_and_paid_rest_overlap_require_review(clock, user):
    service = seeded(clock, user)
    command(clock, user, "rest", at(10))
    command(clock, user, "back", at(10, 10))
    with pytest.raises(ValueError, match="paid rest"):
        service.preview(user.user_key, at(10), at(11), now=at(13))
    command(clock, user, "out", at(11))
    command(clock, user, "in", at(12), "onsite")
    with pytest.raises(ValueError, match="outside"):
        service.preview(user.user_key, at(11), at(12), now=at(13))


def test_confirm_resolves_only_explicit_linked_report(clock, user):
    service = seeded(clock, user)
    command(clock, user, "report", at(12), "Reported meal", event="meal-report")
    command(clock, user, "report", at(12, 1), "Unrelated historical hours", event="other-report")
    _, token = service.preview(user.user_key, at(11), at(12), now=at(13), source_report_id="worker:meal-report")
    service.confirm(user.user_key, token, now=at(13, 1))
    assert [r["id"] for r in clock.reports()] == ["worker:other-report"]


@pytest.mark.parametrize("text", ["fix lunch today 11am-12pm", "fix lunch 2026-09-07 11:00AM–12:00 PM", "fix lunch today 11am to 12pm"])
def test_explicit_parser(clock, text):
    assert parse_interval(text, at(13), clock.zone) == (at(11), at(12))


@pytest.mark.parametrize("text", ["fix lunch today 11-12", "fix lunch yesterday 11am-12pm", "fix lunch today 25am-12pm", "fix lunch today 11am-12pm maybe"])
def test_ambiguous_parser_does_not_guess(clock, text):
    with pytest.raises(ValueError):
        parse_interval(text, at(13), clock.zone)


def test_slack_preview_confirm_archives_and_lunch_prose_does_not_become_work(runtime):
    from agent.slack_beta import ledger
    worker = runtime.roster_by_key["worker"]
    ledger(runtime).handle(worker, "in", "onsite", event_id="kiosk", now=at(9), kiosk_verified=True)
    asyncio.run(runtime.handle_slack_direct_message(None, event("On lunch since 1130", 12)))
    assert "not a project update" in runtime.test_sent[-1][1]
    asyncio.run(runtime.handle_slack_direct_message(None, event("fix lunch today 11am-12pm", 13)))
    with runtime.state_store._connect() as conn:
        token = conn.execute("SELECT token FROM meal_correction_previews").fetchone()[0]
    asyncio.run(runtime.handle_slack_direct_message(None, {**event(f"confirm lunch {token}", 13), "ts": str(at(13, 1).timestamp())}))
    assert "correction applied" in runtime.test_sent[-1][1]
    assert runtime.test_archives
    session = runtime.state_store.get_session("worker", "2026-09-07")
    assert session.time_summary["clocked_in_total_seconds"] == 181 * 60


@pytest.mark.parametrize("action,minutes", [("lunch", 30), ("rest", 10)])
def test_slack_break_return_keeps_new_shift_kiosk_and_minimum_guards(clock, user, action, minutes):
    clock.require_kiosk = True
    clock.allow_slack_break_returns = True
    assert "KIOSK_REQUIRED" in command(clock, user, "in", at(9), "onsite")[0]
    clock.handle(user, "in", "onsite", event_id="kiosk", now=at(9), kiosk_verified=True)
    command(clock, user, action, at(11))
    due = at(11) + timedelta(minutes=minutes)
    assert "remaining" in command(clock, user, "back", due-timedelta(seconds=1))[0]
    clock.tick(user, due)
    response, returned = command(clock, user, "back", due+timedelta(minutes=1))
    assert not returned.clocked_out_at
    assert not returned.metadata.get("slack_clock_meal_started_at")
    assert not returned.metadata.get("slack_clock_rest_started_at")
    if action == "rest":
        assert not returned.metadata.get("slack_clock_return_gaps")
        assert returned.metadata["paid_rest_windows"][0]["ended_at"]
    command(clock, user, "out", at(13))
    assert "No active break" in command(clock, user, "back", at(13, 1))[0]
    assert "KIOSK_REQUIRED" in command(clock, user, "in", at(13, 2), "onsite")[0]

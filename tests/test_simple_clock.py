"""DP-only revision: isolated ledger, no real punches or messages."""
import re
from datetime import timedelta

from agent.clock_buttons import blocks
from agent.slack_timekeeping import SlackTimekeeping, paid_seconds, clock_command
from test_slack_timekeeping import at, clock, user, command


def simple(clock):
    return SlackTimekeeping(clock.store, simplified_flow=True)


def test_paid_rest_needs_no_return_and_never_invents_completed_window(clock, user):
    clock = simple(clock)
    command(clock, user, 'in', at(9), 'onsite')
    response, session = command(clock, user, 'rest', at(11))
    assert 'no logout or return message needed' in response
    assert clock.return_countdown([session], at(11, 1)) is None
    assert not session.metadata.get('paid_rest_windows')
    assert 'already running' in command(clock, user, 'back', at(11, 2))[0]
    assert paid_seconds([session], at(12)) == 3*3600


def test_lunch_reminders_are_bounded_no_auto_deduction_or_start_gate(clock, user):
    clock = simple(clock)
    command(clock, user, 'in', at(9), 'onsite')
    clock.record_activity(user, 'working on prototype', at(13))
    for now, phrase in [(at(13, 30), '30 minutes'), (at(13, 50), '10 minutes'), (at(14), 'Lunch is due now')]:
        notices, _ = clock.tick(user, now)
        assert any(phrase in n for n in notices)
        assert not clock.tick(user, now + timedelta(seconds=1))[0]
    session = clock.store.get_session(user.user_key, '2026-09-07')
    assert not session.clocked_out_at and not session.metadata.get('lunch_windows')
    command(clock, user, 'out', at(14, 1))
    assert 'Clocked in' in command(clock, user, 'in', at(14, 2), 'onsite')[0]


def test_one_review_confirmation_is_bound_to_worker_and_source(clock, user):
    clock = simple(clock)
    command(clock, user, 'in', at(9), 'onsite')
    reply, session = command(clock, user, 'out', at(11))
    detail = re.search(r'confirm day ([^`]+)', reply)[1]
    assert clock_command('confirm day ' + detail) == ('review', detail)
    assert blocks(reply)[1]['elements'][0]['value'] == detail
    assert 'saved' in command(clock, user, 'review', at(11, 1), detail)[0]
    command(clock, user, 'in', at(11, 2), 'onsite')
    command(clock, user, 'out', at(12))
    assert 'changed' in command(clock, user, 'review', at(12, 1), detail)[0]


def test_hours_limit_stays_enforced(clock, user):
    clock = simple(clock)
    command(clock, user, 'in', at(9), 'onsite')
    clock.record_activity(user, 'test results', at(16))
    notices, session = clock.tick(user, at(17))
    assert session.clocked_out_at and session.metadata['slack_clock_stop_reason'] == 'hours_limit'
    assert 'limit' in command(clock, user, 'in', at(17, 1), 'onsite')[0]


def test_lunch_still_requires_reported_return_and_no_duplicate_credit(clock, user):
    clock = simple(clock)
    command(clock, user, 'in', at(9), 'onsite')
    command(clock, user, 'lunch', at(12))
    assert 'remaining' in command(clock, user, 'back', at(12, 10))[0]
    command(clock, user, 'back', at(12, 30))
    clock.record_activity(user, 'test results', at(14))
    assert not any('lunch' in n.lower() for n in clock.tick(user, at(14, 30))[0])
    _, session = command(clock, user, 'out', at(15))
    assert paid_seconds([session], at(15)) == 5.5*3600


def test_late_notice_suppressed_after_worker_response(clock, user):
    clock = simple(clock)
    command(clock, user, 'in', at(9), 'onsite')
    clock.tick(user, at(13, 30))
    command(clock, user, 'lunch', at(13, 31))
    clock.record_activity(user, 'working update', at(13, 32))
    assert not any(n['text'].startswith(('Plan lunch', 'Still working?', 'At a safe')) for n in clock.pending_notices(user.user_key))


def test_inactivity_stop_is_explicitly_unconfirmed(clock, user):
    clock = simple(clock)
    command(clock, user, 'in', at(9), 'onsite')
    clock.tick(user, at(13))
    notices, session = clock.tick(user, at(13, 15))
    assert any('finish time is unconfirmed' in n for n in notices)
    assert session.metadata['compliance_events'][-1]['confirmation'] == 'stop_instruction_not_proof_of_stopped_work'
    message = next(n for n in notices if 'confirm stop' in n)
    detail = re.search(r'confirm stop ([^`]+)', message)[1]
    assert blocks(message)[1]['elements'][0]['value'] == detail
    response, confirmed = command(clock, user, 'confirm_stop', at(13, 16), detail)
    assert 'finish time is confirmed' in response
    assert confirmed.metadata['compliance_events'][-1]['confirmation'] == 'worker_confirmed_stop'
    assert paid_seconds([confirmed], at(15)) == 4.25*3600


def test_buttons_for_meal_and_correction_preview():
    assert blocks('Lunch is due now. Stop for lunch.')[1]['elements'][0]['action_id'] == 'dp_clock_lunch'
    assert blocks('*Lunch correction preview — today* Reply `confirm lunch aabb1234`')[1]['elements'][0]['value'] == 'aabb1234'
    assert blocks('ordinary project update') is None


def test_kiosk_checkout_queues_one_private_review_and_suppresses_after_confirmation(clock, user):
    clock = simple(clock)
    clock.handle(user, 'in', 'onsite', event_id='start', now=at(9), kiosk_verified=True)
    response, _ = clock.handle(user, 'out', '', event_id='finish', now=at(11), kiosk_verified=True)
    clock.handle(user, 'out', '', event_id='finish', now=at(11), kiosk_verified=True)
    assert len(clock.pending_notices(user.user_key)) == 1
    detail = re.search(r'confirm day ([^`]+)', response)[1]
    command(clock, user, 'review', at(11, 1), detail)
    assert clock.pending_notices(user.user_key) == []


def test_missing_meal_cannot_be_confirmed_as_clean_day(clock, user):
    clock = simple(clock)
    command(clock, user, 'in', at(9), 'onsite')
    response, session = command(clock, user, 'out', at(15))
    assert 'confirm day' not in response
    assert session.metadata['slack_clock_daily_review']['status'] == 'needs_correction'
    detail = session.session_date + ' ' + clock._review_token(session)
    assert 'unresolved' in command(clock, user, 'review', at(15, 1), detail)[0]
    assert paid_seconds([session], at(15)) == 6*3600


def test_legacy_open_rest_preserved_without_invented_end_or_return_gate(clock, user):
    command(clock, user, 'in', at(9), 'onsite')
    command(clock, user, 'rest', at(10))
    revised = simple(clock)
    reply, session = command(revised, user, 'back', at(10, 1))
    assert 'already running' in reply
    assert session.metadata['slack_clock_legacy_rest_reports'][0]['end_status'] == 'unconfirmed'
    assert not session.metadata.get('paid_rest_windows')
    assert paid_seconds([session], at(11)) == 7200


def test_fresh_channel_update_clears_warning_without_changing_punches(clock, user, monkeypatch):
    import asyncio
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from agent.channel_updates import handle
    from agent import slack_beta
    clock = simple(clock)
    now = datetime.now(timezone.utc)
    command(clock, user, 'in', now - timedelta(hours=4), 'onsite')
    clock.tick(user, now - timedelta(seconds=3))
    before = clock.current_session(user, now).work_segments.copy()
    monkeypatch.setattr(slack_beta, 'ledger', lambda runtime: clock)
    runtime = SimpleNamespace(state_store=clock.store, roster_by_slack_id={user.slack_user_id:user},
        config=SimpleNamespace(slack=SimpleNamespace(channel_updates_enabled=True,
            work_intake_beta_slack_user_ids=[user.slack_user_id], work_summary_channels={})),
        _user_session_lock=lambda key: asyncio.Lock())
    async def link(**kwargs): return {'permalink':'https://example.com/update'}
    event = {'type':'message','channel_type':'channel','channel':'C1','user':user.slack_user_id,
             'ts':str((now-timedelta(seconds=1)).timestamp()),'text':'Finished prototype testing; results uploaded'}
    asyncio.run(handle(runtime, SimpleNamespace(chat_getPermalink=link), event))
    session = clock.current_session(user, now)
    assert session.work_segments == before
    assert not session.metadata.get('slack_clock_inactivity_warning_at')
    assert session.latest_status == event['text']

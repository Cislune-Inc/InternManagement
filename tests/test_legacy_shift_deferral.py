import json
from datetime import date, datetime, timezone

import pytest

from agent.models import SessionState, UserProfile
from agent.state_store import StateStore
from agent.slack_timekeeping import SlackTimekeeping
from ops.defer_legacy_shifts import defer


def test_explicit_deferral_preserves_original_and_allows_new_clock(tmp_path):
    path = tmp_path / 'state.sqlite3'
    store = StateStore(path)
    old = SessionState(user_key='owner', session_date='2026-08-07', clocked_in_at='2026-08-07T09:00:00-07:00')
    store.save_session(old)
    store.save_session(SessionState(user_key='other', session_date='2026-08-07', clocked_in_at=old.clocked_in_at))
    before = path.read_bytes()
    assert defer(path, 'owner', date(2026, 9, 7), 'ERIK') == 1
    assert path.read_bytes() == before
    assert defer(path, 'owner', date(2026, 9, 7), 'ERIK', apply=True) == 1
    assert defer(path, 'owner', date(2026, 9, 7), 'ERIK', apply=True) == 0
    saved = store.get_session('owner', old.session_date)
    assert saved.clocked_out_at is None and saved.clocked_in_at == old.clocked_in_at
    assert not store.get_session('other', old.session_date).metadata
    with store._connect() as conn:
        original = json.loads(conn.execute('SELECT original_payload FROM legacy_shift_deferrals').fetchone()[0])
        assert original['metadata'] == {}
    clock = SlackTimekeeping(store)
    user = UserProfile(user_key='owner', display_name='Owner', worker_type='admin')
    now = datetime(2026, 9, 7, 16, tzinfo=timezone.utc)
    response, fresh = clock.handle(user, 'in', 'onsite', event_id='new', now=now)
    assert response.startswith('Clocked in') and fresh.session_date == '2026-09-07'
    assert 'today 0.00' in clock.handle(user, 'hours', '', event_id='status', now=now)[0]
    assert len(clock.reports()) == 1


def test_deferral_refuses_beta_records_atomically(tmp_path):
    path = tmp_path / 'state.sqlite3'
    store = StateStore(path)
    store.save_session(SessionState(user_key='w', session_date='2026-09-06',
                                   clocked_in_at='2026-09-06T09:00:00-07:00', metadata={'slack_clock_beta': True}))
    with pytest.raises(ValueError, match='pre-beta'):
        defer(path, 'w', date(2026, 9, 7), 'ERIK', apply=True)
    assert store.get_session('w', '2026-09-06').metadata == {'slack_clock_beta': True}

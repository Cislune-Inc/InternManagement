import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.dm_work_updates import DMWorkUpdates
from agent.slack_work_intake import SlackWorkIntake
from agent.state_store import StateStore


@pytest.fixture
def setup(tmp_path):
    store = StateStore(tmp_path/'state.db')
    intake = SlackWorkIntake(store)
    sent = []
    async def post(channel, text):
        sent.append((channel,text))
        return {'ts':'123.456'}
    async def audience(channel, actor):
        return True
    async def coach(actor, note, **kwargs):
        assert kwargs['channel_only'] and 'recent' not in kwargs['context']
        return {'shareable':True, 'project_suggestion':'bagworm','excerpts':[note]}
    runtime = SimpleNamespace(state_store=store,config=SimpleNamespace(slack=SimpleNamespace(
        dm_work_sharing_enabled=True,work_summary_channels={'bagworm':'C123'})),
        slack=SimpleNamespace(post_message=post,can_share_work=audience))
    return store,intake,sent,runtime,SimpleNamespace(coach=coach)


def run(setup, text='Bagworm specimen removed cleanly; ready for inspection', ts=None):
    store,intake,sent,runtime,ai = setup
    ts = ts or str(datetime.now(timezone.utc).timestamp())
    response = intake.handle(actor_id='WORKER',actor_name='Worker',is_manager=False,text='work '+text,event_id=ts)
    with store._connect() as conn:
        item = conn.execute('SELECT id FROM work_intake_items ORDER BY rowid DESC LIMIT 1').fetchone()[0]
    result = asyncio.run(DMWorkUpdates(store).process(runtime,'WORKER','work '+text,{'ts':ts},item,ai=ai))
    return result,ts


def test_current_note_gets_private_link_once_and_never_publishes(setup):
    result,ts = run(setup)
    assert '<#C123>' in result and 'post this update' in result
    assert run(setup,ts=ts)[0] == ''
    assert not setup[2]
    service=DMWorkUpdates(setup[0])
    assert service.published_records(channels=[]) == []
    assert service.published_records(channels=['OTHER']) == []
    assert service.published_records(channels=['C123']) == []
    with setup[0]._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0


@pytest.mark.parametrize('text', ['private Bagworm specimen needs inspection tomorrow',
    'Bagworm progress and my lunch was missed today', 'Bagworm test finished but payroll needs fixing'])
def test_private_or_time_notes_never_shared(setup,text):
    assert run(setup,text)[0] == ''
    assert not setup[2]


def test_historical_notes_and_unknown_destinations_not_shared(setup):
    run(setup,ts=str((datetime.now(timezone.utc)-timedelta(days=1)).timestamp()))
    run(setup,'CITA comparison is ready for technical review')
    assert not setup[2]


def test_unverified_audience_and_hallucinated_excerpt_fail_closed(setup):
    async def no(channel,actor): return False
    setup[3].slack.can_share_work=no
    run(setup)
    assert not setup[2]


def test_obsolete_excerpt_ai_is_never_used_to_publish(setup):
    async def invented(*args,**kwargs):
        return {'shareable':True,'project_suggestion':'bagworm','excerpts':['All tests passed perfectly']}
    setup[4].coach=invented
    run(setup)
    assert not setup[2]


def test_transport_timeout_claim_is_not_retried(setup):
    async def uncertain(channel,text):
        setup[2].append((channel,text))
        raise TimeoutError()
    setup[3].slack.post_message=uncertain
    _,ts=run(setup)
    run(setup,ts=ts)
    assert len(setup[2]) == 0  # No channel transport, even when configured.
    with setup[0]._connect() as conn:
        assert conn.execute('SELECT status FROM dm_work_updates').fetchone()[0] == 'reminded'


def test_channel_first_duplicate_suppresses_mirror(setup):
    from agent.channel_updates import ChannelUpdates
    note='Bagworm specimen removed cleanly; ready for inspection'
    ChannelUpdates(setup[0]).capture({'type':'message','channel_type':'channel','channel':'C123',
        'user':'WORKER','text':note,'ts':str((datetime.now(timezone.utc)-timedelta(minutes=1)).timestamp())},'bagworm')
    assert run(setup,note)[0] == ''
    assert not setup[2]


def test_fuller_human_post_suppresses_subset_dm_reminder(setup):
    from agent.channel_updates import ChannelUpdates
    note='Bagworm specimen removed cleanly ready for inspection'
    ChannelUpdates(setup[0]).capture({'type':'message','channel_type':'channel','channel':'C123',
        'user':'WORKER','text':note+' We also found a hydraulic constraint and need a peer review before the next test.',
        'ts':str((datetime.now(timezone.utc)-timedelta(minutes=1)).timestamp())},'bagworm')
    assert run(setup,note)[0] == ''


def test_reminder_is_bounded_and_ai_independent(setup):
    async def unavailable(*args,**kwargs): raise AssertionError('No AI required for a channel link')
    setup[4].coach=unavailable
    assert run(setup)[0]
    assert run(setup,'Bagworm testing changed the mold setup for tomorrow')[0] == ''
    assert not setup[2]


def test_historical_excerpt_prefers_fuller_human_same_audience(setup):
    import json
    from agent.channel_updates import ChannelUpdates
    service=DMWorkUpdates(setup[0]);now=datetime.now(timezone.utc)
    ts=str((now-timedelta(minutes=2)).timestamp())
    note='Bagworm specimen removed cleanly ready for inspection'
    with setup[0]._connect() as c:
        c.execute('INSERT INTO dm_work_updates VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            ('old','WORKER','item',1,'private-ts','C123','fp','sent',now.isoformat(),
             json.dumps({'text':'> '+note,'project_key':'bagworm'}),ts))
    # A different channel must not reveal a matching source or suppress this one.
    e={'type':'message','channel_type':'channel','channel':'OTHER','user':'WORKER',
       'text':note+' Next test requires a new mold and peer inspection.',
       'ts':str((now-timedelta(minutes=1)).timestamp())}
    ChannelUpdates(setup[0]).capture(e,'bagworm')
    assert 'preferred_source' not in service.published_records(channels=['C123'])[0]
    e['channel']='C123';ChannelUpdates(setup[0]).capture(e,'bagworm')
    record=service.published_records(channels=['C123'])[0]
    assert record['preferred_source']=={'channel':'C123','message_ts':e['ts']}
    assert record['count_as_separate_progress'] is False
    assert 'private-ts' not in str(record)
    newer=str(float(e['ts'])+.5)
    ChannelUpdates(setup[0]).capture({**e,'subtype':'message_changed','event_ts':newer,
        'message':{**e,'edited':{'ts':newer},'text':e['text'].replace('removed','not removed')}},'bagworm')
    assert 'preferred_source' not in service.published_records(channels=['C123'])[0]
    ChannelUpdates(setup[0]).capture({**e,'subtype':'message_deleted',
        'deleted_ts':e['ts'],'event_ts':str(float(e['ts'])+1)},'bagworm')
    assert 'preferred_source' not in service.published_records(channels=['C123'])[0]

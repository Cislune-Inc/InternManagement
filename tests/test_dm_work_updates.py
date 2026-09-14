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


def test_literal_current_note_shared_once_with_author_not_hours(setup):
    result,ts = run(setup)
    assert 'Shared' in result
    assert setup[2][0][0] == 'C123' and '<@WORKER>' in setup[2][0][1]
    run(setup,ts=ts)
    assert len(setup[2]) == 1
    service=DMWorkUpdates(setup[0])
    assert service.published_records(channels=[]) == []
    assert service.published_records(channels=['OTHER']) == []
    published=service.published_records(channels=['C123'])[0]
    assert published['actor']=='WORKER' and 'source_ts' not in published
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


def test_nonliteral_excerpt_never_shared(setup):
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
    assert len(setup[2]) == 1
    with setup[0]._connect() as conn:
        assert conn.execute('SELECT status FROM dm_work_updates').fetchone()[0] == 'attempting'


def test_channel_first_duplicate_suppresses_mirror(setup):
    from agent.channel_updates import ChannelUpdates
    note='Bagworm specimen removed cleanly; ready for inspection'
    ChannelUpdates(setup[0]).capture({'type':'message','channel_type':'channel','channel':'C123',
        'user':'WORKER','text':note,'ts':str((datetime.now(timezone.utc)-timedelta(minutes=1)).timestamp())},'bagworm')
    run(setup,note)
    assert not setup[2]

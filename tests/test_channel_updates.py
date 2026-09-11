import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agent.channel_updates import ChannelUpdates, handle
from agent.models import UserProfile
from agent.progress_checkins import ProgressCheckins
from agent.slack_timekeeping import SlackTimekeeping
from agent.state_store import StateStore


def event(text='The comparison plot is saved and ready for review', **kwargs):
    return dict(type='app_mention', user='WORKER', channel='C123',
                ts=str((datetime.now(timezone.utc)-timedelta(seconds=5)).timestamp()),
                text=text, **kwargs)


def test_capture_is_explicit_idempotent_private_to_actor_and_never_punches(tmp_path):
    store = StateStore(tmp_path/'state.db')
    service = ChannelUpdates(store)
    e = event('<@BOT> Saved plot <!channel> with three labeled runs', files=[{'id':'F123','name':'plot.png','mimetype':'image/png','url_private':'secret'}])
    assert service.capture(e, 'grasp')
    assert not service.capture(e, 'grasp')
    assert not service.capture({**e,'type':'message'}, 'grasp')
    assert not service.capture({**e,'subtype':'message_changed'}, 'grasp')
    assert not service.capture({**e,'bot_id':'BOT'}, 'grasp')
    assert service.latest('WORKER')
    assert service.latest('OTHER') is None
    assert 'No channel updates' in service.recent('OTHER')
    assert '&lt;!channel&gt;' in service.recent('WORKER')
    with store._connect() as c:
        assert c.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
        assert 'secret' not in c.execute('SELECT files_json FROM channel_work_updates').fetchone()[0]


def test_channel_update_suppresses_progress_prompt_not_clock(tmp_path):
    store = StateStore(tmp_path/'state.db')
    now = datetime.now(timezone.utc)
    _, session = SlackTimekeeping(store).handle(UserProfile(user_key='worker',display_name='Worker'), 'in','onsite',event_id='start',now=now-timedelta(hours=3))
    original = repr(session)
    service = ChannelUpdates(store)
    assert service.capture(event(), 'grasp')
    assert ProgressCheckins(store).claim('WORKER',session,now) is None
    assert repr(session) == original


def test_short_message_does_not_suppress_checkin(tmp_path):
    service = ChannelUpdates(StateStore(tmp_path/'state.db'))
    assert service.capture(event('hello'), 'grasp')
    assert service.latest('WORKER') is None


def test_only_enrolled_active_workers_in_unique_routes_get_thread_ack(tmp_path):
    store = StateStore(tmp_path/'state.db')
    sent=[]
    async def post(channel,text,**kwargs): sent.append((channel,text,kwargs))
    async def link(**kwargs): return {'permalink':'https://example.com/source'}
    user=UserProfile(user_key='worker',display_name='Worker',slack_user_id='WORKER')
    runtime=SimpleNamespace(state_store=store,config=SimpleNamespace(slack=SimpleNamespace(
        work_summary_channels={'grasp':'C123'},work_intake_beta_slack_user_ids=['WORKER'],channel_updates_enabled=False)),
        roster_by_slack_id={'WORKER':user},admin_profile_by_slack_user_id=lambda x:None,
        slack=SimpleNamespace(post_message=post))
    web=SimpleNamespace(chat_getPermalink=link)
    e=event(thread_ts='123.456')
    asyncio.run(handle(runtime,web,e))
    assert not sent
    runtime.config.slack.channel_updates_enabled=True
    asyncio.run(handle(runtime,web,{**e,'channel':'OTHER'}))
    asyncio.run(handle(runtime,web,{**e,'user':'OTHER'}))
    assert not sent
    asyncio.run(handle(runtime,web,e));asyncio.run(handle(runtime,web,e))
    assert len(sent)==1 and sent[0][2]['thread_ts']=='123.456'
    assert 'https://example.com/source' in ChannelUpdates(store).recent('WORKER')
    with store._connect() as c:
        assert c.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]==0

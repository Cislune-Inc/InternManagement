import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.channel_updates import ChannelUpdates
from agent.channel_followups import ChannelFollowups
from agent.state_store import StateStore


def setup(tmp_path, text='Bagworm prototype is blocked'):
    store=StateStore(tmp_path/'state.db')
    event=dict(type='message',channel_type='channel',channel='C1',user='U1',
               ts=str((datetime.now(timezone.utc)-timedelta(seconds=5)).timestamp()),text=text)
    ChannelUpdates(store).capture(event,'bagworm')
    sent=[]
    async def audience(*args): return True
    async def post(channel,text,**kwargs):
        sent.append((channel,text,kwargs)); return {'ts':'123.45'}
    async def replies(**kwargs): return {'messages':[event]}
    async def coach(*args,**kwargs): return {'follow_up_question':'What is blocking the prototype, and what help would unblock it?'}
    runtime=SimpleNamespace(state_store=store,slack=SimpleNamespace(can_share_work=audience,post_message=post),
        config=SimpleNamespace(slack=SimpleNamespace(dm_work_sharing_enabled=True,work_summary_channels={'bagworm':'C1'})))
    return store,event,sent,runtime,SimpleNamespace(conversations_replies=replies),SimpleNamespace(coach=coach)


def run(s):
    store,event,sent,runtime,web,ai=s
    asyncio.run(ChannelFollowups(store).process(runtime,web,event,ai=ai))


def test_question_is_source_thread_only_once_no_clock(tmp_path):
    s=setup(tmp_path); run(s);run(s)
    assert s[2] == [('C1','What is blocking this work, and what help or decision would unblock it?',{'thread_ts':s[1]['ts']})]
    with s[0]._connect() as c:
        assert c.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]==0


@pytest.mark.parametrize('text',['Prototype blocked because the vendor has no stock',
    'Prototype blocked, can George help?', 'My lunch is blocked', 'Prototype tested and ready',
    'Prototype blocked and I need a replacement sensor'])
def test_useful_or_private_updates_stay_quiet(tmp_path,text):
    s=setup(tmp_path,text);run(s); assert not s[2]


@pytest.mark.parametrize('mode',['reply','truncated','denied','failed','edit','delete','hours','ai_error','stale','thread'])
def test_context_and_audience_fail_closed(tmp_path,mode):
    s=setup(tmp_path);store,event,sent,runtime,web,ai=s
    async def page(**kwargs):
        if mode=='failed': raise TimeoutError()
        return {'messages':[event,{'text':'Already fixed','user':'U2'}] if mode=='reply' else [event],
                'has_more':mode=='truncated'}
    web.conversations_replies=page
    async def audience(*args): return mode!='denied'
    runtime.slack.can_share_work=audience
    async def coach(*args,**kwargs):
        if mode=='ai_error': raise TimeoutError()
        if mode in {'edit','delete'}:
            with store._connect() as c:
                c.execute('UPDATE channel_work_updates SET text=?,deleted=?',('Already solved',int(mode=='delete')))
        return {'follow_up_question':'How many hours did you work?' if mode=='hours' else 'What help would unblock it?'}
    ai.coach=coach
    if mode=='stale':event['ts']=str((datetime.now(timezone.utc)-timedelta(days=1)).timestamp())
    if mode=='thread':event['thread_ts']='123.45'
    run(s);assert not sent


def test_reply_arriving_during_ai_suppresses_question_and_is_captured(tmp_path):
    s=setup(tmp_path);store,event,sent,runtime,web,ai=s
    async def coach(*args,**kwargs):
        reply={**event,'ts':str(float(event['ts'])+1),'thread_ts':event['ts'],
               'user':'U2','text':'Fixed with the new sensor; test result saved'}
        ChannelUpdates(store).capture(reply,'bagworm')
        return {'follow_up_question':'What help would unblock it?'}
    ai.coach=coach
    run(s);assert not sent
    records=ChannelUpdates(store).records(channels=['C1'])
    assert len(records)==2 and records[1]['actor']=='U2' and records[1]['thread_ts']==event['ts']


def test_uncertain_send_not_retried(tmp_path):
    s=setup(tmp_path)
    async def post(*args,**kwargs): s[2].append(args);raise TimeoutError()
    s[3].slack.post_message=post
    run(s);run(s);assert len(s[2])==1

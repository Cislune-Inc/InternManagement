"""Trusted owner-machine pilot over existing SSH; never run on a shared kiosk."""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
from contextvars import ContextVar
from aiohttp import web
from .planning_store import PlanningStore, Principal
from .planning_web import create_planning_app


def remote_program(options):
    # Ship code through stdin for read-only evaluation, not a production checkout edit.
    names=('planning_store','planning_adapter','planning_time')
    modules={name:Path(__file__).with_name(name+'.py').read_text() for name in names}
    return '''import sys,types,json,asyncio,os
from agent.config import load_bootstrap,parse_agent_config,parse_roster_bytes
from agent.slack_client import SlackClient
modules=MODULES
for name,source in modules.items():
 m=types.ModuleType('agent.'+name);m.__package__='agent';sys.modules[m.__name__]=m;exec(source,m.__dict__)
from agent.planning_adapter import read_channel_batch
from agent.planning_time import read_person_time
options=OPTIONS
b=load_bootstrap();c=parse_agent_config(json.loads(b.agent_config_path.read_text()),b.default_timezone)
owner=next((a for a in c.admins if a.discord_user_id==c.admin_discord_user_id),None)
if owner is None or owner.slack_user_id!=options['owner']:raise ValueError('Configured owner mismatch')
slack=SlackClient(os.environ['SLACK_BOT_TOKEN'])
async def collect():
 auth=await asyncio.to_thread(slack._request,'GET','/auth.test')
 if auth.get('team_id')!=options['workspace']:raise ValueError('Workspace mismatch')
 allowed=await slack.can_share_work(options['channel'],options['owner'])
 scopes=[f"slack:{options['workspace']}:{options['channel']}"] if allowed else []
 batches=[];cursor=None
 if allowed:
  for _ in range(10):
   batch=read_channel_batch(b.state_db_path,workspace=options['workspace'],channels=[options['channel']],cursor=cursor,limit=200)
   batches.extend(batch['events']);cursor=batch['next_cursor']
   if not batch['has_more']:break
  if batch['has_more']:raise ValueError('Captured source exceeds pilot bound')
 if allowed:
  history=await asyncio.to_thread(slack._request,'GET','/conversations.history',params={'channel':options['channel'],'limit':20})
  from datetime import datetime,timezone
  for message in history.get('messages',[]):
   if message.get('bot_id') or not message.get('user') or not message.get('ts'):continue
   ts=message['ts'];channel=options['channel'];workspace=options['workspace']
   batches.append({'source_ref':f'slack:{workspace}:{channel}:{ts}','version':message.get('edited',{}).get('ts',ts),
    'scope':f'slack:{workspace}:{channel}','project':'bagworm','person_ref':f"slack:{workspace}:{message['user']}",
    'text':message.get('text','')[:6000],'files':[{'id':f.get('id',''),'name':f.get('name','')} for f in message.get('files',[])[:5]],
    'posted_at':datetime.fromtimestamp(float(ts),timezone.utc).isoformat(),'deleted':False,
    'permalink':f'https://app.slack.com/archives/{channel}/p'+ts.replace('.',''),
    'source_kind':'slack_history_snapshot','verification':'Recent 20-message snapshot; older history, replies and deletions may be missing'})
 p=b.agent_config_path.parent/c.roster_file_name
 users=parse_roster_bytes(p.name,p.read_bytes())
 user=next((u for u in users if u.active and u.slack_user_id==options['owner']),None)
 summary=read_person_time(b.state_db_path,user_key=user.user_key,timezone_name=c.timezone) if user else None
 print(json.dumps({'owner_ref':f"slack:{options['workspace']}:{options['owner']}",'scopes':scopes,'sources':batches,'time':summary}))
asyncio.run(collect())
'''.replace('MODULES',repr(modules)).replace('OPTIONS',repr(options))


class OwnerBridge:
    def __init__(self,host,options):
        self.command=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=6',
                      '-o','HostName='+host,'-o','HostKeyAlias=cmacmini.local','don-pollo-mini',
                      'cd /Users/pm/InternManagement && .venv/bin/python -B -']
        self.program=remote_program(options)
    def read(self):
        result=subprocess.run(self.command,input=self.program,text=True,capture_output=True,timeout=45)
        if result.returncode:
            raise ValueError('Live owner connection unavailable. Check shop/VPN access; no cached permissions used.')
        return json.loads(result.stdout)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('host','owner','workspace','channel'):p.add_argument('--'+name,required=True)
    p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--database',type=Path,required=True)
    p.add_argument('--port',type=int,default=8879)
    args=p.parse_args()
    bridge=OwnerBridge(args.host,{k:getattr(args,k) for k in ('owner','workspace','channel')})
    first=bridge.read()
    store=PlanningStore(args.database,owner_ref=first['owner_ref'])
    plan=json.loads(args.snapshot.read_text())
    if any(p['id']!='bagworm' for p in plan['projects']):raise ValueError('This pilot is limited to Bagworm')
    store.initialize(plan)
    current_time=ContextVar('owner_time',default=None)
    async def authenticate(request):
        if request.path.startswith('/planning-assets/') or request.path in ('/','/planning'):
            return Principal(first['owner_ref'],frozenset({'bagworm'}))
        current=await asyncio.to_thread(bridge.read)
        if current['owner_ref']!=store.owner_ref:raise ValueError('Owner identity changed')
        for source in current['sources']:
            if source['project']=='bagworm':store.ingest_source(source)
        current_time.set(current['time'])
        return Principal(current['owner_ref'],frozenset({'bagworm'}),frozenset(current['scopes']+['person:'+store.owner_ref]))
    # A per-request time callback avoids one request receiving another's snapshot.
    # This listener has exactly one explicitly verified owner identity.
    app=create_planning_app(store,authenticate=authenticate,allowed_origin=f'http://127.0.0.1:{args.port}',
                            time_reader=lambda actor:current_time.get(),
                            mode_label='OWNER PILOT · Live Don Pollo reads over SSH. Private to this Mac; worker access is not enabled. Proposals still require your acceptance.')
    web.run_app(app,host='127.0.0.1',port=args.port,access_log=None)


if __name__=='__main__':main()

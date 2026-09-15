"""Verify installed Slack access before activating owner-approved work sharing.

Run from the production repository. Defaults to read-only preflight. Never joins
channels or creates credentials. Explicit --apply preserves a private config copy.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from agent.slack_client import SlackClient
from agent.slack_work_intake import PROJECTS


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--dm-sharing',action='store_true')
    parser.add_argument('--route',action='append',default=[],help='project_key=channel_id')
    args=parser.parse_args()
    load_dotenv('.env')
    client=SlackClient(os.environ['SLACK_BOT_TOKEN'])
    identity=client._request('GET','/auth.test')
    if identity.get('team_id') != 'T01T2P57XB6' or identity.get('user_id') != 'U0BLA55B1SA':
        raise SystemExit('Unexpected DP Slack installation; configuration unchanged.')
    # Exercise installed read scopes without logging channel contents.
    channels=[]; cursor=''
    for _ in range(10):
        page=client._request('GET','/users.conversations',params={'types':'public_channel,private_channel','exclude_archived':'true','limit':200,'cursor':cursor})
        channels.extend(page.get('channels',[]))
        cursor=page.get('response_metadata',{}).get('next_cursor','')
        if not cursor: break
    if cursor: raise SystemExit('Channel inventory incomplete; configuration unchanged.')
    path=Path('config/agent.config.json')
    config=json.loads(path.read_text())
    routes=dict(config.get('slack',{}).get('work_summary_channels',{}))
    for specification in args.route:
        key,sep,destination=specification.partition('=')
        if not sep or key not in PROJECTS: raise SystemExit('Invalid project route.')
        routes[key]=destination
    available={c['id']:c for c in channels}
    for destination in set(routes.values()):
        if destination not in available: raise SystemExit('A routed channel is unavailable; configuration unchanged.')
        info=client._request('GET','/conversations.info',params={'channel':destination})['channel']
        if info.get('is_archived') or info.get('is_shared') or not info.get('is_member'):
            raise SystemExit('A routed channel is not an internal bot-member destination.')
    if not routes: raise SystemExit('No verified routes.')
    print(json.dumps({'workspace':identity['team_id'],'bot_channels':len(channels),'routes':routes,'apply':args.apply,'dm_sharing':args.dm_sharing}))
    if not args.apply: return
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=path.with_name('agent.config.before-channel-work.'+stamp+'.json')
    shutil.copy2(path,backup); backup.chmod(0o600)
    config.setdefault('slack',{}).update(channel_updates_enabled=True,dm_work_sharing_enabled=args.dm_sharing,work_summary_channels=routes)
    temporary=path.with_suffix('.channel-work.tmp')
    with temporary.open('x') as output:
        os.chmod(temporary,0o600)
        output.write(json.dumps(config,indent=2)+'\n')
    temporary.replace(path)
    print('Approved channel configuration saved. No credentials, roster or time records changed.')


if __name__ == '__main__':
    main()

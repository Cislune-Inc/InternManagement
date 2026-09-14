"""Read-only boundary to Don Pollo capture. No capture, enrollment or clock writes."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
from datetime import datetime,timezone

from .planning_store import PlanningError, identifier


def person_ref(workspace: str, slack_user_id: str) -> str:
    return f'slack:{identifier(workspace)}:{identifier(slack_user_id)}'


def read_channel_batch(path: Path, *, workspace: str, channels=None, cursor=None, limit=100, project_map=None):
    """Keyset delta over captured_at/channel/ts; edits and tombstones are included.

    Caller retains cursor only AFTER successfully ingesting the entire batch.
    Missing capture tables are a coverage gap, never proof of no Slack activity.
    Use a supplied operator snapshot when available; mode=ro also supports WAL.
    """
    identifier(workspace)
    if not channels:
        return dict(events=[],next_cursor=cursor or ['','',''],has_more=False,coverage='source_grants_required')
    channels=list(dict.fromkeys(identifier(c) for c in channels))
    if len(channels)>100:
        raise PlanningError('Use at most 100 explicitly granted channels.')
    if type(limit) is not int or not 1 <= limit <= 200:
        raise PlanningError('Read at most 200 source records per batch.')
    cursor = cursor or ['', '', '']
    if not isinstance(cursor, list) or len(cursor) != 3 or any(not isinstance(v,str) for v in cursor):
        raise PlanningError('Invalid source checkpoint.')
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_work_updates'").fetchone():
            return dict(events=[],next_cursor=cursor,has_more=False,coverage='capture_table_unavailable')
        columns={r[1] for r in db.execute('PRAGMA table_info(channel_work_updates)')}
        extra=','.join(k if k in columns else "'' AS "+k for k in ('workspace_id','user_key','thread_ts'))
        rows = db.execute('''SELECT channel,message_ts,actor,project_key,text,files_json,
            posted_at,captured_at,meaningful,permalink,source_version,deleted,'''+extra+'''
            FROM channel_work_updates WHERE (captured_at,channel,message_ts) > (?,?,?) AND channel IN ('''
            +','.join('?' for _ in channels)+''') ORDER BY captured_at,channel,message_ts LIMIT ?''', (*cursor,*channels,limit+1)).fetchall()
    events=[]
    for r in rows[:limit]:
        if r['workspace_id'] and r['workspace_id']!=workspace:
            raise PlanningError('The source workspace differs from the configured identity namespace.',409)
        channel=identifier(r['channel'])
        events.append(dict(source_ref=f"slack:{workspace}:{channel}:{r['message_ts']}",
            version=r['source_version'],scope=f'slack:{workspace}:{channel}',
            project=(project_map or {}).get(r['project_key'],r['project_key']) or 'unmapped',
            workstream_key=r['project_key'],person_ref=person_ref(workspace,r['actor']),
            roster_resolved=bool(r['user_key']),thread_ts=r['thread_ts'],
            text=r['text'],files=[{k:f[k] for k in ('id','name','mimetype') if k in f}
                                for f in json.loads(r['files_json'])[:5] if isinstance(f,dict)],
            posted_at=r['posted_at'],captured_at=r['captured_at'],meaningful=bool(r['meaningful']),
            permalink=r['permalink'],deleted=bool(r['deleted']),source_kind='slack_channel'))
    last=rows[min(limit,len(rows))-1] if rows else None
    return dict(events=events,next_cursor=[last[k] for k in ('captured_at','channel','message_ts')] if last else cursor,
                has_more=len(rows)>limit,coverage='captured_records_only')


def ingest_channel_batch(store, path: Path, *, workspace: str, channels=None, cursor=None, limit=100, project_map=None):
    batch=read_channel_batch(path,workspace=workspace,channels=channels,cursor=cursor,limit=limit,project_map=project_map)
    changed=sum(store.ingest_source(event) for event in batch['events'])
    return {k:v for k,v in batch.items() if k!='events'} | {'read':len(batch['events']),'changed':changed}


def read_published_excerpts(path: Path, *, workspace: str, channels, cursor=None, limit=100,project_map=None):
    """Same sent-only projection as DP DMWorkUpdates.published_records().

    Never export the audit row, original DM text/timestamp, held inputs, or AI
    drafts. This is a publication receipt, not a live channel reconciliation.
    """
    identifier(workspace)
    cursor=cursor or ['','']
    if type(limit) is not int or not 1<=limit<=200 or not isinstance(cursor,list) or len(cursor)!=2 or any(not isinstance(c,str) for c in cursor):
        raise PlanningError('Invalid publication checkpoint.')
    channels=list(dict.fromkeys(identifier(c) for c in channels))
    if not channels:return dict(events=[],next_cursor=cursor,has_more=False,coverage='source_grants_required')
    if len(channels)>100:raise PlanningError('Use at most 100 explicitly granted channels.')
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='dm_work_updates' AND type='table'").fetchone():
            return dict(events=[],next_cursor=cursor,has_more=False,coverage='publication_table_unavailable')
        rows=db.execute("SELECT actor,channel,message_ts,payload FROM dm_work_updates WHERE status='sent' AND (message_ts,channel)>(?,?) AND channel IN ("
            +','.join('?' for _ in channels)+') ORDER BY message_ts,channel LIMIT ?',(*cursor,*channels,limit+1)).fetchall()
    events=[]
    for row in rows[:limit]:
        payload=json.loads(row['payload']);ts=identifier(row['message_ts']);channel=identifier(row['channel'])
        events.append(dict(source_ref=f'published:{workspace}:{channel}:{ts}',version=ts,scope=f'slack:{workspace}:{channel}',
            project=(project_map or {}).get(payload.get('project_key'),payload.get('project_key')) or 'unmapped',
            person_ref=person_ref(workspace,row['actor']),text=payload.get('text','')[:6000],files=[],deleted=False,
            posted_at=datetime.fromtimestamp(float(ts),timezone.utc).isoformat(),
            permalink=f'https://app.slack.com/archives/{channel}/p'+ts.replace('.',''),
            source_kind='dp_published_work_excerpt',verification='publication_receipt_only'))
    last=rows[min(limit,len(rows))-1] if rows else None
    return dict(events=events,next_cursor=[last['message_ts'],last['channel']] if last else cursor,
                has_more=len(rows)>limit,coverage='published_excerpts_only_not_live_reconciliation')


def read_work_events(path: Path, *, workspace: str, after_id=0, limit=100, item_projects=None):
    """Immutable DP event IDs stay external references, never planner packet IDs.

    Explicit operator item->project mappings avoid relabeling historical work from
    today's item project_key. Unmapped items remain unresolved. Raw work notes keep
    the worker-specific audience; plan approval is never inferred from DP status.
    """
    identifier(workspace)
    if type(after_id) is not int or after_id<0 or type(limit) is not int or not 1<=limit<=200:
        raise PlanningError('Invalid work-event checkpoint or limit.')
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'work_intake_events','work_intake_items'}<=tables:
            return dict(events=[],next_id=after_id,has_more=False,coverage='work_tables_unavailable')
        rows=db.execute('''SELECT e.id,e.item_id,e.actor_id,e.kind,e.text,e.created_at,i.owner_id
            FROM work_intake_events e JOIN work_intake_items i ON e.item_id=i.id
            WHERE e.id>? ORDER BY e.id LIMIT ?''',(after_id,limit+1)).fetchall()
    events=[]
    for r in rows[:limit]:
        owner=person_ref(workspace,r['owner_id'])
        events.append(dict(source_ref=f"dp-work:{workspace}:{identifier(r['item_id'])}:{r['id']}",
            version=str(r['id']),scope='person:'+owner,project=(item_projects or {}).get(r['item_id'],'unmapped'),
            person_ref=person_ref(workspace,r['actor_id']),text=r['text'][:6000],files=[],
            posted_at=r['created_at'],source_kind='dp_work_event',external_item_ref=r['item_id'],
            event_kind=r['kind'],deleted=False,permalink=''))
    return dict(events=events,next_id=rows[min(limit,len(rows))-1]['id'] if rows else after_id,
                has_more=len(rows)>limit,coverage='captured_work_events_only')


def roster_bridge(workspace, users):
    """Explicit Slack ID -> existing ledger key only. Never resolve by names."""
    result={}
    for user in users:
        sid=getattr(user,'slack_user_id','')
        key=getattr(user,'user_key','')
        if sid and key:
            ref=person_ref(workspace,sid)
            if ref in result and result[ref]!=key:
                raise PlanningError('Ambiguous roster identity; reconcile before integration.',409)
            result[ref]=key
    return result

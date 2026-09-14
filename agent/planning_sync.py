"""Bounded operator-driven source refresh; no scheduler, grants or clock writes."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from .planning_adapter import read_channel_batch, read_published_excerpts
from .planning_store import PlanningError, encode, identifier, now


def sync_sources(store, source_path, checkpoint_path, *, stream, workspace,
                 channels, project_map=None, kind='channels', limit=100,
                 max_batches=1, reconcile=False):
    """Commit checkpoint only after ingestion; crash retries replay idempotently.

    Host supplies current explicit channel grants for EACH invocation. A stream
    pins its routing, source and destination, so a changed configuration cannot
    silently reuse an incompatible cursor. Use a new stream after reviewing a
    config change. reconcile=True starts a bounded full sweep; subsequent calls
    resume it normally. No raw DM/work-event ingestion is exposed here.
    """
    identifier(stream)
    identifier(workspace)
    if kind not in ('channels', 'published'):
        raise PlanningError('Choose channel capture or published excerpts.')
    if type(max_batches) is not int or not 1 <= max_batches <= 10:
        raise PlanningError('Refresh at most ten batches per invocation.')
    if type(limit) is not int or not 1 <= limit <= 200:
        raise PlanningError('Read at most 200 records per batch.')
    if type(reconcile) is not bool:
        raise PlanningError('Invalid reconciliation mode.')
    channels = sorted(set(identifier(c) for c in channels))
    if not channels or len(channels) > 100:
        raise PlanningError('Supply one to 100 currently granted channels.')
    project_map = dict(project_map or {})
    for key, value in project_map.items():
        identifier(key)
        identifier(value)
    source_path = Path(source_path).resolve()
    checkpoint_path = Path(checkpoint_path).resolve()
    destination = Path(store.path).resolve()
    if len({source_path, checkpoint_path, destination}) != 3:
        raise PlanningError('Source, planner and checkpoint databases must be separate.')
    paths = [source_path, checkpoint_path, destination]
    if any(a.exists() and b.exists() and a.samefile(b)
           for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise PlanningError('Source, planner and checkpoint databases must be separate.')
    if not source_path.is_file():
        raise PlanningError('Source database is unavailable.', 503)
    config = dict(source=str(source_path), destination=str(destination),
                  workspace=workspace, channels=channels, project_map=project_map, kind=kind)
    fingerprint = hashlib.sha256(encode(config).encode()).hexdigest()
    initial = ['', '', ''] if kind == 'channels' else ['', '']
    reader = read_channel_batch if kind == 'channels' else read_published_excerpts
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize refreshers without locking the live source or the planning store.
    with closing(sqlite3.connect(checkpoint_path, timeout=5)) as db:
        checkpoint_path.chmod(0o600)
        db.execute('CREATE TABLE IF NOT EXISTS source_checkpoints '
                   '(stream TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, cursor TEXT NOT NULL, '
                   'summary TEXT NOT NULL)')
        db.commit()
        with db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT fingerprint,cursor FROM source_checkpoints WHERE stream=?',
                                  (stream,)).fetchone()
            if previous and previous[0] != fingerprint:
                raise PlanningError('Source configuration changed; review and use a new stream.', 409)
            cursor = initial if reconcile or not previous else json.loads(previous[1])
            read = changed = batches = 0
            for _ in range(max_batches):
                batch = reader(source_path, workspace=workspace, channels=channels,
                               cursor=cursor, limit=limit, project_map=project_map)
                for event in batch['events']:
                    changed += bool(store.ingest_source(event))
                read += len(batch['events'])
                batches += 1
                cursor = batch['next_cursor']
                if not batch['has_more']:
                    break
            summary = dict(stream=stream, kind=kind, read=read, changed=changed,
                           batches=batches, next_cursor=cursor, has_more=batch['has_more'],
                           coverage=batch['coverage'], checked_at=now())
            db.execute('INSERT INTO source_checkpoints VALUES (?,?,?,?) '
                       'ON CONFLICT(stream) DO UPDATE SET cursor=excluded.cursor, summary=excluded.summary',
                       (stream, fingerprint, encode(cursor), encode(summary)))
    return summary

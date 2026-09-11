"""Quiet channel-message capture; source-linked work, never attendance."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .slack_work_intake import PROJECTS, _safe

logger = logging.getLogger(__name__)


class ChannelUpdates:
    def __init__(self, store: Any) -> None:
        self.store = store
        with store._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS channel_work_updates (
                channel TEXT NOT NULL, message_ts TEXT NOT NULL, actor TEXT NOT NULL,
                project_key TEXT NOT NULL, text TEXT NOT NULL, files_json TEXT NOT NULL,
                posted_at TEXT NOT NULL, captured_at TEXT NOT NULL,
                meaningful INTEGER NOT NULL, permalink TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(channel,message_ts))""")
            columns = {r[1] for r in conn.execute('PRAGMA table_info(channel_work_updates)')}
            for name, definition in [('source_version', "TEXT NOT NULL DEFAULT '0'"), ('deleted', 'INTEGER NOT NULL DEFAULT 0')]:
                if name not in columns:
                    conn.execute(f'ALTER TABLE channel_work_updates ADD COLUMN {name} {definition}')
            conn.execute('''CREATE TABLE IF NOT EXISTS channel_work_revisions (
                channel TEXT NOT NULL, message_ts TEXT NOT NULL, source_version TEXT NOT NULL,
                payload TEXT NOT NULL, captured_at TEXT NOT NULL,
                PRIMARY KEY(channel,message_ts,source_version))''')

    def capture(self, event: dict[str, Any], project: str) -> bool:
        # Channels, not personal/group DMs. Capture all human posts quietly;
        # mapped project channels help interpretation but do not gate capture.
        if event.get('type') not in {'message', 'app_mention'} or event.get('channel_type') not in {'channel', 'group'}:
            return False
        subtype = event.get('subtype')
        if subtype not in {None, '', 'file_share', 'thread_broadcast', 'message_changed', 'message_deleted'}:
            return False
        source = event.get('message', {}) if subtype == 'message_changed' else event
        if subtype == 'message_deleted':
            source = event.get('previous_message', {})
        if source.get('bot_id') or source.get('subtype') == 'bot_message' or event.get('bot_id'):
            return False
        actor = str(source.get('user') or '')
        channel = str(event.get('channel') or '')
        ts = str(event.get('deleted_ts') if subtype == 'message_deleted' else source.get('ts') or '')
        version = str((source.get('edited') or {}).get('ts') or event.get('event_ts') or event.get('ts') or ts)
        if not re.fullmatch(r'\d+\.\d+', version):
            return False
        deleted = subtype == 'message_deleted'
        # Deletions can omit author/text; retain a tombstone to defeat late retries.
        if deleted and not actor:
            actor = 'unknown'
        if not actor or not channel or not re.fullmatch(r'\d+\.\d+', ts):
            return False
        now = datetime.now(timezone.utc)
        try:
            posted = datetime.fromtimestamp(float(ts), timezone.utc)
        except (ValueError, OverflowError, OSError):
            return False
        if posted > now:
            return False
        text = str(source.get('text') or '').strip()[:6000] if not deleted else ''
        files = [{k: f[k] for k in ('id', 'name', 'mimetype') if k in f}
                 for f in source.get('files', [])[:5] if isinstance(f, dict)] if not deleted else []
        # A conservative hint for prompt suppression, not a worker formatting rule.
        # Preserve short/ambiguous posts too, without treating every chat as work.
        work_signal = re.search(r'\b(?:finished|completed|fixed|tested|testing|built|building|saved|uploaded|updated|working|designed|printed|blocked|waiting|ready|result|prototype|PCB|CAD|run|test|mold|specimen|proposal|review|design|experiment)\b', text, re.I)
        meaningful = not deleted and bool(work_signal or (files and text))
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM channel_work_updates WHERE channel=? AND message_ts=?', (channel,ts)).fetchone()
            if old and (old['deleted'] or float(old['source_version']) >= float(version)):
                return False
            if old:
                conn.execute('INSERT OR IGNORE INTO channel_work_revisions VALUES (?,?,?,?,?)',
                             (channel,ts,old['source_version'],json.dumps(dict(old)),now.isoformat()))
            conn.execute("""INSERT INTO channel_work_updates
                (channel,message_ts,actor,project_key,text,files_json,posted_at,captured_at,meaningful,source_version,deleted)
                VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(channel,message_ts) DO UPDATE SET
                actor=excluded.actor,project_key=excluded.project_key,text=excluded.text,
                files_json=excluded.files_json,captured_at=excluded.captured_at,
                meaningful=excluded.meaningful,source_version=excluded.source_version,deleted=excluded.deleted""",
                (channel, ts, actor, project, text, json.dumps(files), posted.isoformat(),
                 now.isoformat(), int(meaningful), version, int(deleted)))
            return True

    def latest(self, actor: str) -> str | None:
        with self.store._connect() as conn:
            return conn.execute('SELECT MAX(posted_at) FROM channel_work_updates WHERE actor=? AND meaningful=1 AND deleted=0', (actor,)).fetchone()[0]

    def recent(self, actor: str | None = None) -> str:
        with self.store._connect() as conn:
            rows = conn.execute('SELECT * FROM channel_work_updates WHERE deleted=0 AND (? IS NULL OR actor=?) ORDER BY posted_at DESC LIMIT 10', (actor, actor)).fetchall()
        return ('Recent channel updates (worker reports, not work/charging approvals):\n' + '\n\n'.join(
            f"{_safe(PROJECTS.get(r['project_key'], 'Project not mapped'))} · {_safe(r['actor'])}\n"
            f"{_safe(r['text'])}\n" + (r['permalink'] or f"Channel <#{r['channel']}> · {r['posted_at']}")
            for r in rows)) if rows else 'No channel updates captured yet. Post normally in a channel Don Pollo has access to; no special format is required.'


async def handle(runtime: Any, web_client: Any, event: dict[str, Any]) -> None:
    if not getattr(runtime.config.slack, 'channel_updates_enabled', False):
        return
    actor, channel = str(event.get('user') or ''), str(event.get('channel') or '')
    routes = getattr(runtime.config.slack, 'work_summary_channels', {})
    projects = [key for key, destination in routes.items() if destination == channel]
    service = ChannelUpdates(runtime.state_store)
    if not service.capture(event, projects[0] if len(projects) == 1 else ''):
        return
    # Capture-before-send gives duplicate suppression even after transport errors.
    # Never replay messages through the DM handler: clock commands stay private.
    source_ts = str(event.get('deleted_ts') or (event.get('message') or {}).get('ts') or event.get('ts') or '')
    try:
        link = await web_client.chat_getPermalink(channel=channel, message_ts=source_ts)
        with runtime.state_store._connect() as conn:
            conn.execute('UPDATE channel_work_updates SET permalink=? WHERE channel=? AND message_ts=?',
                         (str(link.get('permalink') or ''), channel, source_ts))
    except Exception:
        logger.warning('Channel work saved; permalink lookup unavailable.')
    # No receipt/reaction/question for every post. Reviewed useful summaries are
    # separate; raw channel content never automatically crosses into another room.

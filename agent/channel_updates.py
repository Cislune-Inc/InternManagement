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
            for name, definition in [('source_version', "TEXT NOT NULL DEFAULT '0'"), ('deleted', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('workspace_id', "TEXT NOT NULL DEFAULT ''"), ('user_key', "TEXT NOT NULL DEFAULT ''"),
                                     ('thread_ts', "TEXT NOT NULL DEFAULT ''")]:
                if name not in columns:
                    conn.execute(f'ALTER TABLE channel_work_updates ADD COLUMN {name} {definition}')
            conn.execute('''CREATE TABLE IF NOT EXISTS channel_work_revisions (
                channel TEXT NOT NULL, message_ts TEXT NOT NULL, source_version TEXT NOT NULL,
                payload TEXT NOT NULL, captured_at TEXT NOT NULL,
                PRIMARY KEY(channel,message_ts,source_version))''')

    def capture(self, event: dict[str, Any], project: str, *, user_key: str = '') -> bool:
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
                if deleted and actor == 'unknown':
                    actor = old['actor']
            conn.execute("""INSERT INTO channel_work_updates
                (channel,message_ts,actor,project_key,text,files_json,posted_at,captured_at,meaningful,source_version,deleted)
                VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(channel,message_ts) DO UPDATE SET
                actor=excluded.actor,project_key=excluded.project_key,text=excluded.text,
                files_json=excluded.files_json,captured_at=excluded.captured_at,
                meaningful=excluded.meaningful,source_version=excluded.source_version,deleted=excluded.deleted""",
                (channel, ts, actor, project, text, json.dumps(files), posted.isoformat(),
                 now.isoformat(), int(meaningful), version, int(deleted)))
            conn.execute('UPDATE channel_work_updates SET workspace_id=?,user_key=?,thread_ts=? WHERE channel=? AND message_ts=?',
                         (str(event.get('team') or (old['workspace_id'] if old else '')), user_key or (old['user_key'] if old else ''),
                          str(source.get('thread_ts') or (old['thread_ts'] if old else '')), channel, ts))
            return True

    def records(self, *, channels: list[str], after: str = '', limit: int = 100) -> list[dict[str, Any]]:
        """Planner adapter: caller MUST supply server-verified source grants.

        Includes tombstones so an authorized consumer can remove deleted inputs.
        IDs/revisions are observations, never accepted-plan or attendance events.
        """
        channels = list(dict.fromkeys(channels))[:100]
        if not channels:
            return []
        with self.store._connect() as conn:
            rows = conn.execute('SELECT * FROM channel_work_updates WHERE channel IN (' + ','.join('?' for _ in channels)
                                + ') AND captured_at>=? ORDER BY captured_at,channel,message_ts LIMIT ?',
                                (*channels, after, max(1,min(limit,500)))).fetchall()
        return [{**dict(r), 'files':json.loads(r['files_json']),
                 'person_ref':f"slack:{r['workspace_id']}:{r['actor']}" if r['workspace_id'] else None,
                 'source_ref':f"slack:{r['workspace_id']}:{r['channel']}:{r['message_ts']}",
                 'plan_status':'observation'} for r in rows]

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

    def review(self) -> str:
        """Owner-only bounded triage, not an inferred plan or automatic decision."""
        with self.store._connect() as conn:
            rows = conn.execute('SELECT * FROM channel_work_updates WHERE deleted=0 AND meaningful=1 ORDER BY posted_at DESC LIMIT 100').fetchall()
        items = []
        for row in rows:
            reasons = []
            if not row['project_key']:
                reasons.append('workstream/channel mapping needs review')
            if re.search(r'\b(?:blocked|waiting|need help|cannot|can.t)\b', row['text'], re.I):
                reasons.append('possible blocker: confirm the help or decision needed')
            if re.search(r'\b(?:stop|instead|switch|switching|abandon|approve|approval|should|could)\b', row['text'], re.I):
                reasons.append('possible direction change: compare with the accepted plan before deciding')
            if not reasons:
                continue
            source = row['permalink'] or f"Channel <#{row['channel']}> · {row['posted_at']}"
            items.append(f"{_safe(row['actor'])} · {_safe(PROJECTS.get(row['project_key'], 'Unmapped'))}\n"
                         + '; '.join(reasons) + '\n' + _safe(row['text'][:700]) + '\n' + source)
            if len(items) == 10:
                break
        return ('Channel review candidates — latest 100 useful captured posts; hints, not confirmed conflicts or approvals. '
                'Review the linked source and accepted plan with Erik/George. Nothing here changes assignments, hours or charging.\n\n'
                + '\n\n'.join(items)) if items else 'No channel review candidates in the latest captured posts. This does not establish that all work is aligned or that channel capture is active.'


async def handle(runtime: Any, web_client: Any, event: dict[str, Any]) -> None:
    if not getattr(runtime.config.slack, 'channel_updates_enabled', False):
        return
    source = event.get('message') or event.get('previous_message') or event
    actor, channel = str(source.get('user') or ''), str(event.get('channel') or '')
    routes = getattr(runtime.config.slack, 'work_summary_channels', {})
    projects = [key for key, destination in routes.items() if destination == channel]
    if len(projects) != 1:
        from .slack_work_intake import project_candidates
        candidates = project_candidates(str(source.get('text') or ''))
        projects = [key for key in candidates if not projects or key in projects]
    service = ChannelUpdates(runtime.state_store)
    person = getattr(runtime, 'roster_by_slack_id', {}).get(actor)
    if not service.capture(event, projects[0] if len(projects) == 1 else '', user_key=person.user_key if person else ''):
        return
    # Capture-before-send gives duplicate suppression even after transport errors.
    # Never replay messages through the DM handler: clock commands stay private.
    source_ts = str(event.get('deleted_ts') or (event.get('message') or {}).get('ts') or event.get('ts') or '')
    # A fresh useful channel post is an activity signal, never a clock start or
    # proof of hours. Old imports and edits must not keep unattended clocks alive.
    if person and person.active and actor in getattr(runtime.config.slack, 'work_intake_beta_slack_user_ids', []) and event.get('subtype') in {None, '', 'file_share', 'thread_broadcast'}:
        posted = datetime.fromtimestamp(float(source_ts), timezone.utc)
        age = (datetime.now(timezone.utc) - posted).total_seconds()
        with runtime.state_store._connect() as conn:
            row = conn.execute('SELECT meaningful FROM channel_work_updates WHERE channel=? AND message_ts=?', (channel, source_ts)).fetchone()
        with runtime.state_store._connect() as conn:
            active = conn.execute("SELECT 1 FROM sessions WHERE user_key=? AND json_extract(payload, '$.clocked_in_at') IS NOT NULL AND json_extract(payload, '$.clocked_out_at') IS NULL AND json_extract(payload, '$.metadata.slack_clock_beta')=1", (person.user_key,)).fetchone()
        if active and row and row[0] and 0 <= age <= 900:
            from .slack_beta import ledger
            async with runtime._user_session_lock(person.user_key):
                ledger(runtime).record_activity(person, str(source.get('text') or ''), posted)
    try:
        link = await web_client.chat_getPermalink(channel=channel, message_ts=source_ts)
        with runtime.state_store._connect() as conn:
            conn.execute('UPDATE channel_work_updates SET permalink=? WHERE channel=? AND message_ts=?',
                         (str(link.get('permalink') or ''), channel, source_ts))
    except Exception:
        logger.warning('Channel work saved; permalink lookup unavailable.')
    # Selective source-thread work questions; private timekeeping never enters
    # this path. Ordinary replies are already captured with source attribution.
    from .channel_followups import ChannelFollowups
    await ChannelFollowups(runtime.state_store).process(runtime, web_client, event)

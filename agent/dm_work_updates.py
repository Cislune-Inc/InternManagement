"""Owner-enabled, bounded DM work excerpts to explicit project destinations.

Never forwards conversation history, clock records, private file bytes or AI prose.
The model selects literal excerpts from one current work note; uncertain results
stay private. A durable attempt claim prevents blind retries after delivery errors.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .slack_work_intake import PROJECTS, _safe, project_candidates
from .work_ai import WorkAI
from .work_evidence import source_references


def private_note(text: str) -> bool:
    return bool(re.search(
        r"\b(?:private|confidential|do not share|don't share|keep this|password|token|secret|"
        r"salary|payroll|gusto|wages?|compensation|medical|doctor|sick|diagnos\w*|"
        r"fired|firing|offboarding|harass\w*|disciplin\w*|clock\w*|timesheet|"
        r"lunch|break|report hours|overtime|remote approval)\b|xox[baprs]-|sk-[A-Za-z0-9]", text, re.I))


def normalized(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower())) - {'work', 'update', 'the', 'a', 'i', 'and', 'for'}


class DMWorkUpdates:
    def __init__(self, store: Any) -> None:
        self.store = store
        with store._connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS dm_work_updates (
                id TEXT PRIMARY KEY, actor TEXT NOT NULL, item_id TEXT NOT NULL,
                revision INTEGER NOT NULL, source_ts TEXT NOT NULL, channel TEXT NOT NULL,
                fingerprint TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}', message_ts TEXT NOT NULL DEFAULT '')''')

    def duplicate_in_channel(self, actor: str, channel: str, text: str, now: datetime) -> bool:
        from .channel_updates import ChannelUpdates
        ChannelUpdates(self.store)
        with self.store._connect() as conn:
            rows = conn.execute('''SELECT text FROM channel_work_updates WHERE actor=? AND channel=?
                AND deleted=0 AND posted_at>=? ORDER BY posted_at DESC LIMIT 30''',
                (actor, channel, (now-timedelta(days=1)).isoformat())).fetchall()
        words = normalized(text)
        return bool(words) and any(len(words & normalized(r['text'])) / max(1, len(words | normalized(r['text']))) >= .8 for r in rows)

    async def process(self, runtime: Any, actor: str, text: str, event: dict[str, Any], item_id: str, *, ai: Any = None) -> str:
        """Return a private delivery receipt, or empty when not eligible."""
        config = runtime.config.slack
        if not getattr(config, 'dm_work_sharing_enabled', False):
            return ''
        # Only new ordinary notes, never historical replay or control commands.
        note = re.sub(r'^work\s+(?:update\s+)?', '', text, count=1, flags=re.I).strip()
        if re.match(r'^work\s+(?:next|detail|edit|project|approve|redirect|review|queue|status|options|help|evidence|handoff)\b', text, re.I):
            return ''
        if private_note(note) or len(note.split()) < 5 or len(note) > 6000:
            return ''
        now = datetime.now(timezone.utc)
        source_ts = str(event.get('ts') or event.get('event_ts') or '')
        try:
            age = (now - datetime.fromtimestamp(float(source_ts), timezone.utc)).total_seconds()
        except (ValueError, OverflowError, OSError):
            return ''
        if not 0 <= age <= 900:
            return ''
        with self.store._connect() as conn:
            item = conn.execute('SELECT * FROM work_intake_items WHERE id=? AND owner_id=?', (item_id, actor)).fetchone()
        if not item:
            return ''
        project = item['project_key']
        destination = config.work_summary_channels.get(project)
        candidates = project_candidates(note)
        if not destination or (candidates and candidates != [project]):
            return ''
        if self.duplicate_in_channel(actor, destination, note, now):
            return ''
        ident = hashlib.sha256(f'{actor}:{source_ts}'.encode()).hexdigest()
        fingerprint = hashlib.sha256(f'{actor}:{destination}:{sorted(normalized(note))}'.encode()).hexdigest()
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM dm_work_updates WHERE id=? OR (fingerprint=? AND created_at>=?)',
                            (ident, fingerprint, (now-timedelta(days=1)).isoformat())).fetchone():
                return ''
            rows = conn.execute('SELECT actor,channel,created_at FROM dm_work_updates WHERE created_at>=?',
                                ((now-timedelta(days=1)).isoformat(),)).fetchall()
            if len(rows) >= 30 or sum(r['actor'] == actor for r in rows) >= 4:
                return ''
            if any(r['actor'] == actor and r['channel'] == destination and
                   (now-datetime.fromisoformat(r['created_at'])).total_seconds() < 1800 for r in rows):
                return ''
            conn.execute('INSERT INTO dm_work_updates(id,actor,item_id,revision,source_ts,channel,fingerprint,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                         (ident,actor,item_id,item['revision'],source_ts,destination,fingerprint,'preparing',now.isoformat()))
        try:
            # No auto-join, shared-channel or arbitrary-recipient expansion.
            if not await runtime.slack.can_share_work(destination, actor):
                self.finish(ident, 'audience_unverified')
                return ''
            prepared = await (ai or WorkAI(self.store)).coach(actor, note, context=f'Current routing label: {project}.', channel_only=True)
            if not prepared or not prepared.get('shareable') or prepared.get('project_suggestion') != project:
                self.finish(ident, 'held')
                return ''
            excerpts = prepared.get('excerpts', [])
            if not excerpts or any(not isinstance(s,str) or s not in note or private_note(s) or 'http' in s.lower() for s in excerpts):
                self.finish(ident, 'held')
                return ''
            with self.store._connect() as conn:
                current = conn.execute('SELECT revision,project_key FROM work_intake_items WHERE id=?', (item_id,)).fetchone()
            if not current or current['revision'] != item['revision'] or current['project_key'] != project or self.duplicate_in_channel(actor,destination,note,now):
                self.finish(ident, 'superseded')
                return ''
            # Explicitly attributed worker report, never model-written approvals.
            message = f"*{_safe(PROJECTS[project])} · work update from <@{actor}>*\n" + '\n'.join('> ' + _safe(s) for s in excerpts)
            refs = [r['url'] for r in source_references(note) if r['source'] != 'chatgpt'][:3]
            if refs:
                message += '\n' + '\n'.join(refs)
            message += '\n_Shared by Don Pollo from a work update; plans remain proposals._'
            self.finish(ident, 'attempting', {'text':message, 'project_key':project, 'source_item':item_id, 'source_revision':item['revision']})
            result = await runtime.slack.post_message(destination, message)
            if not isinstance(result,dict) or not result.get('ts'):
                return ''  # Attempt is uncertain; never retry automatically.
            with self.store._connect() as conn:
                conn.execute("UPDATE dm_work_updates SET status='sent',message_ts=? WHERE id=?", (result['ts'],ident))
            return f"Shared your work update in <#{destination}>. Prefer posting there directly with links/photos; DP can pick it up without a mention."
        except Exception:
            # Fixed state only; never log private note or credential. No blind send retry.
            with self.store._connect() as conn:
                conn.execute("UPDATE dm_work_updates SET status='held' WHERE id=? AND status='preparing'", (ident,))
            return ''

    def finish(self, ident: str, status: str, payload: dict[str, Any] | None = None) -> None:
        with self.store._connect() as conn:
            conn.execute('UPDATE dm_work_updates SET status=?,payload=? WHERE id=?', (status,json.dumps(payload or {}),ident))

    def published_records(self, *, channels: list[str], limit: int = 100) -> list[dict[str, Any]]:
        """Only already-published excerpts, for server-verified channel grants.

        Does not expose original DM text, DM timestamp or unshared/held records.
        """
        channels=list(dict.fromkeys(channels))[:100]
        if not channels:
            return []
        with self.store._connect() as conn:
            rows=conn.execute("SELECT actor,channel,message_ts,payload FROM dm_work_updates WHERE status='sent' AND channel IN ("
                              + ','.join('?' for _ in channels) + ') ORDER BY created_at DESC LIMIT ?',
                              (*channels,max(1,min(limit,500)))).fetchall()
        return [{'actor':r['actor'],'channel':r['channel'],'message_ts':r['message_ts'],
                 'text':json.loads(r['payload']).get('text',''),
                 'project_key':json.loads(r['payload']).get('project_key',''),
                 'plan_status':'observation','source_kind':'dp_published_work_excerpt'} for r in rows]

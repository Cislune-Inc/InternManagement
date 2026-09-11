"""Explicit project-channel mentions; source-linked work, never attendance."""
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

    def capture(self, event: dict[str, Any], project: str) -> bool:
        # Only an explicit bot mention is an update in the first release.
        # Ordinary chatter, edits/deletes and bot messages are not new progress.
        if event.get('type') != 'app_mention' or event.get('bot_id') or event.get('subtype'):
            return False
        actor, channel, ts = (str(event.get(k) or '') for k in ('user', 'channel', 'ts'))
        if not actor or not channel or not re.fullmatch(r'\d+\.\d+', ts):
            return False
        now = datetime.now(timezone.utc)
        try:
            posted = datetime.fromtimestamp(float(ts), timezone.utc)
        except (ValueError, OverflowError, OSError):
            return False
        if posted > now:
            return False
        text = re.sub(r'<@[A-Z0-9]+>', '', str(event.get('text') or '')).strip()[:6000]
        files = [{k: f[k] for k in ('id', 'name', 'mimetype') if k in f}
                 for f in event.get('files', [])[:5] if isinstance(f, dict)]
        meaningful = len(text.split()) >= 5 and not re.fullmatch(
            r'(?:hello|hi|thanks|thank you|clock in|clock out|break|lunch|back|hours)[.! ]*', text, re.I)
        with self.store._connect() as conn:
            cur = conn.execute("""INSERT OR IGNORE INTO channel_work_updates
                (channel,message_ts,actor,project_key,text,files_json,posted_at,captured_at,meaningful)
                VALUES (?,?,?,?,?,?,?,?,?)""", (channel, ts, actor, project, text,
                json.dumps(files), posted.isoformat(), now.isoformat(), int(meaningful)))
            return cur.rowcount == 1

    def latest(self, actor: str) -> str | None:
        with self.store._connect() as conn:
            return conn.execute('SELECT MAX(posted_at) FROM channel_work_updates WHERE actor=? AND meaningful=1', (actor,)).fetchone()[0]

    def recent(self, actor: str | None = None) -> str:
        with self.store._connect() as conn:
            rows = conn.execute('SELECT * FROM channel_work_updates WHERE (? IS NULL OR actor=?) ORDER BY posted_at DESC LIMIT 10', (actor, actor)).fetchall()
        return ('Recent channel updates (worker reports, not work/charging approvals):\n' + '\n\n'.join(
            f"{_safe(PROJECTS.get(r['project_key'], r['project_key']))} · <@{r['actor']}>\n"
            f"{_safe(r['text'])}\n" + (r['permalink'] or f"Channel <#{r['channel']}> · {r['posted_at']}")
            for r in rows)) if rows else 'No channel updates captured yet. Mention Don Pollo with your update in an approved project channel.'


async def handle(runtime: Any, web_client: Any, event: dict[str, Any]) -> None:
    from .slack_beta import clock_user, enabled
    if not getattr(runtime.config.slack, 'channel_updates_enabled', False):
        return
    actor, channel = str(event.get('user') or ''), str(event.get('channel') or '')
    routes = getattr(runtime.config.slack, 'work_summary_channels', {})
    projects = [key for key, destination in routes.items() if destination == channel]
    if len(projects) != 1 or not enabled(runtime, actor):
        return
    user = clock_user(runtime, actor)
    if user is None or not user.active:
        return
    service = ChannelUpdates(runtime.state_store)
    if not service.capture(event, projects[0]):
        return
    # Capture-before-send gives duplicate suppression even after transport errors.
    # Never replay messages through the DM handler: clock commands stay private.
    try:
        link = await web_client.chat_getPermalink(channel=channel, message_ts=event['ts'])
        with runtime.state_store._connect() as conn:
            conn.execute('UPDATE channel_work_updates SET permalink=? WHERE channel=? AND message_ts=?',
                         (str(link.get('permalink') or ''), channel, event['ts']))
    except Exception:
        logger.warning('Channel work saved; permalink lookup unavailable.')
    with runtime.state_store._connect() as conn:
        row = conn.execute('SELECT meaningful FROM channel_work_updates WHERE channel=? AND message_ts=?', (channel,event['ts'])).fetchone()
    message = ('Saved for the project update—no need to repeat it in DM. Add a result photo or company file link here if useful.'
               if row['meaningful'] else 'What changed, what will you finish next, or what is blocking you? Add a short update here and mention me again so I can capture it.')
    try:
        await runtime.slack.post_message(channel, message, thread_ts=event.get('thread_ts') or event['ts'])
    except Exception:
        logger.warning('Channel work saved; acknowledgement delivery uncertain, not retried.')

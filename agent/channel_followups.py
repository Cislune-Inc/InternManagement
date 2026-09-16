"""Selective work questions in their source thread, never timekeeping questions.

Read the live thread twice around optional AI. Any unavailable/truncated context,
reply, edit, audience change or uncertain delivery fails quiet. No clock API.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .dm_work_updates import private_note
from .work_ai import WorkAI


class ChannelFollowups:
    def __init__(self, store: Any) -> None:
        self.store = store
        with store._connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS channel_work_questions (
                channel TEXT NOT NULL, root_ts TEXT NOT NULL, actor TEXT NOT NULL,
                created_at TEXT NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY(channel,root_ts))''')

    async def process(self, runtime: Any, web: Any, event: dict[str, Any], *, ai: Any = None) -> None:
        if not getattr(runtime.config.slack, 'dm_work_sharing_enabled', False):
            return
        if event.get('subtype') not in {None, '', 'file_share'} or event.get('bot_id'):
            return
        actor, channel, ts = (str(event.get(k) or '') for k in ('user', 'channel', 'ts'))
        if event.get('thread_ts') and event['thread_ts'] != ts:
            return  # Replies supply context, not another interrogation.
        text = str(event.get('text') or '')
        # A deliberately narrow first release: only a terse unexplained blocker.
        # Rich updates, explicit peer questions and explained dependencies need no bot.
        if (private_note(text) or not re.search(r'\bblocked\b', text, re.I)
                or len(text.split()) > 12 or '?' in text
                or re.search(r'\b(?:because|waiting for|need|due to)\b', text, re.I)):
            return
        now = datetime.now(timezone.utc)
        try:
            if not 0 <= (now-datetime.fromtimestamp(float(ts), timezone.utc)).total_seconds() <= 900:
                return
        except (ValueError, OSError, OverflowError):
            return
        if channel not in getattr(runtime.config.slack, 'work_summary_channels', {}).values():
            return
        with self.store._connect() as conn:
            if conn.execute('SELECT 1 FROM channel_work_questions WHERE channel=? AND root_ts=?', (channel,ts)).fetchone():
                return
        async def snapshot():
            if not await runtime.slack.can_share_work(channel, actor):
                return None
            page = await web.conversations_replies(channel=channel, ts=ts, limit=20)
            messages = page.get('messages', [])
            if page.get('has_more') or page.get('response_metadata', {}).get('next_cursor') or not messages:
                return None
            root = messages[0]
            if root.get('ts') != ts or root.get('user') != actor or root.get('text') != text or root.get('edited'):
                return None
            # Human or bot response already exists: leave the conversation alone.
            if len(messages) != 1 or root.get('reply_count', 0):
                return None
            with self.store._connect() as conn:
                row = conn.execute('SELECT text,deleted FROM channel_work_updates WHERE channel=? AND message_ts=?', (channel,ts)).fetchone()
                replies = conn.execute('SELECT 1 FROM channel_work_updates WHERE channel=? AND thread_ts=? AND message_ts!=? AND deleted=0', (channel,ts,ts)).fetchone()
            if not row or row['deleted'] or row['text'] != text or replies:
                return None
            return json.dumps(messages, sort_keys=True)
        try:
            before = await snapshot()
            if before is None:
                return
            draft = await (ai or WorkAI(self.store)).coach(actor, text, context=(
                'Source is a project-channel thread with no replies. Only ask a short question '
                'if the blocker lacks an actionable help or decision request. No hours or '
                'personal questions. No private notes or accepted plan supplied.'))
            question = str((draft or {}).get('follow_up_question') or '').strip()
            if (not question or len(question) > 240 or private_note(question)
                    or re.search(r'\b(?:hours?|paid|unpaid|attendance|shift|pay|when did|what time)\b|https?://|<[@!#]', question, re.I)):
                return
            if await snapshot() != before:
                return
            with self.store._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                count = conn.execute('SELECT COUNT(*) FROM channel_work_questions WHERE actor=? AND created_at>=?',
                                     (actor,(now-timedelta(days=1)).isoformat())).fetchone()[0]
                if count >= 2:
                    return
                inserted = conn.execute('INSERT OR IGNORE INTO channel_work_questions VALUES (?,?,?,?,?)',
                                        (channel,ts,actor,now.isoformat(),'attempting')).rowcount
                if not inserted:
                    return
            # AI identifies a gap; fixed work-only copy cannot leak an hours question.
            result = await runtime.slack.post_message(channel,
                'What is blocking this work, and what help or decision would unblock it?', thread_ts=ts)
            if isinstance(result, dict) and result.get('ts'):
                with self.store._connect() as conn:
                    conn.execute("UPDATE channel_work_questions SET status='sent' WHERE channel=? AND root_ts=?", (channel,ts))
        except Exception:
            return  # No blind retry, text/credential logging, or clock fallback.

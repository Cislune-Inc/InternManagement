from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .models import AttachmentRecord, MessageRecord, SessionState


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    user_key TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(user_key, session_date)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_key TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    author_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    content TEXT NOT NULL,
                    attachments_json TEXT NOT NULL
                );

                DELETE FROM messages
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM messages
                    GROUP BY user_key, session_date, message_id, direction
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_unique
                ON messages(user_key, session_date, message_id, direction);
                """
            )

    def get_session(self, user_key: str, session_date: str) -> SessionState:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM sessions WHERE user_key = ? AND session_date = ?",
                (user_key, session_date),
            ).fetchone()
        if not row:
            return SessionState(user_key=user_key, session_date=session_date)
        payload = json.loads(row["payload"])
        return SessionState(**payload)

    def save_session(self, session: SessionState) -> None:
        payload = json.dumps(asdict(session), sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions(user_key, session_date, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(user_key, session_date)
                DO UPDATE SET payload = excluded.payload
                """,
                (session.user_key, session.session_date, payload),
            )

    def append_message(self, user_key: str, session_date: str, message: MessageRecord) -> bool:
        attachments_json = json.dumps([asdict(attachment) for attachment in message.attachments])
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO messages(
                    user_key, session_date, message_id, direction, author_id, created_at, content, attachments_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_key,
                    session_date,
                    message.message_id,
                    message.direction,
                    message.author_id,
                    message.created_at.isoformat(),
                    message.content,
                    attachments_json,
                ),
            )
        return cursor.rowcount > 0

    def list_messages(self, user_key: str, session_date: str) -> list[MessageRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, direction, author_id, created_at, content, attachments_json
                FROM messages
                WHERE user_key = ? AND session_date = ?
                ORDER BY created_at ASC, id ASC
                """,
                (user_key, session_date),
            ).fetchall()
        records: list[MessageRecord] = []
        for row in rows:
            attachments = [
                AttachmentRecord(**attachment)
                for attachment in json.loads(row["attachments_json"])
            ]
            records.append(
                MessageRecord(
                    message_id=row["message_id"],
                    direction=row["direction"],
                    author_id=int(row["author_id"]),
                    created_at=datetime.fromisoformat(row["created_at"]),
                    content=row["content"],
                    attachments=attachments,
                )
            )
        return records

    def list_sessions_for_date(self, session_date: str) -> list[SessionState]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM sessions WHERE session_date = ? ORDER BY user_key ASC",
                (session_date,),
            ).fetchall()
        return [SessionState(**json.loads(row["payload"])) for row in rows]

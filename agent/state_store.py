from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .models import AttachmentRecord, MessageRecord, SessionState


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

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

                CREATE TABLE IF NOT EXISTS operational_state (
                    state_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operational_issues (
                    fingerprint TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'open',
                    last_notified_at TEXT,
                    resolved_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_operational_issues_status
                ON operational_issues(status, last_seen_at DESC);
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (1, ?)
                """,
                (datetime.now().astimezone().isoformat(),),
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

    def get_operational_state(self, state_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM operational_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def set_operational_state(self, state_key: str, payload: dict[str, Any]) -> None:
        serialized = json.dumps(payload, sort_keys=True)
        updated_at = datetime.now().astimezone().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operational_state(state_key, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key)
                DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
                """,
                (state_key, serialized, updated_at),
            )

    def record_operational_issue(
        self,
        *,
        fingerprint: str,
        category: str,
        severity: str,
        summary: str,
        details: dict[str, Any] | None = None,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        now = (observed_at or datetime.now().astimezone()).isoformat()
        details_json = json.dumps(details or {}, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operational_issues(
                    fingerprint, category, severity, summary, details_json,
                    first_seen_at, last_seen_at, occurrence_count, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'open')
                ON CONFLICT(fingerprint)
                DO UPDATE SET
                    category = excluded.category,
                    severity = excluded.severity,
                    summary = excluded.summary,
                    details_json = excluded.details_json,
                    last_seen_at = excluded.last_seen_at,
                    occurrence_count = operational_issues.occurrence_count + 1,
                    status = 'open',
                    resolved_at = NULL
                """,
                (
                    fingerprint,
                    category,
                    severity,
                    summary,
                    details_json,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM operational_issues WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        return self._operational_issue_row(row)

    def operational_issue_needs_notification(
        self,
        fingerprint: str,
        *,
        cooldown: timedelta,
        now: datetime | None = None,
    ) -> bool:
        reference = now or datetime.now().astimezone()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_notified_at FROM operational_issues WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        if row is None or not row["last_notified_at"]:
            return True
        try:
            notified_at = datetime.fromisoformat(row["last_notified_at"])
        except ValueError:
            return True
        if notified_at.tzinfo is None and reference.tzinfo is not None:
            notified_at = notified_at.replace(tzinfo=reference.tzinfo)
        return reference - notified_at >= cooldown

    def mark_operational_issue_notified(
        self,
        fingerprint: str,
        *,
        notified_at: datetime | None = None,
    ) -> None:
        timestamp = (notified_at or datetime.now().astimezone()).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE operational_issues
                SET last_notified_at = ?
                WHERE fingerprint = ?
                """,
                (timestamp, fingerprint),
            )

    def resolve_operational_issue(
        self,
        fingerprint: str,
        *,
        resolved_at: datetime | None = None,
    ) -> bool:
        timestamp = (resolved_at or datetime.now().astimezone()).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE operational_issues
                SET status = 'resolved', resolved_at = ?
                WHERE fingerprint = ? AND status != 'resolved'
                """,
                (timestamp, fingerprint),
            )
        return cursor.rowcount > 0

    def resolve_matching_operational_issues(
        self,
        *,
        category: str,
        details_match: dict[str, Any],
        resolved_at: datetime | None = None,
    ) -> int:
        timestamp = (resolved_at or datetime.now().astimezone()).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT fingerprint, details_json
                FROM operational_issues
                WHERE category = ? AND status = 'open'
                """,
                (category,),
            ).fetchall()
            matching: list[str] = []
            for row in rows:
                try:
                    details = json.loads(row["details_json"])
                except json.JSONDecodeError:
                    continue
                if not isinstance(details, dict):
                    continue
                if all(
                    details.get(key) == value
                    for key, value in details_match.items()
                ):
                    matching.append(str(row["fingerprint"]))
            if matching:
                conn.executemany(
                    """
                    UPDATE operational_issues
                    SET status = 'resolved', resolved_at = ?
                    WHERE fingerprint = ? AND status = 'open'
                    """,
                    [(timestamp, fingerprint) for fingerprint in matching],
                )
        return len(matching)

    def merge_operational_issues(
        self,
        *,
        target_fingerprint: str,
        source_fingerprints: list[str],
        resolved_at: datetime | None = None,
    ) -> bool:
        fingerprints = list(
            dict.fromkeys(
                fingerprint.strip()
                for fingerprint in source_fingerprints
                if fingerprint.strip()
            )
        )
        if not target_fingerprint.strip() or not fingerprints:
            return False
        placeholders = ",".join("?" for _ in fingerprints)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM operational_issues WHERE fingerprint IN ({placeholders})",
                fingerprints,
            ).fetchall()
            if not rows:
                return False
            newest = max(rows, key=lambda row: str(row["last_seen_at"] or ""))
            first_seen_at = min(str(row["first_seen_at"] or "") for row in rows)
            last_seen_at = max(str(row["last_seen_at"] or "") for row in rows)
            occurrence_count = sum(int(row["occurrence_count"] or 1) for row in rows)
            notified_values = [
                str(row["last_notified_at"])
                for row in rows
                if row["last_notified_at"]
            ]
            last_notified_at = max(notified_values) if notified_values else None
            conn.execute(
                """
                INSERT INTO operational_issues(
                    fingerprint, category, severity, summary, details_json,
                    first_seen_at, last_seen_at, occurrence_count, status,
                    last_notified_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, NULL)
                ON CONFLICT(fingerprint)
                DO UPDATE SET
                    category = excluded.category,
                    severity = excluded.severity,
                    summary = excluded.summary,
                    details_json = excluded.details_json,
                    first_seen_at = excluded.first_seen_at,
                    last_seen_at = excluded.last_seen_at,
                    occurrence_count = excluded.occurrence_count,
                    status = 'open',
                    last_notified_at = excluded.last_notified_at,
                    resolved_at = NULL
                """,
                (
                    target_fingerprint,
                    str(newest["category"]),
                    str(newest["severity"]),
                    str(newest["summary"]),
                    str(newest["details_json"]),
                    first_seen_at,
                    last_seen_at,
                    occurrence_count,
                    last_notified_at,
                ),
            )
            superseded = [
                fingerprint
                for fingerprint in fingerprints
                if fingerprint != target_fingerprint
            ]
            if superseded:
                timestamp = (resolved_at or datetime.now().astimezone()).isoformat()
                conn.executemany(
                    """
                    UPDATE operational_issues
                    SET status = 'resolved', resolved_at = ?
                    WHERE fingerprint = ? AND status = 'open'
                    """,
                    [(timestamp, fingerprint) for fingerprint in superseded],
                )
        return True

    def list_operational_issues(
        self,
        *,
        status: str | None = "open",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM operational_issues"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY last_seen_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._operational_issue_row(row) for row in rows]

    def health_snapshot(self) -> dict[str, Any]:
        with self._connect() as conn:
            migration_row = conn.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            open_issue_row = conn.execute(
                "SELECT COUNT(*) AS count FROM operational_issues WHERE status = 'open'"
            ).fetchone()
        return {
            "database_path": str(self.db_path.resolve()),
            "schema_version": int(migration_row["version"] or 0),
            "open_issue_count": int(open_issue_row["count"] or 0),
        }

    def _operational_issue_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            details = {}
        return {
            "fingerprint": row["fingerprint"],
            "category": row["category"],
            "severity": row["severity"],
            "summary": row["summary"],
            "details": details if isinstance(details, dict) else {},
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "occurrence_count": int(row["occurrence_count"]),
            "status": row["status"],
            "last_notified_at": row["last_notified_at"],
            "resolved_at": row["resolved_at"],
        }

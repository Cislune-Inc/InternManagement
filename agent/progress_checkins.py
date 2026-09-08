"""Bounded, private work prompts. Never modifies attendance or payroll."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .slack_timekeeping import timestamp
from .slack_work_intake import SlackWorkIntake, _safe


class ProgressCheckins:
    def __init__(self, store: Any) -> None:
        self.store = store
        SlackWorkIntake(store)
        with store._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS progress_checkins (
                actor TEXT NOT NULL, day TEXT NOT NULL, last_prompt TEXT,
                snoozed_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(actor,day))""")

    def snooze(self, actor: str, day: str, now: datetime) -> None:
        with self.store._connect() as conn:
            conn.execute("""INSERT INTO progress_checkins(actor,day,snoozed_until)
                VALUES (?,?,?) ON CONFLICT(actor,day) DO UPDATE SET
                snoozed_until=excluded.snoozed_until""",
                (actor, day, (now + timedelta(hours=1)).isoformat()))

    def claim(self, actor: str, session: Any, now: datetime) -> str | None:
        if (not session or not session.clocked_in_at or session.clocked_out_at
                or not session.metadata.get("slack_clock_beta")
                or session.stage == "on_lunch_break"
                or session.metadata.get("slack_clock_rest_started_at")
                or session.metadata.get("slack_clock_meal_started_at")):
            return None
        now = now.astimezone(timezone.utc)
        anchors = [timestamp(session.clocked_in_at)]
        for segment in session.work_segments:
            if not segment.get("clocked_out_at"):
                anchors.append(timestamp(segment.get("clocked_in_at")))
        for rest in session.metadata.get("paid_rest_windows", []):
            anchors.append(timestamp(rest.get("ended_at")))
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM progress_checkins WHERE actor=? AND day=?",
                               (actor, session.session_date)).fetchone()
            if row:
                if row["attempts"] >= 3 or (timestamp(row["snoozed_until"]) or now) > now:
                    return None
                anchors.append(timestamp(row["last_prompt"]))
            activity = conn.execute("""SELECT MAX(created_at) FROM work_intake_events
                WHERE actor_id=? AND kind IN ('proposal','detail','update','edit','next')""", (actor,)).fetchone()[0]
            anchors.append(timestamp(activity))
            if now - max(a for a in anchors if a) < timedelta(hours=2):
                return None
            item = conn.execute("""SELECT project_key FROM work_intake_items WHERE owner_id=?
                ORDER BY created_at DESC,rowid DESC LIMIT 1""", (actor,)).fetchone()
            project = _safe(item[0].upper()) if item and item[0] else "your work"
            # Claim before transport: uncertain delivery is not retried and
            # cannot produce a burst after restart. At most three attempts/day.
            conn.execute("""INSERT INTO progress_checkins(actor,day,last_prompt,attempts)
                VALUES (?,?,?,1) ON CONFLICT(actor,day) DO UPDATE SET
                last_prompt=excluded.last_prompt,attempts=attempts+1""",
                (actor, session.session_date, now.isoformat()))
        return (f"Quick check-in on {project}, when you reach a stopping point: "
                "what changed, what is blocked, or what will you finish next? One concrete sentence is enough; "
                "include a company file/link if useful. You can type normally here. "
                "Reply `snooze` for an hour of focus time. This progress prompt does not stop your clock "
                "or change break, hours-limit, or inactivity rules. Nothing is posted to a channel without your preview and share action.")


async def tick(runtime: Any, user: Any, session: Any, now: datetime) -> None:
    if not getattr(runtime.config.slack, "progress_checkins_enabled", False):
        return
    message = ProgressCheckins(runtime.state_store).claim(user.slack_user_id, session, now)
    if message:
        await runtime.slack.post_message(user.slack_user_id, message)

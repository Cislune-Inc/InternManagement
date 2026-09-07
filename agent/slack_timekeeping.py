"""Hours-first Slack beta on the existing durable SessionState ledger.

Deterministic transitions only: models never create timestamps or decide pay.
Automatic stops record an instruction to stop, not a fictional unpaid meal.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .models import SessionState, UserProfile


HELP = (
    "*Don Pollo time clock*\n"
    "`clock in onsite` · `clock out` · `lunch` · `back` · `break` · `hours`\n"
    "You can add what you are doing after clock-in; task selection is not required. "
    "`report hours <date, actual start/end, breaks and what needs correcting>` saves an exception. "
    "If DP fails, Slack Erik your actual hours. Do not use Gusto Kiosk."
)
FALLBACK = "If you worked outside the recorded interval, use `report hours` or Slack Erik the actual times; all work must be recorded."


def timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Time entries require an explicit timezone.")
    return parsed.astimezone(timezone.utc)


def clock_command(text: str) -> tuple[str, str] | None:
    text = text.strip()
    for pattern, command in [
        (r"(?:clock[ -]?in|start work)(?:\s+(.*))?", "in"),
        (r"(?:clock[ -]?out|stop work|done for (?:the )?day)", "out"),
        (r"(?:lunch|start lunch)", "lunch"),
        (r"(?:break|short break|start break)", "rest"),
        (r"(?:back|back from (?:lunch|break)|resume)", "back"),
        (r"(?:hours|my hours|time|status)", "hours"),
        (r"(?:clock help|help|clock)", "help"),
        (r"report hours\s+(.+)", "report"),
    ]:
        match = re.fullmatch(pattern, text, re.I | re.S)
        if match:
            return command, (match.group(1) or "").strip() if match.lastindex else ""
    return None


def _union(bounds: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(bounds):
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def paid_seconds(sessions: list[SessionState], now: datetime,
                 window_start: datetime | None = None, window_end: datetime | None = None) -> int:
    now = now.astimezone(timezone.utc)
    window_start = window_start.astimezone(timezone.utc) if window_start else None
    window_end = window_end.astimezone(timezone.utc) if window_end else None
    work, meals = [], []
    for session in sessions:
        segments = session.work_segments or [{"clocked_in_at": session.clocked_in_at, "clocked_out_at": session.clocked_out_at}]
        for segment in segments:
            start, end = timestamp(segment.get("clocked_in_at")), timestamp(segment.get("clocked_out_at")) or now
            if start:
                work.append((max(start, window_start) if window_start else start,
                             min(end, window_end or now)))
        windows = list(session.metadata.get("lunch_windows") or [])
        if session.metadata.get("lunch_started_at"):
            windows.append({"started_at": session.metadata["lunch_started_at"], "ended_at": session.metadata.get("lunch_ended_at")})
        for meal in windows:
            if meal.get("paid_pending_review"):
                continue
            start = timestamp(meal.get("started_at"))
            end = timestamp(meal.get("ended_at")) or now
            if start:
                meals.append((start, min(end, now)))
    work, meals = _union(work), _union(meals)
    total = sum((end - start).total_seconds() for start, end in work)
    deduction = sum(max(0, (min(end, me) - max(start, ms)).total_seconds())
                    for start, end in work for ms, me in meals)
    return max(0, int(total - deduction))


class SlackTimekeeping:
    def __init__(self, state_store: Any, *, timezone_name: str = "America/Los_Angeles",
                 daily_limit_hours: float = 8, weekly_limit_hours: float = 40) -> None:
        self.store = state_store
        self.zone = ZoneInfo(timezone_name)
        self.daily_limit = daily_limit_hours * 3600
        self.weekly_limit = weekly_limit_hours * 3600
        with self.store._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS slack_clock_receipts (
                    event_key TEXT PRIMARY KEY, user_key TEXT NOT NULL,
                    session_date TEXT NOT NULL, response TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS slack_clock_reports (
                    id TEXT PRIMARY KEY, user_key TEXT NOT NULL, reported_at TEXT NOT NULL,
                    text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS slack_clock_approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_key TEXT NOT NULL,
                    kind TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
                    approved_at TEXT NOT NULL, approver TEXT NOT NULL, reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS slack_clock_notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_key TEXT NOT NULL,
                    session_date TEXT NOT NULL, text TEXT NOT NULL, delivered_at TEXT
                );
            """)

    def _sessions(self, conn: Any, user_key: str) -> list[SessionState]:
        return [SessionState(**json.loads(row["payload"])) for row in conn.execute(
            "SELECT payload FROM sessions WHERE user_key=? ORDER BY session_date", (user_key,)
        )]

    def _current(self, sessions: list[SessionState], user: UserProfile, now: datetime) -> SessionState:
        active = [s for s in sessions if s.clocked_in_at and (not s.clocked_out_at or s.metadata.get("slack_clock_meal_started_at"))]
        if len(active) > 1:
            raise ValueError("More than one open shift needs reconciliation. Slack Erik your actual hours; do not create another clock.")
        if active:
            return active[0]
        today = now.astimezone(self.zone).date().isoformat()
        return next((s for s in sessions if s.session_date == today), SessionState(user_key=user.user_key, session_date=today))

    def handle(self, user: UserProfile, command: str, detail: str, *, event_id: str,
               now: datetime) -> tuple[str, SessionState | None]:
        now = now.astimezone(timezone.utc)
        key = f"{user.user_key}:{event_id}"
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM slack_clock_receipts WHERE event_key=?", (key,)).fetchone()
            if old:
                current = conn.execute("SELECT payload FROM sessions WHERE user_key=? AND session_date=?",
                                       (user.user_key, old['session_date'])).fetchone()
                return old["response"], SessionState(**json.loads(current[0])) if current else None
            if command == "report":
                conn.execute("INSERT INTO slack_clock_reports(id,user_key,reported_at,text) VALUES (?,?,?,?)",
                             (key, user.user_key, now.isoformat(), detail))
                response = "Your actual-hours report is saved for manager reconciliation. It has not been converted into guessed timestamps. " + FALLBACK
                conn.execute("INSERT INTO slack_clock_receipts VALUES (?,?,?,?)", (key, user.user_key, "", response))
                return response, None
            sessions = self._sessions(conn, user.user_key)
            session = self._current(sessions, user, now)
            if not any(s.session_date == session.session_date for s in sessions):
                sessions.append(session)
            previous_event = timestamp(session.metadata.get("slack_clock_last_event_at"))
            if previous_event and now < previous_event:
                return "This message arrived out of order. No clock time was changed. " + FALLBACK, None
            response = self._apply(conn, user, session, sessions, command, detail, now)
            if command not in {"help", "hours"}:
                session.metadata["slack_clock_last_event_at"] = now.isoformat()
                session.last_user_message_at = now.isoformat()
                session.last_contact_at = now.isoformat()
                session.metadata.setdefault("slack_clock_events", []).append({
                    "event_id": event_id, "action": command, "at": now.isoformat(), "detail": detail[:6000],
                })
            self._save(conn, session)
            conn.execute("INSERT INTO slack_clock_receipts VALUES (?,?,?,?)", (key, user.user_key, session.session_date, response))
            return response, session

    @staticmethod
    def _save(conn: Any, session: SessionState) -> None:
        conn.execute("INSERT INTO sessions VALUES (?,?,?) ON CONFLICT(user_key,session_date) DO UPDATE SET payload=excluded.payload",
                     (session.user_key, session.session_date, json.dumps(asdict(session), sort_keys=True)))

    def totals(self, sessions: list[SessionState], now: datetime) -> tuple[int, int]:
        day = now.astimezone(self.zone).replace(hour=0, minute=0, second=0, microsecond=0)
        week = day - timedelta(days=day.weekday())
        return paid_seconds(sessions, now, day), paid_seconds(sessions, now, week)

    def _apply(self, conn: Any, user: UserProfile, session: SessionState,
               sessions: list[SessionState], command: str, detail: str, now: datetime) -> str:
        if command == "help":
            return HELP
        running = bool(session.clocked_in_at and not session.clocked_out_at)
        daily, weekly = self.totals(sessions, now)
        overtime_authorized = self._authorized(conn, user.user_key, "overtime", now)
        over_limit = user.worker_type != "admin" and not overtime_authorized and (
            daily >= self.daily_limit or weekly >= self.weekly_limit or self._seventh_day(sessions, now)
        )
        if command == "hours":
            state = "on lunch" if session.stage == "on_lunch_break" else "clocked in" if running else "clocked out"
            return f"You are {state}. Recorded work + paid rest: today {daily / 3600:.2f} h; this week {weekly / 3600:.2f} h ({self.zone.key}).\n" + FALLBACK
        # First use takes over the same existing attendance record, never a
        # second shadow clock. Preserve but close any legacy local task timer.
        if not session.metadata.get("slack_clock_beta"):
            tracking = session.metadata.pop("clickup_time_tracking", None)
            if tracking:
                tracking["closed_at"] = now.isoformat()
                tracking["end_reason"] = "slack_clock_takeover"
                session.metadata.setdefault("clickup_time_tracking_history", []).append(tracking)
            session.metadata["slack_clock_beta"] = True
        if command == "in":
            if running:
                return "You are already clocked in; no duplicate time was added. Use `hours` for totals or `back` after a break."
            remote = bool(re.match(r"remote\b", detail, re.I))
            if remote and user.worker_type != "admin" and not self._authorized(conn, user.user_key, "remote", now):
                return "Remote work needs Erik's advance approval. No new work is authorized here. " + FALLBACK
            if not remote and not re.match(r"onsite\b", detail, re.I):
                return "Reply `clock in onsite` to confirm you are at the shop. This is an attestation, not a location check. " + FALLBACK
            if over_limit:
                return "The daily or weekly hours limit is reached. Stop work and contact Erik for authorization. " + FALLBACK
            if session.metadata.get("slack_clock_rest_started_at"):
                return "Reply `back` to record your actual return from rest, then clock in onsite. " + FALLBACK
            if user.meal_tracking_required and self._meal_due(session, now):
                return "Take your meal break now: reply `lunch` when it actually starts. If the record is wrong, use `report hours`; do not invent a compliant time."
            session.clocked_in_at = session.clocked_in_at or now.isoformat()
            session.clocked_out_at = None
            session.work_segments.append({"clocked_in_at": now.isoformat(), "clocked_out_at": None})
            session.stage = "active"
            session.intake_completed_at = session.intake_completed_at or now.isoformat()
            session.awaiting_start_photo = False
            session.awaiting_clock_out_photo = False
            session.awaiting_clock_out_summary = False
            session.metadata.pop("slack_clock_stop_reason", None)
            session.metadata["slack_clock_location"] = ("company_management_remote" if user.worker_type == "admin" else "approved_remote") if remote else "worker_attested_onsite"
            session.metadata.pop("slack_clock_inactivity_warning_at", None)
            if len(detail.split(maxsplit=1)) == 2:
                session.latest_plan = detail.split(maxsplit=1)[1]
            return f"Clocked in at {now.astimezone(self.zone):%H:%M %Z}. Your hours are recording; no task selection is required. Tell me what you are doing when ready."
        if command == "out":
            self._finish_meal(session, now)
            if not running:
                session.stage = "clocked_out"
                return "Already clocked out. No time was removed or duplicated. " + FALLBACK
            if rest := session.metadata.pop("slack_clock_rest_started_at", None):
                session.metadata.setdefault("paid_rest_windows", []).append({"started_at": rest, "ended_at": now.isoformat(), "source": "worker_clock_out"})
            self._stop(session, now, "worker_clock_out")
            total = paid_seconds([session], now)
            return f"Clocked out at {now.astimezone(self.zone):%H:%M %Z}. This shift-day records {total / 3600:.2f} hours of work + paid rest. No summary is required to stop the clock. " + FALLBACK
        if command == "lunch":
            if session.stage == "on_lunch_break":
                return "Your reported lunch is already running. Use `back` when it actually ends."
            if not running and not self._meal_due(session, now):
                return "You are clocked out. Use `report hours` for a past break or correction."
            session.metadata["slack_clock_meal_started_at"] = now.isoformat()
            rest = session.metadata.pop("slack_clock_rest_started_at", None)
            if rest:
                session.metadata.setdefault("paid_rest_windows", []).append({"started_at": rest, "ended_at": now.isoformat(), "source": "worker_switched_to_meal"})
            session.metadata.setdefault("lunch_windows", []).append({"started_at": now.isoformat(), "ended_at": None, "source": "slack_worker_report"})
            session.stage = "on_lunch_break"
            return "Lunch recorded starting now. Stop all work and take at least 30 duty-free minutes. Reply `back` when you return; do not work during lunch."
        if command == "rest":
            if not running or session.stage == "on_lunch_break":
                return "Start paid rest from an active shift, not an unpaid meal. " + FALLBACK
            if session.metadata.get("slack_clock_rest_started_at"):
                return "Your paid rest is already running. Reply `back` when you return."
            session.metadata["slack_clock_rest_started_at"] = now.isoformat()
            return "Take 10 duty-free minutes of paid rest. No work or replies are needed during the break. Reply `back` when you return."
        if command == "back":
            meal_start = timestamp(session.metadata.get("slack_clock_meal_started_at"))
            if meal_start:
                remaining = 1800 - (now - meal_start).total_seconds()
                if remaining > 0:
                    return f"Your recorded meal has {int(remaining / 60) + 1} minutes remaining before work is authorized again. If you already worked, report the actual time; don't adjust it to look compliant."
                self._finish_meal(session, now)
                if over_limit:
                    self._stop(session, now, "hours_limit")
                    return "Meal ended; the hours limit prevents more authorized work. Contact Erik. " + FALLBACK
                if session.metadata.get("slack_clock_location") == "approved_remote" and not self._authorized(conn, user.user_key, "remote", now):
                    self._stop(session, now, "remote_approval_expired")
                    return "Meal ended; your remote authorization has expired. No further remote work is authorized. " + FALLBACK
                if session.clocked_out_at:
                    session.clocked_out_at = None
                    session.work_segments.append({"clocked_in_at": now.isoformat(), "clocked_out_at": None})
                session.metadata.pop("slack_clock_stop_reason", None)
                session.stage = "active"
                return "Meal ended now; your work clock is running again."
            if rest := session.metadata.pop("slack_clock_rest_started_at", None):
                session.metadata.setdefault("paid_rest_windows", []).append({"started_at": rest, "ended_at": now.isoformat(), "source": "worker_reported_return"})
                if session.clocked_out_at:
                    return "Your rest return is recorded, but the work clock stopped. Use `clock in onsite` to resume. " + FALLBACK
                return "Welcome back. Paid rest is recorded; your work clock kept running."
            return "No active break found. Use `hours` to check your clock or `report hours` to correct it."
        return HELP

    @staticmethod
    def _finish_meal(session: SessionState, now: datetime) -> None:
        if session.metadata.pop("slack_clock_meal_started_at", None):
            for meal in reversed(session.metadata.get("lunch_windows") or []):
                if not meal.get("ended_at"):
                    meal["ended_at"] = now.isoformat()
                    if now - timestamp(meal["started_at"]) >= timedelta(minutes=30):
                        session.metadata["slack_clock_meal_completed_at"] = now.isoformat()
                    else:
                        meal["paid_pending_review"] = True
                        session.metadata.setdefault("compliance_events", []).append({"event_type": "short_meal_reported", "recorded_at": now.isoformat()})
                    break

    @staticmethod
    def _stop(session: SessionState, now: datetime, reason: str) -> None:
        if not session.work_segments and session.clocked_in_at:
            session.work_segments = [{"clocked_in_at": session.clocked_in_at, "clocked_out_at": None}]
        for segment in session.work_segments:
            if not segment.get("clocked_out_at"):
                segment["clocked_out_at"] = now.isoformat()
        session.clocked_out_at = now.isoformat()
        session.stage = "clocked_out"
        session.metadata["slack_clock_stop_reason"] = reason
        if reason != "worker_clock_out":
            session.metadata.setdefault("compliance_events", []).append({"event_type": reason, "recorded_at": now.isoformat(), "confirmation": "stop_instruction_not_proof_of_stopped_work"})

    def tick(self, user: UserProfile, now: datetime) -> tuple[list[str], SessionState | None]:
        now = now.astimezone(timezone.utc)
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            sessions = self._sessions(conn, user.user_key)
            session = self._current(sessions, user, now)
            if not session.metadata.get("slack_clock_beta") or not session.clocked_in_at or session.clocked_out_at:
                return [], None
            if session.stage == "on_lunch_break":
                return [], None
            notices: list[str] = []
            daily, weekly = self.totals(sessions, now)
            worked = paid_seconds([session], now)
            reason = ""
            overtime_authorized = self._authorized(conn, user.user_key, "overtime", now)
            if user.worker_type != "admin" and not overtime_authorized and (daily >= self.daily_limit or weekly >= self.weekly_limit or self._seventh_day(sessions, now)):
                reason = "hours_limit"
            elif session.metadata.get("slack_clock_location") == "approved_remote" and not self._authorized(conn, user.user_key, "remote", now):
                reason = "remote_approval_expired"
            elif user.meal_tracking_required and self._meal_due(session, now):
                reason = "meal_due"
            elif (rest := timestamp(session.metadata.get("slack_clock_rest_started_at"))) and now - rest >= timedelta(minutes=10):
                reason = "rest_return_unconfirmed"
            if reason:
                self._stop(session, now, reason)
                notices.append(f"Stop work now: {reason.replace('_', ' ')}. Your clock stopped at {now.astimezone(self.zone):%H:%M %Z}; time through this notice remains recorded. " + ("Reply `lunch` when your meal actually begins. " if reason == "meal_due" else "") + FALLBACK)
            else:
                meal_warning_key = "slack_clock_meal_warning_at" if worked < 9.5 * 3600 else "slack_clock_second_meal_warning_at"
                expected = 1 if worked < 9.5 * 3600 else 2
                if user.meal_tracking_required and worked >= 4.5 * 3600 and self._meal_count(session, now) < expected and not session.metadata.get(meal_warning_key):
                    deadline = "five" if expected == 1 else "ten"
                    notices.append(f"Your meal is due before {deadline} hours of work. Plan to stop now and reply `lunch` when your duty-free meal actually begins.")
                    session.metadata[meal_warning_key] = now.isoformat()
                if user.worker_type != "admin" and min(self.daily_limit - daily, self.weekly_limit - weekly) <= 1800 and not session.metadata.get("slack_clock_hours_warning_at"):
                    notices.append("You are within 30 minutes of your daily or weekly hours limit. Wrap up and clock out; additional work needs Erik's authorization.")
                    session.metadata["slack_clock_hours_warning_at"] = now.isoformat()
                if worked >= 2 * 3600 and not session.metadata.get("slack_clock_rest_reminder_at") and not session.metadata.get("slack_clock_rest_started_at"):
                    notices.append("At a safe stopping point, take your 10-minute duty-free paid rest. Reply `break` as it begins.")
                    session.metadata["slack_clock_rest_reminder_at"] = now.isoformat()
                last = timestamp(session.last_user_message_at) or timestamp(session.clocked_in_at)
                if last and now - last >= timedelta(hours=4):
                    warning = timestamp(session.metadata.get("slack_clock_inactivity_warning_at"))
                    if not warning:
                        notices.append("Still working? It has been four hours since your last message. Send a quick update; otherwise I will stop the clock in 15 minutes. " + FALLBACK)
                        session.metadata["slack_clock_inactivity_warning_at"] = now.isoformat()
                    elif now - warning >= timedelta(minutes=15):
                        self._stop(session, now, "inactivity_unconfirmed")
                        notices.append("Stop work now: no response to the four-hour check. Your clock stopped now, not retroactively. " + FALLBACK)
            if notices:
                session.metadata["slack_clock_last_event_at"] = now.isoformat()
                self._save(conn, session)
                for message in notices:
                    conn.execute("INSERT INTO slack_clock_notices(user_key,session_date,text) VALUES (?,?,?)",
                                 (user.user_key, session.session_date, message))
                return notices, session
            return [], None

    def reports(self) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM slack_clock_reports WHERE status='pending' ORDER BY reported_at LIMIT 250")]

    def add_actual_hours(self, user: UserProfile, start: datetime, end: datetime,
                         *, actor_id: str, event_id: str, reason: str, now: datetime) -> str:
        if start.tzinfo is None or end.tzinfo is None or end <= start or end > now or end - start > timedelta(hours=24) or not reason.strip():
            raise ValueError("Supply actual past start/end times with UTC offsets, a positive interval no longer than 24 hours, and a reason.")
        key = f"manager-add:{actor_id}:{event_id}"
        day = start.astimezone(self.zone).date().isoformat()
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT response FROM slack_clock_receipts WHERE event_key=?", (key,)).fetchone()
            if previous:
                return previous[0]
            sessions = self._sessions(conn, user.user_key)
            session = next((s for s in sessions if s.session_date == day), SessionState(user_key=user.user_key, session_date=day))
            if not session.work_segments and session.clocked_in_at:
                session.work_segments.append({"clocked_in_at": session.clocked_in_at, "clocked_out_at": session.clocked_out_at})
            running = bool(session.clocked_in_at and not session.clocked_out_at)
            session.work_segments.append({"clocked_in_at": start.isoformat(), "clocked_out_at": end.isoformat()})
            session.clocked_in_at = min(timestamp(session.clocked_in_at) or start, start).isoformat()
            if not running:
                session.clocked_out_at = max(timestamp(session.clocked_out_at) or end, end).isoformat()
                session.stage = "clocked_out"
            session.metadata["slack_clock_beta"] = True
            session.metadata.setdefault("slack_clock_events", []).append({"action": "manager_added_actual_hours", "actor": actor_id, "event_id": event_id,
                                                                         "recorded_at": now.isoformat(), "start": start.isoformat(), "end": end.isoformat(), "reason": reason})
            self._save(conn, session)
            response = "Actual work interval recorded. Overlapping time is counted once; original records remain. Verify actual break records separately before payroll."
            conn.execute("INSERT INTO slack_clock_receipts VALUES (?,?,?,?)", (key, user.user_key, day, response))
            return response

    def resolve_report(self, report_id: str, *, actor_id: str, note: str, now: datetime) -> str:
        if not note.strip():
            raise ValueError("Give the reconciliation outcome and what was corrected.")
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM slack_clock_reports WHERE id=?", (report_id,)).fetchone()
            if not row:
                return "Hours report not found."
            # Keep the original report immutable; the resolution is a separate
            # append-only manager event in operational state for this report.
            conn.execute("INSERT OR IGNORE INTO operational_state(state_key,payload,updated_at) VALUES (?,?,?)",
                         ("clock_report_resolution:" + report_id, json.dumps({"actor": actor_id, "note": note, "at": now.isoformat()}), now.isoformat()))
            conn.execute("UPDATE slack_clock_reports SET status='resolved' WHERE id=?", (report_id,))
            return "Report marked reconciled; the original report remains. This resolution does not itself alter recorded hours."

    @staticmethod
    def _meal_count(session: SessionState, now: datetime) -> int:
        return sum(bool(window.get("ended_at")) and timestamp(window["ended_at"]) <= now and (timestamp(window["ended_at"]) - timestamp(window["started_at"])) >= timedelta(minutes=30)
                   for window in session.metadata.get("lunch_windows", []) if window.get("started_at"))

    def _meal_due(self, session: SessionState, now: datetime) -> bool:
        worked = paid_seconds([session], now)
        expected = 2 if worked >= 10 * 3600 else 1 if worked >= 5 * 3600 else 0
        return self._meal_count(session, now) < expected

    def pending_notices(self, user_key: str) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM slack_clock_notices WHERE user_key=? AND delivered_at IS NULL ORDER BY id", (user_key,))]

    def notice_delivered(self, notice_id: int, now: datetime) -> None:
        with self.store._connect() as conn:
            conn.execute("UPDATE slack_clock_notices SET delivered_at=? WHERE id=?", (now.isoformat(), notice_id))

    @staticmethod
    def _authorized(conn: Any, user_key: str, kind: str, now: datetime) -> bool:
        return any(timestamp(row["starts_at"]) <= now < timestamp(row["ends_at"]) for row in conn.execute(
            "SELECT starts_at,ends_at FROM slack_clock_approvals WHERE user_key=? AND kind=?", (user_key, kind)
        ))

    def authorize(self, user_key: str, kind: str, start: datetime, end: datetime,
                  *, approver: str, reason: str, now: datetime) -> None:
        if kind not in {"remote", "overtime"} or not reason.strip():
            raise ValueError("Choose remote or overtime and give an approval reason.")
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("Approval times must include a UTC offset.")
        if start - now < timedelta(hours=24) or end <= start or end - start > timedelta(days=7):
            raise ValueError("Approve at least 24 hours ahead, with an end after the start and a window of at most seven days.")
        with self.store._connect() as conn:
            conn.execute("INSERT INTO slack_clock_approvals(user_key,kind,starts_at,ends_at,approved_at,approver,reason) VALUES (?,?,?,?,?,?,?)",
                         (user_key, kind, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat(), now.isoformat(), approver, reason))

    def _seventh_day(self, sessions: list[SessionState], now: datetime) -> bool:
        day = now.astimezone(self.zone).replace(hour=0, minute=0, second=0, microsecond=0)
        # California seventh consecutive day in this configured Monday workweek.
        if day.weekday() != 6:
            return False
        return all(paid_seconds(sessions, now, day - timedelta(days=d), day - timedelta(days=d-1)) > 0 for d in range(1, 7))

    def record_activity(self, user: UserProfile, text: str, now: datetime) -> SessionState | None:
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            sessions = self._sessions(conn, user.user_key)
            session = self._current(sessions, user, now)
            prior = timestamp(session.last_user_message_at)
            if session.metadata.get("slack_clock_beta") and not session.clocked_out_at and (not prior or now > prior):
                session.last_user_message_at = now.isoformat()
                session.latest_status = text[:6000]
                session.metadata.pop("slack_clock_inactivity_warning_at", None)
                self._save(conn, session)
                return session
            return None

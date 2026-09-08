"""Explicit, actor-bound meal correction previews. No model-generated punches."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from .models import SessionState
from .slack_timekeeping import SlackTimekeeping, paid_seconds, timestamp, _union


USAGE = ("To correct a completed lunch, send `fix lunch today 11:30am-12:15pm` "
         "using your actual times, or replace today with YYYY-MM-DD. Times use the shop timezone. "
         "I will show a preview before changing hours. For an interrupted/missed meal or uncertain times, "
         "use `report hours` with what actually happened. Nothing has changed yet.")


def parse_interval(text, now, zone):
    match = re.fullmatch(r"fix lunch\s+(today|\d{4}-\d{2}-\d{2})\s+"
                         r"(\d{1,2}(?::\d{2})?\s*[ap]m)\s*(?:-|–|—|to)\s*"
                         r"(\d{1,2}(?::\d{2})?\s*[ap]m)", text.strip(), re.I)
    if not match:
        raise ValueError(USAGE)
    day = now.astimezone(zone).date().isoformat() if match[1].lower() == "today" else match[1]
    result = []
    for raw in match.groups()[1:]:
        raw = re.sub(r"\s", "", raw).upper()
        fmt = "%Y-%m-%d %I:%M%p" if ":" in raw else "%Y-%m-%d %I%p"
        local = datetime.strptime(day + " " + raw, fmt).replace(tzinfo=zone)
        # Ambiguous/nonexistent DST wall times need explicit manager review.
        if local.utcoffset() != local.replace(fold=1).utcoffset() or local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            raise ValueError("That local time crosses a daylight-saving ambiguity. Use report hours for review.")
        result.append(local.astimezone(timezone.utc))
    return tuple(result)


def clock_basis(session):
    """Ignore background reminders, but invalidate on any relevant clock edit."""
    meta = session.metadata
    return {"in": session.clocked_in_at, "out": session.clocked_out_at,
            "segments": session.work_segments, "stage": session.stage,
            "metadata": {k: meta.get(k) for k in (
                "lunch_windows", "lunch_started_at", "lunch_ended_at", "paid_rest_windows",
                "slack_clock_meal_started_at", "slack_clock_rest_started_at",
                "slack_clock_legacy_unresolved", "retro_hours_backfill")}}


def fingerprint(session):
    return hashlib.sha256(json.dumps(clock_basis(session), sort_keys=True).encode()).hexdigest()


class MealCorrections:
    def __init__(self, clock):
        self.clock = clock
        self.store = clock.store
        with self.store._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS meal_correction_previews (
                token TEXT PRIMARY KEY, user_key TEXT NOT NULL, session_date TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', result TEXT)""")

    def _validate(self, session, start, end, now):
        if not session.clocked_in_at or not session.metadata.get("slack_clock_beta") or session.metadata.get("slack_clock_legacy_unresolved"):
            raise ValueError("No reconciled DP shift exists for that date. Use report hours; Gusto history is not imported here.")
        if end <= start or end > now or now - start > timedelta(days=7):
            raise ValueError("Use a completed lunch within the last seven days, with end after start. Otherwise use report hours.")
        meta = session.metadata
        if any(meta.get(k) for k in ("slack_clock_meal_started_at", "slack_clock_rest_started_at", "lunch_started_at", "retro_hours_backfill")):
            raise ValueError("An active break or legacy correction needs review first. Use report hours; no time changed.")
        windows = meta.get("lunch_windows") or []
        if len(windows) > 1 or any(not w.get("ended_at") for w in windows):
            raise ValueError("Multiple or unfinished lunches need manager review. No existing lunch was replaced.")
        segments = session.work_segments or [{"clocked_in_at": session.clocked_in_at, "clocked_out_at": session.clocked_out_at}]
        bounds = _union([(timestamp(s["clocked_in_at"]), timestamp(s.get("clocked_out_at")) or now) for s in segments if s.get("clocked_in_at")])
        if not any(a <= start < end <= b for a, b in bounds):
            raise ValueError("That lunch crosses time outside the recorded shift. Report the actual work/gap for review first.")
        for rest in meta.get("paid_rest_windows") or []:
            a, b = timestamp(rest.get("started_at")), timestamp(rest.get("ended_at"))
            if a and b and max(a, start) < min(b, end):
                raise ValueError("That lunch overlaps recorded paid rest. Use report hours so paid rest is not silently deducted.")

    def preview(self, user_key, start, end, *, now, source_report_id=None):
        day = start.astimezone(self.clock.zone).date().isoformat()
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM sessions WHERE user_key=? AND session_date=?", (user_key, day)).fetchone()
            if not row:
                raise ValueError("No DP shift exists for that date. Use report hours for the actual-time record.")
            session = SessionState(**json.loads(row[0]))
            self._validate(session, start, end, now)
            if source_report_id:
                report = conn.execute("SELECT user_key,status FROM slack_clock_reports WHERE id=?", (source_report_id,)).fetchone()
                if not report or report[0] != user_key or report[1] != "pending":
                    raise ValueError("The source report is not pending for this worker.")
            token = secrets.token_hex(4)
            payload = {"start": start.isoformat(), "end": end.isoformat(), "basis": fingerprint(session), "source_report_id": source_report_id}
            conn.execute("UPDATE meal_correction_previews SET status='superseded' WHERE user_key=? AND status='pending'", (user_key,))
            conn.execute("INSERT INTO meal_correction_previews(token,user_key,session_date,created_at,expires_at,payload) VALUES (?,?,?,?,?,?)",
                         (token, user_key, day, now.isoformat(), (now + timedelta(minutes=30)).isoformat(), json.dumps(payload)))
            old = session.metadata.get("lunch_windows") or []
            before = paid_seconds([session], now)
            self._set_meal(session, start, end, token)
            after = paid_seconds([session], now)
            description = ("Replace the existing lunch" if old else "Add the missing lunch")
            short = end - start < timedelta(minutes=30)
            return (f"*Lunch correction preview — {day}*\n{description} with {start.astimezone(self.clock.zone):%I:%M %p}–{end.astimezone(self.clock.zone):%I:%M %p} ({self.clock.zone.key}), "
                    f"{(end-start).total_seconds()/60:g} minutes.\n"
                    f"Recorded shift-day work + paid rest: {before/3600:.2f} → {after/3600:.2f} h as of {now.astimezone(self.clock.zone):%H:%M}.\n"
                    + ("Short meal: time remains paid pending review. " if short else "Confirm only if this was an off-duty meal with no work. ")
                    + f"Reply `confirm lunch {token}` to apply, `cancel lunch` to cancel, or send corrected times. Preview expires in 30 minutes. Nothing has changed yet."), token

    @staticmethod
    def _set_meal(session, start, end, token):
        window = {"started_at": start.isoformat(), "ended_at": end.isoformat(), "source": "confirmed_slack_meal_correction", "correction_id": token}
        if end - start < timedelta(minutes=30):
            window["paid_pending_review"] = True
        session.metadata["lunch_windows"] = [window]
        if not window.get("paid_pending_review"):
            session.metadata["slack_clock_meal_completed_at"] = end.isoformat()
        else:
            session.metadata.pop("slack_clock_meal_completed_at", None)

    def confirm(self, user_key, token, *, now):
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM meal_correction_previews WHERE token=? AND user_key=?", (token, user_key)).fetchone()
            if not row:
                raise ValueError("No lunch preview with that code belongs to you. " + USAGE)
            if row["status"] == "applied":
                return row["result"] + " Already applied; no duplicate deduction.", None
            if row["status"] != "pending" or timestamp(row["expires_at"]) < now:
                raise ValueError("That preview expired, was cancelled or was superseded. Send the actual lunch times again for a fresh preview.")
            data = json.loads(row["payload"])
            if report_id := data.get("source_report_id"):
                report = conn.execute("SELECT status FROM slack_clock_reports WHERE id=? AND user_key=?", (report_id, user_key)).fetchone()
                if not report or report[0] != "pending":
                    raise ValueError("The linked report has already been reconciled. Request a fresh preview before another change.")
            current = conn.execute("SELECT payload FROM sessions WHERE user_key=? AND session_date=?", (user_key, row["session_date"])).fetchone()
            if not current:
                raise ValueError("The shift changed; request a fresh preview.")
            session = SessionState(**json.loads(current[0]))
            if fingerprint(session) != data["basis"]:
                raise ValueError("Your clock or breaks changed since that preview. Request a fresh preview; no correction applied.")
            start, end = timestamp(data["start"]), timestamp(data["end"])
            self._validate(session, start, end, now)
            before = json.loads(json.dumps(clock_basis(session)))
            self._set_meal(session, start, end, token)
            session.metadata.setdefault("meal_correction_audit", []).append({"id": token, "actor": user_key, "confirmed_at": now.isoformat(), "before": before, "source_report_id": data.get("source_report_id")})
            if end - start < timedelta(minutes=30):
                session.metadata.setdefault("compliance_events", []).append({"event_type": "short_meal_reported", "recorded_at": now.isoformat(), "correction_id": token})
            SlackTimekeeping._save(conn, session)
            if report_id := data.get("source_report_id"):
                conn.execute("UPDATE slack_clock_reports SET status='resolved' WHERE id=? AND user_key=?", (report_id, user_key))
            result = f"Lunch correction applied for {row['session_date']}: {start.astimezone(self.clock.zone):%I:%M %p}–{end.astimezone(self.clock.zone):%I:%M %p} ({self.clock.zone.key}). "
            result += ("Short meal remains paid pending review. " if end-start < timedelta(minutes=30) else "The off-duty meal is deducted once. ")
            result += f"Recorded shift-day work + paid rest: {paid_seconds([session], now)/3600:.2f} h as of {now.astimezone(self.clock.zone):%H:%M}. Original record preserved. Your start/stop state is unchanged."
            conn.execute("UPDATE meal_correction_previews SET status='applied',result=? WHERE token=?", (result, token))
            return result, session

    def cancel(self, user_key):
        with self.store._connect() as conn:
            conn.execute("UPDATE meal_correction_previews SET status='cancelled' WHERE user_key=? AND status='pending'", (user_key,))
        return "Pending lunch preview cancelled. No hours changed."

"""Live, private payroll review. Drafts never mutate attendance or Gusto."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import SessionState
from .slack_timekeeping import paid_seconds, timestamp
from .payroll_day_review import screen

ZONE = ZoneInfo("America/Los_Angeles")


def observed_source(runtime, end):
    path = runtime._storage_root_path() / "dashboard" / "payroll" / end.isoformat() / "gusto-observed.json"
    if not path.exists():
        return {}
    source = json.loads(path.read_text())
    if source.get("week_ending") != end.isoformat():
        raise ValueError("Gusto snapshot period mismatch")
    for worker in source.get("workers", {}).values():
        days = worker.get("days", {})
        expected = {(end - timedelta(days=n)).isoformat() for n in range(7)}
        if set(days) != expected or any(type(v.get("minutes")) not in (float, int) or not 0 <= v["minutes"] <= 1440 for v in days.values()):
            raise ValueError("Gusto snapshot has incomplete or invalid daily coverage")
        if sum(v["minutes"] for v in days.values()) != worker.get("week_minutes"):
            raise ValueError("Gusto daily totals do not reconcile with the observed week")
    return source


def choices(day, gusto, case):
    if case:
        return case
    dp = day["seconds"] / 3600
    gh = gusto["minutes"] / 60 if gusto else None
    options = []
    if dp and gh:
        options = [{"id":"gusto_once", "label":"A — Gusto covers the whole day; use it once", "hours":gh}, {"id":"dp_once", "label":"B — DP covers the whole day; use it once", "hours":dp}]
    elif day["issues"]:
        options = [{"id":"recorded", "label":"A — Recorded hours stand; any time gap was off duty (confirm)", "hours":dp or gh}, {"id":"custom", "label":"B — I know a correction; enter actual time and evidence"}]
    elif dp or gh:
        options = [{"id":"recorded", "label":"A — Use this day's recorded hours (recommended)", "hours":dp or gh}, {"id":"custom", "label":"B — I know additional or different actual time"}]
    else:
        options = [{"id":"no_work", "label":"A — I confirm no work that day", "hours":0}, {"id":"custom", "label":"B — There was work; enter known hours and evidence"}]
    if dp and gh:
        options.append({"id":"custom", "label":"C — I know the overlap or different actual time; enter it"})
    options.append({"id":"ask", "label":("D" if dp and gh else "C") + " — Request one focused worker clarification only if needed"})
    return {"title":"Choose the day's reconciliation", "evidence":"Selecting a choice prepares a draft only. Missing break entries do not reduce pay or prove a missed break. Confirm actual gaps before settling hours.", "options":options}


def period(value: str, now: datetime) -> tuple[date, date]:
    today = now.astimezone(ZONE).date()
    end = date.fromisoformat(value) if value else today - timedelta(days=today.weekday() + 1)
    if end.weekday() != 6 or end >= today:
        raise ValueError("Choose a completed Sunday; current-day time stays live and outside this review.")
    return end - timedelta(days=6), end


@contextmanager
def _connect(runtime):
    path = runtime._storage_root_path() / "dashboard" / "payroll" / "reconciliation.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS drafts (id INTEGER PRIMARY KEY, week TEXT, user_key TEXT, day TEXT, fingerprint TEXT, body TEXT, saved_at TEXT)")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def build(runtime, week: str = "", now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    start, end = period(week, now)
    source = observed_source(runtime, end)
    week_start = datetime.combine(start, time(), ZONE)
    week_end = datetime.combine(end + timedelta(days=1), time(), ZONE)
    with runtime.state_store._connect() as conn:
        raw = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM sessions ORDER BY user_key,session_date")]
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", ("slack_clock_reports",)).fetchone()
        reports = [dict(r) for r in conn.execute("SELECT id,user_key,reported_at,text,status FROM slack_clock_reports WHERE status=?", ("pending",))] if exists else []
    grouped = {}
    for payload in raw:
        # All ledger identities are considered, including no-longer-active workers.
        grouped.setdefault(payload["user_key"], []).append(payload)
    cohort = getattr(runtime.config.slack, "work_intake_beta_slack_user_ids", [])
    keys = set(grouped) | {r["user_key"] for r in reports} | {u.user_key for u in runtime.roster_by_key.values() if u.slack_user_id in cohort}
    with _connect(runtime) as conn:
        saved = {(r["user_key"], r["day"]): dict(r) for r in conn.execute("SELECT * FROM drafts WHERE week=? ORDER BY id", (end.isoformat(),))}
    workers = []
    for key in sorted(keys):
        gusto_worker = source.get("workers", {}).get(key)
        user = runtime.roster_by_key.get(key)
        if user is None and hasattr(runtime, "resolve_user_profile"):
            user = runtime.resolve_user_profile(key)
        payloads = grouped.get(key, [])
        relevant = []
        for p in payloads:
            segments = p.get("work_segments") or [{"clocked_in_at": p.get("clocked_in_at"), "clocked_out_at": p.get("clocked_out_at")}]
            intersects = any(timestamp(s.get("clocked_in_at")) and timestamp(s["clocked_in_at"]) < week_end and (timestamp(s.get("clocked_out_at")) or week_end) > week_start and (s.get("clocked_out_at") or timestamp(s["clocked_in_at"]) >= week_start) for s in segments)
            if start.isoformat() <= p["session_date"] <= end.isoformat() or intersects:
                relevant.append(p)
        worker_reports = [r for r in reports if r["user_key"] == key]
        if not relevant and not worker_reports and not (user and user.slack_user_id in cohort):
            continue
        days = []
        for offset in range(7):
            day = start + timedelta(days=offset)
            lo = datetime.combine(day, time(), ZONE)
            hi = lo + timedelta(days=1)
            sessions, evidence, issues, clock_events = [], [], [], []
            for p in relevant:
                s = SessionState(**p)
                segs = s.work_segments or [{"clocked_in_at": s.clocked_in_at, "clocked_out_at": s.clocked_out_at}]
                closed = []
                for seg in segs:
                    a, b = timestamp(seg.get("clocked_in_at")), timestamp(seg.get("clocked_out_at"))
                    if a and a < hi and (b or hi) > lo:
                        evidence.append({"kind": "Work", "start": a.isoformat(), "end": b.isoformat() if b else None, "session_date": s.session_date})
                        if b:
                            closed.append(seg)
                        else:
                            issues.append("Open interval: actual end needed; unknown tail excluded from recorded hours")
                if closed:
                    s.work_segments = closed
                    sessions.append(s)
                if any(e["session_date"] == s.session_date for e in evidence):
                    for event in s.metadata.get("compliance_events") or []:
                        at = timestamp(event.get("recorded_at"))
                        kind = event.get("event_type")
                        corrected_end = timestamp(s.clocked_out_at) if s.metadata.get("slack_clock_stop_reason") == "owner_confirmed_actual_clock_out" else None
                        if at and lo <= at < hi and (not corrected_end or at <= corrected_end) and kind in {"meal_due", "inactivity_unconfirmed", "hours_limit", "rest_return_unconfirmed"}:
                            issues.append("Earlier automatic stop: " + str(kind).replace("_", " ") + "; inspect any gap before restart")
                    for event in s.metadata.get("slack_clock_events") or []:
                        at = timestamp(event.get("at"))
                        if at and lo <= at < hi:
                            clock_events.append({"action":event.get("action"), "at":event["at"], "detail":event.get("detail", "")})
                    reason = s.metadata.get("slack_clock_stop_reason")
                    if reason and reason not in {"manual", "worker_clock_out", "owner_confirmed_end", "owner_confirmed_actual_clock_out"}:
                        issues.append("Clock stop: " + str(reason).replace("_", " ") + "; confirm actual end")
                    for field, label in (("lunch_windows", "Lunch"), ("paid_rest_windows", "Paid rest")):
                        for w in s.metadata.get(field) or []:
                            a, b = timestamp(w.get("started_at")), timestamp(w.get("ended_at"))
                            if a and a < hi and (b or hi) > lo:
                                evidence.append({"kind": label + (" — paid pending review" if w.get("paid_pending_review") else ""), "start": a.isoformat(), "end": b.isoformat() if b else None, "session_date": s.session_date})
                                if not b or w.get("paid_pending_review"):
                                    issues.append(label + " needs timing review")
                                if label == "Lunch" and b and (b-a).total_seconds() > 5400:
                                    issues.append("Long recorded unpaid lunch; verify return time")
                    if s.metadata.get("slack_clock_legacy_unresolved"):
                        issues.append("Preserved unresolved historical shift")
            seconds = paid_seconds(sessions, now, lo, hi)
            if seconds >= 5 * 3600 and user and user.meal_tracking_required and not any(e["kind"].startswith("Lunch") for e in evidence):
                issues.append("No lunch record for this day; check actual meal timing")
            if seconds >= 3.5 * 3600 and user and user.worker_type != "admin" and not any(e["kind"].startswith("Paid rest") for e in evidence):
                issues.append("No paid-rest record; check whether a break entry is missing")
            if seconds > 8 * 3600 and not (user and user.worker_type == "admin"):
                issues.append("Over 8 recorded hours; review applicable overtime classification")
            gusto_day = gusto_worker.get("days", {}).get(day.isoformat()) if gusto_worker else None
            case = source.get("cases", {}).get(key + "/" + day.isoformat())
            if gusto_day and gusto_day["minutes"] and seconds:
                issues.append("Both Gusto and DP contain time: resolve overlap before combining")
            fingerprint = hashlib.sha256(json.dumps({"sources": relevant, "reports": worker_reports, "day": day.isoformat(), "gusto":gusto_worker, "case":case}, sort_keys=True).encode()).hexdigest()
            draft = saved.get((key, day.isoformat()))
            current = draft and draft["fingerprint"] == fingerprint
            days.append({"day": day.isoformat(), "seconds": seconds, "hours": round(seconds / 3600, 4), "evidence": evidence, "issues": sorted(set(issues)), "fingerprint": fingerprint,
                         "draft": json.loads(draft["body"]) if draft else None, "draft_current": bool(current), "saved_at": draft["saved_at"] if draft else None,
                         "gusto":gusto_day, "clock_events":clock_events})
            days[-1]["case"] = choices(days[-1], gusto_day, case)
            days[-1]["screen"] = screen(days[-1], user, ZONE)
        total = sum(d["seconds"] for d in days)
        workers.append({"user_key": key, "name": user.display_name if user else key, "compensation": user.compensation_plan if user else "needs_review", "mapped": bool(user and user.gusto_entity_uuid), "seconds": total, "hours": round(total / 3600, 4), "days": days, "reports": worker_reports, "gusto":gusto_worker,
                        "issues": (["Weekly recorded hours exceed 40; review classification"] if total > 144000 and not (user and user.worker_type == "admin") else []) + (["No DP hours recorded: check earlier Gusto/cutover records"] if not total else [])})
        week_case = source.get("worker_cases", {}).get(key)
        if week_case:
            fingerprint = hashlib.sha256(json.dumps({"days":[d["fingerprint"] for d in days],"case":week_case},sort_keys=True).encode()).hexdigest()
            draft = saved.get((key,"week"))
            workers[-1]["week_review"] = {"day":"week","seconds":0,"hours":0,"gusto":None,"evidence":[],"issues":[],"clock_events":[],"case":week_case,"fingerprint":fingerprint,"draft":json.loads(draft["body"]) if draft else None,"draft_current":bool(draft and draft["fingerprint"]==fingerprint)}
    return {"week_start": start.isoformat(), "week_ending": end.isoformat(), "timezone": ZONE.key, "refreshed_at": now.isoformat(), "workers": workers, "gusto_snapshot":source, "gusto_status": ("Gusto observed " + str(source.get("observed_on")) + ". Six mapped time-tracking records; absent workers remain unknown. Displayed-minute precision; not a live sync." if source else "Gusto source hours have not been loaded into this view. Blank does not mean zero hours; enter verified Gusto figures when reconciling."), "source": "Live DP SQLite sessions and pending actual-hours reports. Closed work intervals are unioned within each Pacific calendar day; overlapping recorded unpaid meals are deducted; paid rests remain included. Open tails are excluded, not assumed to be zero actual work. Draft reconciliation never changes DP or Gusto."}


def save(runtime, payload: dict) -> dict:
    data = build(runtime, str(payload.get("week") or ""))
    worker = next((w for w in data["workers"] if w["user_key"] == payload.get("user_key")), None)
    row = next((d for d in worker["days"] if d["day"] == payload.get("day")), None) if worker else None
    if worker and payload.get("day") == "week":
        row = worker.get("week_review")
    if not row or row["fingerprint"] != payload.get("fingerprint"):
        raise ValueError("Source time changed or row is unavailable. Refresh and review before saving.")
    body = {}
    for field in ("gusto_hours", "target_hours"):
        value = payload.get(field)
        value = None if value in (None, "") else float(value)
        if row["day"] == "week" and value is not None:
            raise ValueError("Weekly coverage notes cannot allocate hours; use actual dated rows")
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 24):
            raise ValueError("Daily hours must be between 0 and 24; leave unknown values blank.")
        body[field] = value
    body["note"] = str(payload.get("note") or "").strip()[:4000]
    choice = str(payload.get("choice") or "")
    if choice and choice not in {c["id"] for c in row["case"]["options"]}:
        raise ValueError("Reconciliation choice changed; refresh the source")
    body["choice"] = choice
    for field in ("rest_review", "meal_review"):
        value = str(payload.get(field) or "")
        if value not in {"", "recorded", "correction", "missed", "unsure"}:
            raise ValueError("Choose a valid break review option")
        body[field] = value
    if not body["note"]:
        raise ValueError("Add the source/evidence and what you resolved.")
    body["status"] = "draft"  # This is not approval, a punch correction, or a payroll transaction.
    body["break_notes"] = str(payload.get("break_notes") or "").strip()[:4000]
    if "correction" in (body["rest_review"], body["meal_review"]) and not body["break_notes"]:
        raise ValueError("Add the actual break times or witness context for the correction")
    with _connect(runtime) as conn:
        conn.execute("INSERT INTO drafts(week,user_key,day,fingerprint,body,saved_at) VALUES(?,?,?,?,?,?)", (data["week_ending"], worker["user_key"], row["day"], row["fingerprint"], json.dumps(body), datetime.now(timezone.utc).isoformat()))
    return {"saved": True, "message": "Reconciliation draft saved. DP punches and Gusto are unchanged."}


def render(data: dict) -> str:
    template = Path(__file__).with_name("payroll_reconciliation_ui.html").read_text()
    encoded = json.dumps(data).replace("<", "\\u003c")
    return template.replace("__REVIEW_DATA__", encoded, 1)

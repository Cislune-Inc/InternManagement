"""Read-only break guidance, not a payroll classification or compliance verdict."""
from datetime import timedelta


def completed_rests(session):
    from .slack_timekeeping import timestamp
    return [r for r in session.metadata.get("paid_rest_windows", [])
            if r.get("kind") != "extra" and timestamp(r.get("started_at"))
            and timestamp(r.get("ended_at"))
            and timestamp(r["ended_at"]) - timestamp(r["started_at"]) >= timedelta(minutes=10)]


def next_rest_hours(session):
    # Planning targets near the middle of successive work periods. These do
    # not certify earlier timing, or infer a worker's eventual shift length.
    return 2 + 4 * len(completed_rests(session))


def summary(session, user, now, zone, *, meal_due=False):
    from .slack_timekeeping import timestamp, paid_seconds
    if not session.clocked_in_at:
        return ""
    full = completed_rests(session)
    all_rests = session.metadata.get("paid_rest_windows", [])
    parts = []
    if user.worker_type == "admin":
        parts.append("Owner practice guidance; not a payroll classification.")
    if all_rests:
        latest = max(all_rests, key=lambda r: r.get("ended_at") or "")
        start, end = timestamp(latest.get("started_at")), timestamp(latest.get("ended_at"))
        if start and end:
            kind = "Extra paid pause" if latest.get("kind") == "extra" else "Paid rest"
            parts.append(f"Last break: {kind}, {start.astimezone(zone):%H:%M}–{end.astimezone(zone):%H:%M} ({(end-start).total_seconds()/60:.0f} min).")
    parts.append(f"Completed 10-minute rests: {len(full)} recorded today.")
    meal_starts = [timestamp(m.get("started_at")) for m in session.metadata.get("lunch_windows", []) if m.get("started_at")]
    if meal_starts and not any(timestamp(r["ended_at"]) <= min(meal_starts) for r in full):
        parts.append("Rest timing: no full rest recorded before the first lunch.")
    if session.metadata.get("slack_clock_meal_started_at"):
        parts.append("Now: unpaid duty-free lunch; return after the minimum when your meal actually ends.")
    elif session.metadata.get("slack_clock_rest_started_at"):
        if session.metadata.get("slack_clock_rest_kind") == "extra":
            parts.append("Now: extra paid pause. Reply `back` whenever ready; no minimum countdown.")
        else:
            parts.append("Now: 10-minute paid rest. Reply `back` after the minimum when you return.")
    elif session.clocked_out_at:
        parts.append("Now: clocked out.")
    else:
        remaining = next_rest_hours(session) * 3600 - paid_seconds([session], now)
        if user.meal_tracking_required and meal_due:
            parts.append("Next: lunch is due now. Reply `lunch` as your 30-minute duty-free meal begins.")
        elif remaining <= 0:
            parts.append("Next: take a 10-minute paid rest at a safe stopping point; reply `break`.")
        else:
            parts.append(f"Next rest planning target: after about {remaining/3600:.1f} more hours of work, if continuing. No rest due now.")
        if user.meal_tracking_required and not meal_starts:
            parts.append("Lunch: start before five hours of work; reply `lunch` as it begins.")
    return "\n" + "\n".join(parts)

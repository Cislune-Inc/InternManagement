"""Own-person read-only DP clock projection; no payroll or attendance mutations."""
from contextlib import closing
from dataclasses import fields
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .models import SessionState
from .planning_adapter import person_ref
from .slack_timekeeping import paid_seconds


def read_person_time(path, *, user_key, timezone_name, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Use a timezone-aware observation time.')
    zone = ZoneInfo(timezone_name)
    day = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    # Never construct StateStore or SlackTimekeeping: constructors migrate tables.
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        rows = db.execute('SELECT payload FROM sessions WHERE user_key=? ORDER BY session_date LIMIT 5001', (user_key,)).fetchall()
        if len(rows)>5000:
            raise ValueError('Clock history exceeds the bounded reader; review coverage.')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        pending = db.execute("SELECT COUNT(*) FROM slack_clock_reports WHERE user_key=? AND status='pending'", (user_key,)).fetchone()[0] if 'slack_clock_reports' in tables else 0
    keys = {f.name for f in fields(SessionState)}
    sessions = [SessionState(**{k:v for k,v in json.loads(row[0]).items() if k in keys}) for row in rows]
    if any(s.user_key != user_key for s in sessions):
        raise ValueError('Ledger identity mismatch.')
    active = [s for s in sessions if not s.metadata.get('slack_clock_legacy_unresolved') and s.clocked_in_at
              and (not s.clocked_out_at or s.metadata.get('slack_clock_meal_started_at') or s.metadata.get('slack_clock_rest_started_at'))]
    unresolved = []
    if pending:
        unresolved.append(f'{pending} actual-hours report(s) pending review')
    if any(s.metadata.get('slack_clock_legacy_unresolved') for s in sessions):
        unresolved.append('Historical time remains unresolved; unknown legacy tails excluded')
    if len(active)>1:
        state='Needs reconciliation'
        unresolved.append('Multiple open shifts; current clock state is uncertain')
    else:
        current = active[0] if active else next((s for s in sessions if s.session_date==day.date().isoformat()),None)
        state = ('On lunch' if current and current.metadata.get('slack_clock_meal_started_at') else
                 'On paid rest' if current and current.metadata.get('slack_clock_rest_started_at') and not current.clocked_out_at else
                 'Clocked in' if current and current.clocked_in_at and not current.clocked_out_at else 'Clocked out')
    return dict(clock_state=state,recorded_seconds_today=paid_seconds(sessions,now,day),
                as_of=now.isoformat(),source='Don Pollo ledger · recorded work and paid rest; excludes Gusto',
                unresolved=unresolved)


def dp_time_reader(runtime, *, workspace):
    """Resolve the verified Slack subject to today's active ledger roster each call."""
    async def read(principal):
        import asyncio
        prefix=f'slack:{workspace}:'
        if not principal.person_ref.startswith(prefix):
            return None
        sid=principal.person_ref[len(prefix):]
        user=runtime.roster_by_slack_id.get(sid)
        if not user or not user.active or person_ref(workspace,user.slack_user_id)!=principal.person_ref:
            return None  # An administrator without a ledger identity has no guessed hours.
        return await asyncio.to_thread(read_person_time,runtime.state_store.db_path,
                                       user_key=user.user_key,timezone_name=runtime.config.timezone)
    return read

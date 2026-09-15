"""Preserve explicitly selected old open shifts for review, without inventing ends."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path


def defer(db: Path, user_key: str, before: date, actor_id: str, *, apply: bool = False) -> int:
    if not db.is_file() or not user_key or not actor_id:
        raise ValueError("Existing canonical database, exact worker and approving actor are required.")
    with sqlite3.connect(db.resolve().as_uri() + ("?mode=rw" if apply else "?mode=ro"), uri=True) as conn:
        if apply:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("PRAGMA query_only=ON")
        selected = []
        for day, raw in conn.execute("SELECT session_date,payload FROM sessions WHERE user_key=?", (user_key,)):
            state = json.loads(raw)
            meta = state.get("metadata") or {}
            if date.fromisoformat(day) >= before or not state.get("clocked_in_at") or state.get("clocked_out_at") or meta.get("slack_clock_legacy_unresolved"):
                continue
            if meta.get("slack_clock_beta"):
                raise ValueError("This operation is only for historical pre-beta shifts; reconcile active beta records directly.")
            selected.append((day, raw, state))
        if not apply:
            return len(selected)
        conn.execute("CREATE TABLE IF NOT EXISTS legacy_shift_deferrals (user_key TEXT,session_date TEXT,original_payload TEXT,actor_id TEXT,recorded_at TEXT,PRIMARY KEY(user_key,session_date))")
        conn.execute("CREATE TABLE IF NOT EXISTS slack_clock_reports (id TEXT PRIMARY KEY,user_key TEXT NOT NULL,reported_at TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending')")
        now = datetime.now(timezone.utc).isoformat()
        for day, raw, state in selected:
            conn.execute("INSERT INTO legacy_shift_deferrals VALUES (?,?,?,?,?)", (user_key, day, raw, actor_id, now))
            state.setdefault("metadata", {})["slack_clock_legacy_unresolved"] = {
                "recorded_at": now, "approved_by": actor_id,
                "reason": "Owner-approved cutover: historical end unknown; preserve actual-time reconciliation, do not infer continuous work."}
            conn.execute("UPDATE sessions SET payload=? WHERE user_key=? AND session_date=?", (json.dumps(state), user_key, day))
            conn.execute("INSERT OR IGNORE INTO slack_clock_reports(id,user_key,reported_at,text) VALUES (?,?,?,?)",
                         (f"legacy-unresolved:{user_key}:{day}", user_key, now,
                          f"Historical {day} shift starts at {state['clocked_in_at']} but has no confirmed end. Original record is preserved. Owner approved starting a fresh clock, not an end-time guess or a waiver of worked hours. Reconcile actual time before settling this record."))
        return len(selected)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-db", type=Path, required=True)
    p.add_argument("--user-key")
    p.add_argument("--before-date", type=date.fromisoformat, required=True)
    p.add_argument("--actor-id")
    p.add_argument("--primary-admin", action="store_true", help="Use the already verified primary admin in local config.")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    if a.primary_admin:
        if a.user_key or a.actor_id:
            p.error("Choose the explicit worker/actor or primary-admin mode, not both.")
        from agent.config import parse_agent_config, parse_roster_bytes
        from agent.worker_portal import _actor_user_key
        c = parse_agent_config(json.loads(Path("config/agent.config.json").read_text()), "America/Los_Angeles")
        admin = next(x for x in c.admins if x.discord_user_id == c.admin_discord_user_id)
        roster = parse_roster_bytes(c.roster_file_name, (Path("config") / c.roster_file_name).read_bytes())
        worker = next((x for x in roster if x.slack_user_id == admin.slack_user_id), None)
        a.user_key, a.actor_id = (worker.user_key if worker else _actor_user_key(admin)), admin.slack_user_id
    count = defer(a.state_db, a.user_key, a.before_date, a.actor_id, apply=a.apply)
    print(f"Historical shifts {'preserved as unresolved' if a.apply else 'selected (dry run)'}: {count}. No end times were assigned.")


if __name__ == "__main__":
    main()

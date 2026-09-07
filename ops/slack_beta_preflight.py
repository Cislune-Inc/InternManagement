"""Read-only local cutover checks; never enrolls, sends messages or calls APIs."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

from dotenv import dotenv_values

from agent.config import parse_agent_config, parse_roster_bytes
from ops.enable_slack_clock_beta import prepare


def inspect(config_path: Path, db_path: Path, credentials: dict, user_keys: list[str] | None = None) -> dict:
    result = {"blocks": [], "warnings": [], "checks": {}, "live_verification_required": True}
    try:
        payload = json.loads(config_path.read_text())
        config = parse_agent_config(payload, "America/Los_Angeles")
        raw = (config_path.parent / config.roster_file_name).read_bytes()
        candidate, excluded = prepare(payload, raw, user_keys)
        roster = [u for u in parse_roster_bytes(config.roster_file_name, raw) if u.active]
        selected = [u for u in roster if not user_keys or u.user_key in user_keys]
        result["checks"]["candidate_slack_identities"] = len(candidate["slack"]["work_intake_beta_slack_user_ids"])
        result["checks"]["excluded_active_workers"] = len(excluded)
        result["checks"]["company_timezone"] = config.timezone
        result["checks"]["daily_limit_hours"] = config.labor.overtime_limit_hours
        result["checks"]["compensation_plan_counts"] = dict(Counter(u.compensation_plan for u in selected))
        if excluded:
            result["warnings"].append("Excluded active workers must report actual hours to Erik; verify their fallback before switching.")
        if any(u.compensation_plan == "needs_review" for u in selected):
            result["warnings"].append("Some classifications need payroll review; preserve their hours meanwhile.")
        if any(u.compensation_plan == "cislune_hourly" and not u.gusto_entity_uuid for u in selected):
            result["warnings"].append("Some hourly workers lack Gusto mappings; resolve before payroll, not by rejecting time.")
    except (OSError, ValueError, TypeError, KeyError):
        # Do not dump private config/roster contents or parser exceptions.
        result["blocks"].append("Config/roster validation failed. Run the enrollment dry run locally to resolve mappings and selected identities.")

    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "OPENAI_API_KEY"):
        present = bool(str(credentials.get(key) or "").strip())
        result["checks"][key + "_present"] = present
        if not present:
            if key == "OPENAI_API_KEY":
                result["warnings"].append("OpenAI key missing: clock can work, but real AI assistance remains unverified.")
            else:
                result["blocks"].append(key + " is missing; the Slack clock cannot start.")

    try:
        if not db_path.is_file():
            raise FileNotFoundError
        # mode=ro preserves the canonical ledger and includes committed WAL data;
        # never use immutable=1 against a running production database.
        with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
            conn.execute("PRAGMA query_only=ON")
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError
            active = []
            for user_key, day, raw in conn.execute("SELECT user_key,session_date,payload FROM sessions"):
                state = json.loads(raw)
                meta = state.get("metadata") or {}
                if state.get("clocked_in_at") and (not state.get("clocked_out_at") or meta.get("slack_clock_meal_started_at")):
                    active.append((user_key, day))
            result["checks"]["open_shift_records"] = len(active)
            if active:
                result["warnings"].append("Open shifts exist: reconcile handover and any external ClickUp timers; never silently close or discard them.")
            if any(count > 1 for count in Counter(user for user, _ in active).values()):
                result["blocks"].append("A worker has multiple open shifts; reconcile before enabling that worker.")
            result["checks"]["database_quick_check"] = "ok"
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        result["blocks"].append("Canonical database missing, unreadable or invalid. Confirm its actual path and preserve evidence.")
    result["warnings"].append("Verify actual payroll workday/week, roster, current shifts, backup restore, Slack delivery and real OpenAI access; this report is not live acceptance.")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/agent.config.json"))
    parser.add_argument("--state-db", type=Path, required=True, help="Use the actual state_db_path from local bootstrap configuration.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--user-key", action="append")
    args = parser.parse_args()
    values = {**dotenv_values(args.env_file), **os.environ}
    result = inspect(args.config, args.state_db, values, args.user_key)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result["blocks"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

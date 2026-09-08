"""Explicit, backed-up roster cutover. Dry-run unless --apply is supplied."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agent.config import parse_agent_config, parse_roster_bytes
from agent.persistence import atomic_write_json


def check_setup_only_sessions(state_db: Path, user_keys: list[str]) -> None:
    """Read-only preflight: setup staging must never hold a running DP shift."""
    if not state_db.is_file():
        raise ValueError("An existing --state-db is required for setup-only enrollment.")
    with sqlite3.connect(state_db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        for key in user_keys:
            for row in conn.execute("SELECT payload FROM sessions WHERE user_key=?", (key,)):
                session = json.loads(row[0])
                meta = session.get("metadata", {})
                if meta.get("slack_clock_legacy_unresolved"):
                    continue
                if session.get("clocked_in_at") and (not session.get("clocked_out_at") or
                        meta.get("slack_clock_meal_started_at") or meta.get("slack_clock_rest_started_at")):
                    raise ValueError("Cannot stage setup with an open DP shift or break: " + key)


def prepare(payload: dict, roster_bytes: bytes, user_keys: list[str] | None,
            *, primary_admin_only: bool = False, primary_admin_slack_id: str | None = None,
            setup_only: bool = False) -> tuple[dict, list[str]]:
    payload = json.loads(json.dumps(payload))
    config = parse_agent_config(payload, "America/Los_Angeles")
    if primary_admin_slack_id:
        matches = [a for a in config.admins if a.slack_user_id == primary_admin_slack_id]
        if len(matches) != 1:
            raise ValueError("Primary admin must match exactly one existing verified admin Slack identity.")
        payload.update(admin_discord_user_id=matches[0].discord_user_id,
                       admin_slack_user_id=matches[0].slack_user_id,
                       admin_display_name=matches[0].name)
        config = parse_agent_config(payload, "America/Los_Angeles")
    roster = parse_roster_bytes(config.roster_file_name, roster_bytes)
    active = [u for u in roster if u.active]
    if len({u.user_key for u in active}) != len(active):
        raise ValueError("Duplicate active roster user_key; reconcile identities before cutover.")
    mapped = [u.slack_user_id for u in active if u.slack_user_id]
    if len(set(mapped)) != len(mapped):
        raise ValueError("Duplicate Slack identity across active workers; reconcile identities before cutover.")
    known = {u.user_key: u for u in roster if u.active}
    if primary_admin_only and user_keys:
        raise ValueError("Choose either primary-admin-only or a worker cohort.")
    if not primary_admin_only and not user_keys:
        raise ValueError("Choose explicit --user-key entries; the historical all-active roster is never a safe default.")
    chosen = set() if primary_admin_only else set(user_keys)
    if setup_only and primary_admin_only:
        raise ValueError("Setup-only needs explicit workers, not the primary-admin-only mode.")
    if chosen - set(known):
        raise ValueError("Unknown/inactive roster user_key: " + ", ".join(sorted(chosen - set(known))))
    missing = [key for key in chosen if not known[key].slack_user_id]
    if missing:
        raise ValueError("Map these active workers to verified Slack identities before cutover: " + ", ".join(sorted(missing)))
    ids = [known[key].slack_user_id for key in sorted(chosen)]
    ids.extend(admin.slack_user_id for admin in config.admins if admin.slack_user_id
               and admin.discord_user_id == config.admin_discord_user_id)
    if not ids or not any(admin.discord_user_id == config.admin_discord_user_id and admin.slack_user_id for admin in config.admins):
        raise ValueError("A verified primary-admin Slack mapping and a nonempty cohort are required.")
    updated = json.loads(json.dumps(payload))
    updated.setdefault("slack", {}).update({
        "enabled": True, "work_intake_beta_slack_user_ids": sorted(set(ids)),
        "daily_updates_enabled": False, "weekly_recaps_enabled": False,
    })
    if setup_only:
        # Never clear an earlier hold as a side effect of enrolling someone else.
        pending = set(updated["slack"].get("clock_handover_pending_slack_user_ids", []))
        pending.update(known[key].slack_user_id for key in chosen)
        updated["slack"]["clock_handover_pending_slack_user_ids"] = sorted(pending)
    parse_agent_config(updated, "America/Los_Angeles")
    return updated, sorted(set(known) - chosen)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/agent.config.json"))
    parser.add_argument("--user-key", action="append", help="Explicit current worker; repeat for the reviewed cohort. Only the primary admin is added automatically.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--setup-only", action="store_true", help="Show selected names and allow PIN setup, but hold starts/returns until actual earlier time and handover are reconciled. Never use for an active DP shift.")
    parser.add_argument("--state-db", type=Path, help="Existing production ledger for setup-only open-shift safety check.")
    parser.add_argument("--primary-admin-only", action="store_true", help="Explicit isolated dogfood cohort; no old worker or secondary-admin enrollment.")
    parser.add_argument("--primary-admin-slack-id", help="Explicitly select an already configured manager as primary authorization owner.")
    args = parser.parse_args()
    config_path = args.config.resolve()
    payload = json.loads(config_path.read_text())
    config = parse_agent_config(payload, "America/Los_Angeles")
    roster_path = config_path.parent / config.roster_file_name
    updated, excluded = prepare(payload, roster_path.read_bytes(), args.user_key,
                                primary_admin_only=args.primary_admin_only,
                                primary_admin_slack_id=args.primary_admin_slack_id,
                                setup_only=args.setup_only)
    if args.setup_only:
        if args.state_db is None:
            raise ValueError("Setup-only requires --state-db for the open-shift safety check.")
        check_setup_only_sessions(args.state_db, args.user_key)
        print("Setup-only: selected workers cannot start/return yet. PIN setup does not change attendance.")
    print("Slack will be the sole beta clock; Gusto Kiosk and Discord are not fallback clocks.")
    print("Selected Slack identities:", len(updated["slack"]["work_intake_beta_slack_user_ids"]))
    print("Excluded active workers (must Slack Erik actual hours):", ", ".join(excluded) or "none")
    if not args.apply:
        print("Dry run only. Verify Slack/OpenAI tokens, current shifts, roster classifications and payroll handling, then use --apply and restart.")
        return
    backup = config_path.with_name(config_path.name + ".before-slack-clock-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    shutil.copy2(config_path, backup)
    original_mode = config_path.stat().st_mode & 0o777
    atomic_write_json(config_path, updated)
    config_path.chmod(original_mode)
    print("Config updated with a preserved backup. Restart and verify an actual Slack clock-in/out and persisted hours before announcing that it is live.")


if __name__ == "__main__":
    main()

"""Explicit, backed-up roster cutover. Dry-run unless --apply is supplied."""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from agent.config import parse_agent_config, parse_roster_bytes
from agent.persistence import atomic_write_json


def prepare(payload: dict, roster_bytes: bytes, user_keys: list[str] | None) -> tuple[dict, list[str]]:
    config = parse_agent_config(payload, "America/Los_Angeles")
    roster = parse_roster_bytes(config.roster_file_name, roster_bytes)
    active = [u for u in roster if u.active]
    if len({u.user_key for u in active}) != len(active):
        raise ValueError("Duplicate active roster user_key; reconcile identities before cutover.")
    mapped = [u.slack_user_id for u in active if u.slack_user_id]
    if len(set(mapped)) != len(mapped):
        raise ValueError("Duplicate Slack identity across active workers; reconcile identities before cutover.")
    known = {u.user_key: u for u in roster if u.active}
    chosen = set(user_keys) if user_keys else set(known)
    if chosen - set(known):
        raise ValueError("Unknown/inactive roster user_key: " + ", ".join(sorted(chosen - set(known))))
    missing = [key for key in chosen if not known[key].slack_user_id]
    if missing:
        raise ValueError("Map these active workers to verified Slack identities before cutover: " + ", ".join(sorted(missing)))
    ids = [known[key].slack_user_id for key in sorted(chosen)]
    ids.extend(admin.slack_user_id for admin in config.admins if admin.slack_user_id)
    if not ids or not any(admin.discord_user_id == config.admin_discord_user_id and admin.slack_user_id for admin in config.admins):
        raise ValueError("A verified primary-admin Slack mapping and a nonempty cohort are required.")
    updated = json.loads(json.dumps(payload))
    updated.setdefault("slack", {}).update({
        "enabled": True, "work_intake_beta_slack_user_ids": sorted(set(ids)),
        "daily_updates_enabled": False, "weekly_recaps_enabled": False,
    })
    parse_agent_config(updated, "America/Los_Angeles")
    return updated, sorted(set(known) - chosen)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/agent.config.json"))
    parser.add_argument("--user-key", action="append", help="Limit the cohort; omitted means every active roster worker plus mapped admins.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config_path = args.config.resolve()
    payload = json.loads(config_path.read_text())
    config = parse_agent_config(payload, "America/Los_Angeles")
    roster_path = config_path.parent / config.roster_file_name
    updated, excluded = prepare(payload, roster_path.read_bytes(), args.user_key)
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

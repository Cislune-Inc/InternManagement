"""Explicit interaction rollout, separate from workforce enrollment. Dry-run default."""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from agent.config import parse_agent_config
from agent.persistence import atomic_write_json


def prepare(payload: dict, routes: list[str], enable_checkins: bool) -> dict:
    updated = json.loads(json.dumps(payload))
    slack = updated.setdefault("slack", {})
    channels = dict(slack.get("work_summary_channels", {}))
    for route in routes:
        key, channel = route.split("=", 1)
        if key in channels and channels[key] != channel:
            raise ValueError("Destination changes require a separate reviewed config edit")
        channels[key] = channel
    slack["work_summary_channels"] = channels
    if enable_checkins:
        slack["progress_checkins_enabled"] = True
    parse_agent_config(updated, "America/Los_Angeles")
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/agent.config.json"))
    parser.add_argument("--route", action="append", default=[], help="Verified project_key=channel_ID; no inferred routes")
    parser.add_argument("--enable-checkins", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    path = args.config.resolve()
    original = json.loads(path.read_text())
    updated = prepare(original, args.route, args.enable_checkins)
    print(json.dumps({k: updated["slack"][k] for k in ("progress_checkins_enabled", "work_summary_channels") if k in updated["slack"]}))
    if not args.apply:
        print("Dry run. Verify app membership and intended audiences before applying. No enrollment or posting performed.")
        return
    if original != updated:
        shutil.copy2(path, path.with_name(path.name + ".before-work-checkins-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")))
        mode = path.stat().st_mode & 0o777
        atomic_write_json(path, updated)
        path.chmod(mode)
    print("Saved. Restart the service to load. Existing enrollment and payroll config are unchanged.")


if __name__ == "__main__":
    main()

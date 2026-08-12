from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.persistence import atomic_write_json


def _marker_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "deploy_maintenance.json"


def start(minutes: int) -> Path:
    now = datetime.now(timezone.utc)
    path = _marker_path()
    atomic_write_json(
        path,
        {
            "reason": "controlled deployment",
            "started_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=max(1, minutes))).isoformat(),
        },
    )
    return path


def stop() -> Path:
    path = _marker_path()
    path.unlink(missing_ok=True)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the bounded Don Pollo deployment window.")
    subparsers = parser.add_subparsers(dest="action", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--minutes", type=int, default=15)
    subparsers.add_parser("stop")
    args = parser.parse_args()
    path = start(args.minutes) if args.action == "start" else stop()
    print(path)


if __name__ == "__main__":
    main()

"""Prepare a worker-confirmed preview from an explicitly selected hours report.

Does not apply time changes or send Slack. Run on the trusted service host.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path

from agent.meal_corrections import MealCorrections
from agent.slack_timekeeping import SlackTimekeeping, timestamp
from agent.state_store import StateStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", required=True, type=Path)
    parser.add_argument("--user-key", required=True)
    parser.add_argument("--start", required=True, help="Actual ISO timestamp with UTC offset")
    parser.add_argument("--end", required=True, help="Actual ISO timestamp with UTC offset")
    parser.add_argument("--source-report-id", required=True)
    args = parser.parse_args()
    if not args.state_db.is_file():
        parser.error("Existing state database required")
    clock = SlackTimekeeping(StateStore(args.state_db))
    response, _ = MealCorrections(clock).preview(
        args.user_key, timestamp(args.start), timestamp(args.end),
        now=datetime.now(timezone.utc), source_report_id=args.source_report_id)
    print(response)


if __name__ == "__main__":
    main()

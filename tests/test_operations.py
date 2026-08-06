from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agent.models import AdminProfile, SlackConfig
from agent.operations import OperationalIssueReporter
from agent.state_store import StateStore


class _Slack:
    def __init__(self):
        self.messages = []

    async def post_message(self, channel_id, message):
        self.messages.append((channel_id, message))
        return {"ok": True}


def test_operational_reporter_deduplicates_and_notifies_slack_admin(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    slack = _Slack()
    config = SimpleNamespace(
        slack=SlackConfig(
            enabled=True,
            operational_alert_cooldown_minutes=60,
        )
    )
    reporter = OperationalIssueReporter(
        state_store=store,
        config_provider=lambda: config,
        slack_provider=lambda: slack,
        admins_provider=lambda: [
            AdminProfile(
                name="Erik",
                discord_user_id=1,
                slack_user_id="UERIK",
            )
        ],
        timezone_provider=lambda: "America/Los_Angeles",
    )
    now = datetime(2026, 7, 30, 16, tzinfo=timezone.utc)

    first = asyncio.run(
        reporter.report(
            category="integration_slack",
            severity="error",
            summary="Slack failed.",
            fingerprint_parts=("slack",),
            now=now,
        )
    )
    second = asyncio.run(
        reporter.report(
            category="integration_slack",
            severity="error",
            summary="Slack failed again.",
            fingerprint_parts=("slack",),
            now=now + timedelta(minutes=10),
        )
    )

    assert first["occurrence_count"] == 1
    assert second["occurrence_count"] == 2
    assert len(slack.messages) == 1
    assert slack.messages[0][0] == "UERIK"
    assert "manager queue" in slack.messages[0][1]


def test_operational_warning_is_held_for_digest(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    slack = _Slack()
    config = SimpleNamespace(
        slack=SlackConfig(
            enabled=True,
            operational_digest_interval_minutes=60,
            manager_queue_url="http://192.168.4.87:8765/exceptions",
        )
    )
    reporter = OperationalIssueReporter(
        state_store=store,
        config_provider=lambda: config,
        slack_provider=lambda: slack,
        admins_provider=lambda: [
            AdminProfile(name="Erik", discord_user_id=1, slack_user_id="UERIK")
        ],
        timezone_provider=lambda: "America/Los_Angeles",
    )
    now = datetime(2026, 7, 30, 16, tzinfo=timezone.utc)

    asyncio.run(
        reporter.report(
            category="slack_route_uncertain",
            severity="warning",
            summary="Route needs review.",
            fingerprint_parts=("alex", "task-1"),
            now=now,
        )
    )

    assert slack.messages == []
    assert asyncio.run(reporter.maybe_send_digest(now)) is True
    assert len(slack.messages) == 1
    assert "operational digest" in slack.messages[0][1]
    assert "`slack_route_uncertain`: 1 open" in slack.messages[0][1]
    assert "http://192.168.4.87:8765/exceptions" in slack.messages[0][1]
    assert asyncio.run(reporter.maybe_send_digest(now + timedelta(minutes=30))) is False


def test_operational_digest_waits_until_eight_pacific_and_sends_once_per_day(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    slack = _Slack()
    config = SimpleNamespace(
        slack=SlackConfig(
            enabled=True,
            operational_digest_hour=8,
            operational_digest_timezone="America/Los_Angeles",
        )
    )
    reporter = OperationalIssueReporter(
        state_store=store,
        config_provider=lambda: config,
        slack_provider=lambda: slack,
        admins_provider=lambda: [
            AdminProfile(name="Erik", discord_user_id=1, slack_user_id="UERIK")
        ],
        timezone_provider=lambda: "America/Los_Angeles",
    )
    reporter.state_store.record_operational_issue(
        fingerprint="issue-1",
        category="review",
        severity="warning",
        summary="Review this.",
        observed_at=datetime(2026, 7, 30, 7, tzinfo=timezone(timedelta(hours=-7))),
    )

    before = datetime(2026, 7, 30, 7, 59, tzinfo=timezone(timedelta(hours=-7)))
    after = datetime(2026, 7, 30, 8, 1, tzinfo=timezone(timedelta(hours=-7)))
    late = datetime(2026, 7, 30, 17, 0, tzinfo=timezone(timedelta(hours=-7)))

    assert asyncio.run(reporter.maybe_send_digest(before)) is False
    assert asyncio.run(reporter.maybe_send_digest(after)) is True
    assert asyncio.run(reporter.maybe_send_digest(late)) is False
    assert len(slack.messages) == 1


def test_route_issue_maintenance_merges_worker_task_duplicates_and_retires_stale(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    reporter = OperationalIssueReporter(
        state_store=store,
        config_provider=lambda: None,
        slack_provider=lambda: None,
        admins_provider=lambda: [],
        timezone_provider=lambda: "America/Los_Angeles",
    )
    now = datetime(2026, 7, 30, 9, tzinfo=timezone.utc)
    details = {"user_key": "alex", "active_task_id": "task-1"}
    for index in range(3):
        store.record_operational_issue(
            fingerprint=f"legacy-{index}",
            category="slack_route_uncertain",
            severity="warning",
            summary="Route needs review.",
            details=details,
            observed_at=now - timedelta(days=index),
        )
    store.record_operational_issue(
        fingerprint="stale",
        category="slack_route_uncertain",
        severity="warning",
        summary="Old route.",
        details={"user_key": "old", "active_task_id": "task-old"},
        observed_at=now - timedelta(days=20),
    )

    result = reporter._maintain_route_issues(now)
    open_routes = [
        issue
        for issue in store.list_operational_issues(status="open", limit=100)
        if issue["category"] == "slack_route_uncertain"
    ]

    assert result == {"stale_resolved": 1, "merged": 2}
    assert len(open_routes) == 1
    assert open_routes[0]["occurrence_count"] == 3
    assert open_routes[0]["details"]["active_task_id"] == "task-1"

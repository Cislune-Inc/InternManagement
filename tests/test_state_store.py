from datetime import datetime, timedelta
from pathlib import Path

from agent.models import MessageRecord
from agent.state_store import StateStore


def test_append_message_ignores_exact_duplicates(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    message = MessageRecord(
        message_id="123",
        direction="inbound",
        author_id=1,
        created_at=datetime.fromisoformat("2026-05-28T10:00:00+00:00"),
        content="hello",
        attachments=[],
    )
    inserted_first = store.append_message("user", "2026-05-28", message)
    inserted_second = store.append_message("user", "2026-05-28", message)
    saved = store.list_messages("user", "2026-05-28")
    assert inserted_first is True
    assert inserted_second is False
    assert len(saved) == 1


def test_operational_state_and_issue_lifecycle(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.set_operational_state("slack_updates", {"last_daily_post_at": "2026-07-30T09:00:00-07:00"})

    assert store.get_operational_state("slack_updates") == {
        "last_daily_post_at": "2026-07-30T09:00:00-07:00"
    }

    first = store.record_operational_issue(
        fingerprint="scheduler:alex",
        category="scheduler_user_failure",
        severity="error",
        summary="Scheduler failed for Alex.",
        details={"user_key": "alex"},
        observed_at=datetime.fromisoformat("2026-07-30T10:00:00-07:00"),
    )
    second = store.record_operational_issue(
        fingerprint="scheduler:alex",
        category="scheduler_user_failure",
        severity="error",
        summary="Scheduler failed for Alex.",
        details={"user_key": "alex"},
        observed_at=datetime.fromisoformat("2026-07-30T10:05:00-07:00"),
    )

    assert first["occurrence_count"] == 1
    assert second["occurrence_count"] == 2
    assert store.operational_issue_needs_notification(
        "scheduler:alex",
        cooldown=timedelta(hours=6),
        now=datetime.fromisoformat("2026-07-30T10:05:00-07:00"),
    )

    store.mark_operational_issue_notified(
        "scheduler:alex",
        notified_at=datetime.fromisoformat("2026-07-30T10:05:00-07:00"),
    )
    assert not store.operational_issue_needs_notification(
        "scheduler:alex",
        cooldown=timedelta(hours=6),
        now=datetime.fromisoformat("2026-07-30T11:00:00-07:00"),
    )
    assert store.resolve_operational_issue(
        "scheduler:alex",
        resolved_at=datetime.fromisoformat("2026-07-30T11:30:00-07:00"),
    )
    assert store.list_operational_issues(status="open") == []
    assert store.list_operational_issues(status="resolved")[0]["status"] == "resolved"
    assert store.health_snapshot()["schema_version"] == 1


def test_resolve_matching_operational_issues_clears_only_recovered_subject(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    now = datetime.fromisoformat("2026-07-30T09:00:00-07:00")
    for user_key in ("Mac", "Alex"):
        store.record_operational_issue(
            fingerprint=f"scheduler:{user_key}",
            category="scheduler_user_failure",
            severity="error",
            summary=f"Scheduler failed for {user_key}.",
            details={"user_key": user_key, "error_type": "ConnectionError"},
            observed_at=now,
        )

    resolved = store.resolve_matching_operational_issues(
        category="scheduler_user_failure",
        details_match={"user_key": "Mac"},
        resolved_at=now + timedelta(minutes=1),
    )

    assert resolved == 1
    open_issues = store.list_operational_issues(status="open")
    assert [issue["details"]["user_key"] for issue in open_issues] == ["Alex"]

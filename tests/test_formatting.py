from datetime import datetime

from agent.formatting import build_clickup_update, build_transcript_markdown
from agent.models import AttachmentRecord, MessageRecord, SessionState, UserProfile


def test_build_clickup_update_prefers_task_onboarding_summary_when_present() -> None:
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-05-28",
        stage="active",
        latest_plan="old plan text",
        metadata={
            "active_clickup_task_name": "formalize project tree",
            "last_task_onboarding_summary": "Plan: Reorganize the tree.\nExpected duration: 2 hours.",
        },
    )

    rendered = build_clickup_update(user, session, "Daily summary", "tests")

    assert "Task onboarding plan" in rendered
    assert "Expected duration: 2 hours." in rendered
    assert "old plan text" not in rendered


def test_build_clickup_update_formats_clock_times_for_humans() -> None:
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
        timezone="America/New_York",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-04",
        stage="clocked_out",
        clocked_in_at="2026-06-04T09:00:00-07:00",
        clocked_out_at="2026-06-04T17:00:00-07:00",
    )

    rendered = build_clickup_update(user, session, "Daily summary", "tests")

    assert "Clocked in: " in rendered
    assert "9:00 AM PDT" in rendered
    assert "Clocked out: " in rendered
    assert "5:00 PM PDT" in rendered
    assert "T09:00:00" not in rendered


def test_build_transcript_markdown_renders_pacific_timestamps_without_changing_content() -> None:
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
    )
    session = SessionState(
        user_key="alex",
        session_date="2026-06-08",
        stage="active",
    )
    messages = [
        MessageRecord(
            message_id="m1",
            direction="inbound",
            author_id=1,
            created_at=datetime.fromisoformat("2026-06-08T16:09:32+00:00"),
            content="Still working on the controller.",
            attachments=[
                AttachmentRecord(
                    filename="photo.png",
                    url="https://example.com/photo.png",
                    content_type="image/png",
                    size=123,
                    local_path="C:/tmp/photo.png",
                    original_filename="controller.png",
                )
            ],
        ),
        MessageRecord(
            message_id="m2",
            direction="outbound",
            author_id=0,
            created_at=datetime.fromisoformat("2026-06-08T16:10:32+00:00"),
            content="Thanks, keep me posted.",
        ),
    ]

    rendered = build_transcript_markdown(user, session, messages)

    assert "### 2026-06-08 09:09:32 - Alex" in rendered
    assert "### 2026-06-08 09:10:32 - Agent" in rendered
    assert "Still working on the controller." in rendered
    assert "Thanks, keep me posted." in rendered
    assert "- photo.png [from controller.png]" in rendered

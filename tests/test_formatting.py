from agent.formatting import build_clickup_update
from agent.models import SessionState, UserProfile


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

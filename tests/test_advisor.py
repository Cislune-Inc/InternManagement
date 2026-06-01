import asyncio

from agent.advisor import Advisor, HeuristicAdvisor, ResilientAdvisor
from agent.models import SessionState, UserProfile


class FailingAdvisor(Advisor):
    async def plan_feedback(self, user: UserProfile, plan: str, clickup_context: str) -> str:
        raise RuntimeError("boom")

    async def summarize_updates(
        self,
        user: UserProfile,
        session: SessionState,
        messages,
        clickup_context: str,
    ) -> str:
        raise RuntimeError("boom")


def test_resilient_advisor_falls_back_after_primary_failure() -> None:
    advisor = ResilientAdvisor(FailingAdvisor(), HeuristicAdvisor())
    user = UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
    )
    session = SessionState(user_key="alex", session_date="2026-05-28")
    feedback = asyncio.run(advisor.plan_feedback(user, "ship feature", ""))
    summary = asyncio.run(advisor.summarize_updates(user, session, [], ""))
    assert "workable" in feedback
    assert "currently in stage" in summary
    assert advisor.primary_enabled is False

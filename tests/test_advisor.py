import asyncio
from types import SimpleNamespace

from agent.advisor import Advisor, HeuristicAdvisor, OpenAIAdvisor, ResilientAdvisor
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


class _FakeResponses:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def create(self, *, model: str, input) -> SimpleNamespace:
        self.calls.append(model)
        if model == "gpt-5-mini":
            raise RuntimeError("model unavailable")
        return SimpleNamespace(output_text=f"used {model}")


class _FakeClient:
    def __init__(self) -> None:
        self.responses = _FakeResponses()


def _build_user() -> UserProfile:
    return UserProfile(
        user_key="alex",
        display_name="Alex",
        discord_user_id=1,
        discord_username="alex",
        storage_folder_name="Alex",
    )


def test_resilient_advisor_falls_back_after_primary_failure() -> None:
    advisor = ResilientAdvisor(FailingAdvisor(), HeuristicAdvisor())
    user = _build_user()
    session = SessionState(user_key="alex", session_date="2026-05-28")
    feedback = asyncio.run(advisor.plan_feedback(user, "ship feature", ""))
    summary = asyncio.run(advisor.summarize_updates(user, session, [], ""))
    assert "workable" in feedback
    assert "currently in stage" in summary
    assert advisor.primary_enabled is False


def test_openai_advisor_promotes_backup_model_after_primary_failure() -> None:
    advisor = OpenAIAdvisor("test-key", "gpt-5-mini", "gpt-4.1-mini")
    advisor.client = _FakeClient()
    user = _build_user()
    session = SessionState(user_key="alex", session_date="2026-05-28")

    feedback = asyncio.run(advisor.plan_feedback(user, "ship feature", ""))
    summary = asyncio.run(advisor.summarize_updates(user, session, [], ""))

    assert feedback == "used gpt-4.1-mini"
    assert summary == "used gpt-4.1-mini"
    assert advisor.client.responses.calls == ["gpt-5-mini", "gpt-4.1-mini", "gpt-4.1-mini"]
    assert advisor.model == "gpt-4.1-mini"

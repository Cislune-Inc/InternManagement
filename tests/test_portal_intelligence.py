from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.portal_intelligence import PortalAIBudgetExceeded, PortalIntelligence
from agent.state_store import StateStore


class _FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "ready_to_use": True,
                    "outcome": "A revised bracket drawing with the corrected dimensions ready for review",
                    "first_step": "Open the current drawing and compare its dimensions with the measured bracket",
                    "evidence": "The revised drawing and a screenshot of the checked dimensions",
                    "estimate": "1 hour",
                    "checkpoint": "60 minutes",
                    "coaching_note": "The draft now has a recognizable result.",
                    "follow_up_question": "",
                }
            ),
            usage=SimpleNamespace(
                model_dump=lambda: {
                    "input_tokens": 180,
                    "output_tokens": 120,
                    "total_tokens": 300,
                    "input_tokens_details": {"cached_tokens": 80},
                }
            ),
        )


class _FakeAsyncOpenAI:
    responses = _FakeResponses()

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None


def test_portal_intelligence_uses_strict_structured_output_and_records_usage(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_PORTAL_MODEL", "gpt-5-mini")
    monkeypatch.setenv("OPENAI_PORTAL_MAX_CALLS_PER_WORKER", "1")
    monkeypatch.setattr("agent.portal_intelligence.AsyncOpenAI", _FakeAsyncOpenAI)
    _FakeAsyncOpenAI.responses = _FakeResponses()
    service = PortalIntelligence(StateStore(tmp_path / "state.sqlite3"))

    result = asyncio.run(
        service.coach_plan(
            slack_user_id="UERIK",
            worker_name="Erik",
            task_name="Revise bracket",
            task_location="NASA / Hardware",
            profile={"skills": ["CAD"], "interests": ["fabrication"]},
            intent="Fix the dimensions and leave the drawing ready for George to review.",
            existing_draft={},
        )
    )

    assert result["ready_to_use"] is True
    request = _FakeAsyncOpenAI.responses.calls[0]
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["store"] is False
    assert request["safety_identifier"].startswith("dp-")
    assert request["safety_identifier"] != "UERIK"
    snapshot = service.snapshot("UERIK")
    assert snapshot["tokens_used_today"] == 300
    assert snapshot["worker_calls_today"] == 1

    with pytest.raises(PortalAIBudgetExceeded, match="worker has been reached"):
        asyncio.run(
            service.coach_plan(
                slack_user_id="UERIK",
                worker_name="Erik",
                task_name="Revise bracket",
                task_location="NASA / Hardware",
                profile={},
                intent="Continue refining the same plan.",
                existing_draft={},
            )
        )

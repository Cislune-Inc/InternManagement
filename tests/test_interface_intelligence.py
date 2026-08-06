import asyncio
from types import SimpleNamespace

from agent.interface_intelligence import InterfaceIntelligence


def _build_interpreter() -> InterfaceIntelligence:
    interpreter = InterfaceIntelligence.__new__(InterfaceIntelligence)
    interpreter.client = object()
    interpreter.enabled = True
    interpreter.model = "gpt-4.1-mini"
    interpreter.models = SimpleNamespace()
    return interpreter


def test_resolve_task_draft_intent_returns_match_for_valid_json() -> None:
    interpreter = _build_interpreter()

    async def fake_create_response(_prompt: str):
        return SimpleNamespace(
            output_text=(
                '{"match_type":"intent_match","action":"mistaken_task_creation",'
                '"confidence":0.91,"reason":"Intern says they meant an existing task instead."}'
            )
        )

    interpreter._create_response = fake_create_response  # type: ignore[method-assign]

    match = asyncio.run(
        interpreter.resolve_task_draft_intent(
            "Actually I meant to pick one of my current tasks instead.",
            prompt_type="task_creation",
            step="title",
            source="intern_switch",
        )
    )

    assert match is not None
    assert match.action == "mistaken_task_creation"
    assert match.confidence == 0.91
    assert match.reason == "Intern says they meant an existing task instead."


def test_resolve_task_draft_intent_returns_none_for_low_confidence() -> None:
    interpreter = _build_interpreter()

    async def fake_create_response(_prompt: str):
        return SimpleNamespace(
            output_text='{"match_type":"intent_match","action":"mistaken_task_creation","confidence":0.42,"reason":"weak"}'
        )

    interpreter._create_response = fake_create_response  # type: ignore[method-assign]

    match = asyncio.run(
        interpreter.resolve_task_draft_intent(
            "Maybe not that.",
            prompt_type="unblocker_task_draft",
            step="description",
            source=None,
        )
    )

    assert match is None


def test_resolve_task_draft_intent_returns_none_for_malformed_output() -> None:
    interpreter = _build_interpreter()

    async def fake_create_response(_prompt: str):
        return SimpleNamespace(output_text="not json")

    interpreter._create_response = fake_create_response  # type: ignore[method-assign]

    match = asyncio.run(
        interpreter.resolve_task_draft_intent(
            "That was the wrong branch.",
            prompt_type="blocker_task",
            step="title",
            source=None,
        )
    )

    assert match is None

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Any

from openai import AsyncOpenAI

from .openai_models import ModelFallbackChain


_USAGE_STATE_PREFIX = "openai_portal_usage:"
_PLAN_OUTPUT_TOKENS = 800
_CHECKPOINT_OUTPUT_TOKENS = 650
_REQUEST_HEADROOM_TOKENS = 2_500

_INSTRUCTIONS = """You are Don Pollo's work-plan coauthor for a small engineering company.
Turn a worker's own facts into concise, useful project-management records.
Never invent measurements, file names, test results, deadlines, approvals, completed work, or contract facts.
Preserve the worker's meaning. Prefer plain language and concrete, independently recognizable outputs.
If an important fact is missing, draft only what the worker actually supported and ask one focused follow-up question.
Do not make timekeeping, break, overtime, disciplinary, payroll, or task-approval decisions.
The worker will review your draft, and deterministic software will validate it before any work record changes."""

_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ready_to_use": {"type": "boolean"},
        "outcome": {"type": "string"},
        "first_step": {"type": "string"},
        "evidence": {"type": "string"},
        "estimate": {
            "type": "string",
            "enum": ["30 minutes", "1 hour", "2 hours", "half day", "full day", "multi-day"],
        },
        "checkpoint": {
            "type": "string",
            "enum": ["30 minutes", "60 minutes", "90 minutes", "2 hours"],
        },
        "coaching_note": {"type": "string"},
        "follow_up_question": {"type": "string"},
    },
    "required": [
        "ready_to_use",
        "outcome",
        "first_step",
        "evidence",
        "estimate",
        "checkpoint",
        "coaching_note",
        "follow_up_question",
    ],
    "additionalProperties": False,
}

_CHECKPOINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ready_to_save": {"type": "boolean"},
        "progress": {"type": "string"},
        "evidence": {"type": "string"},
        "next_step": {"type": "string"},
        "blocker": {"type": "string"},
        "coaching_note": {"type": "string"},
        "follow_up_question": {"type": "string"},
    },
    "required": [
        "ready_to_save",
        "progress",
        "evidence",
        "next_step",
        "blocker",
        "coaching_note",
        "follow_up_question",
    ],
    "additionalProperties": False,
}


class PortalAIUnavailable(RuntimeError):
    pass


class PortalAIBudgetExceeded(RuntimeError):
    pass


class PortalIntelligence:
    """Explicit, review-before-submit AI coauthoring for the worker portal."""

    def __init__(self, state_store: Any) -> None:
        self.state_store = state_store
        self.api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
        self.models = ModelFallbackChain(
            "worker portal coaching",
            os.environ.get("OPENAI_PORTAL_MODEL"),
            os.environ.get("OPENAI_MODEL") or "gpt-5-mini",
            os.environ.get("BACKUP_OPENAI_MODEL"),
            "gpt-4.1-mini",
        )
        self.model = self.models.active_model or "gpt-5-mini"
        self.daily_token_budget = _positive_int(
            os.environ.get("OPENAI_PORTAL_DAILY_TOKEN_BUDGET"),
            default=100_000,
        )
        self.max_calls_per_worker = _positive_int(
            os.environ.get("OPENAI_PORTAL_MAX_CALLS_PER_WORKER"),
            default=30,
        )
        self.enabled = bool(self.api_key and self.models.candidate_models())

    def snapshot(self, slack_user_id: str) -> dict[str, Any]:
        state = self._usage_state()
        worker = state.get("by_worker", {}).get(_worker_hash(slack_user_id), {})
        total_tokens = _nonnegative_int(state.get("total_tokens"))
        worker_calls = _nonnegative_int(worker.get("calls"))
        remaining_tokens = max(0, self.daily_token_budget - total_tokens)
        remaining_calls = max(0, self.max_calls_per_worker - worker_calls)
        return {
            "enabled": self.enabled,
            "request_available": bool(
                self.enabled
                and remaining_tokens >= _REQUEST_HEADROOM_TOKENS
                and remaining_calls > 0
            ),
            "model": self.model,
            "daily_token_budget": self.daily_token_budget,
            "tokens_used_today": total_tokens,
            "tokens_remaining_today": remaining_tokens,
            "worker_calls_today": worker_calls,
            "worker_calls_remaining_today": remaining_calls,
        }

    async def coach_plan(
        self,
        *,
        slack_user_id: str,
        worker_name: str,
        task_name: str,
        task_location: str,
        profile: dict[str, Any],
        intent: str,
        existing_draft: dict[str, str],
    ) -> dict[str, Any]:
        prompt = {
            "request": "Create a reviewable work-plan draft from the worker's own information.",
            "worker": worker_name,
            "selected_task": {"name": task_name, "location": task_location},
            "worker_context": {
                "skills": list(profile.get("skills") or [])[:12],
                "interests": list(profile.get("interests") or [])[:12],
            },
            "worker_intent": intent,
            "existing_draft": existing_draft,
            "quality_rules": [
                "Outcome says what will exist or be demonstrably different by the checkpoint.",
                "First step names the first concrete action, artifact, part, test, or person.",
                "Evidence names a file, photo, measurement, test result, decision, or sent message.",
                "Use only the provided estimate and checkpoint choices.",
                "Set ready_to_use false and ask one focused question when the facts are insufficient.",
            ],
        }
        return await self._request(
            slack_user_id=slack_user_id,
            schema_name="don_pollo_work_plan",
            schema=_PLAN_SCHEMA,
            prompt=prompt,
            max_output_tokens=_PLAN_OUTPUT_TOKENS,
        )

    async def coach_checkpoint(
        self,
        *,
        slack_user_id: str,
        worker_name: str,
        task_name: str,
        plan: dict[str, str],
        progress: str,
        blocker: str,
    ) -> dict[str, Any]:
        prompt = {
            "request": "Turn the worker's rough update into a reviewable checkpoint draft.",
            "worker": worker_name,
            "selected_task": task_name,
            "current_plan": plan,
            "rough_progress": progress,
            "rough_blocker": blocker,
            "quality_rules": [
                "Progress says what changed, now exists, passed, failed, or was learned.",
                "Evidence identifies the observable proof already supplied by the worker; do not invent it.",
                "Next step is a concrete action supported by the worker's update and current plan.",
                "Blocker is empty when none was stated.",
                "Set ready_to_save false and ask one focused question when meaningful progress is missing.",
            ],
        }
        return await self._request(
            slack_user_id=slack_user_id,
            schema_name="don_pollo_checkpoint",
            schema=_CHECKPOINT_SCHEMA,
            prompt=prompt,
            max_output_tokens=_CHECKPOINT_OUTPUT_TOKENS,
        )

    async def _request(
        self,
        *,
        slack_user_id: str,
        schema_name: str,
        schema: dict[str, Any],
        prompt: dict[str, Any],
        max_output_tokens: int,
    ) -> dict[str, Any]:
        self._check_budget(slack_user_id)
        last_exc: Exception | None = None
        async with AsyncOpenAI(api_key=self.api_key) as client:
            for model in self.models.candidate_models():
                try:
                    request: dict[str, Any] = {
                        "model": model,
                        "instructions": _INSTRUCTIONS,
                        "input": json.dumps(prompt, sort_keys=True, separators=(",", ":")),
                        "text": {
                            "format": {
                                "type": "json_schema",
                                "name": schema_name,
                                "strict": True,
                                "schema": schema,
                            }
                        },
                        "max_output_tokens": max_output_tokens,
                        "store": False,
                        "safety_identifier": _worker_hash(slack_user_id),
                    }
                    if model.lower().startswith("gpt-5"):
                        request["reasoning"] = {"effort": "low"}
                    response = await client.responses.create(
                        **request,
                    )
                    result = json.loads(str(response.output_text or ""))
                    if not isinstance(result, dict):
                        raise ValueError("Structured response was not an object.")
                except Exception as exc:
                    last_exc = exc
                    continue
                self.models.record_success(model)
                self.model = model
                self._record_usage(slack_user_id, model=model, usage=getattr(response, "usage", None))
                return result
        self._record_error(slack_user_id, last_exc)
        raise PortalAIUnavailable(
            "Don Pollo's writing helper could not respond right now. Your draft is still here; edit the structured fields manually or try again."
        ) from last_exc

    def _check_budget(self, slack_user_id: str) -> None:
        if not self.enabled:
            raise PortalAIUnavailable(
                "Don Pollo's writing helper is not configured. You can still complete the structured fields manually."
            )
        snapshot = self.snapshot(slack_user_id)
        if snapshot["worker_calls_remaining_today"] <= 0:
            raise PortalAIBudgetExceeded(
                "Today's writing-helper limit for this worker has been reached. Continue with the structured fields or ask a manager."
            )
        if snapshot["tokens_remaining_today"] < _REQUEST_HEADROOM_TOKENS:
            raise PortalAIBudgetExceeded(
                "Today's Don Pollo AI token budget has been reached. Continue with the structured fields; timekeeping is unaffected."
            )

    def _usage_state(self) -> dict[str, Any]:
        today = datetime.now().astimezone().date().isoformat()
        state = self.state_store.get_operational_state(_USAGE_STATE_PREFIX + today) or {}
        if not isinstance(state.get("by_worker"), dict):
            state["by_worker"] = {}
        return state

    def _record_usage(self, slack_user_id: str, *, model: str, usage: Any) -> None:
        usage_data = _object_dict(usage)
        input_tokens = _nonnegative_int(usage_data.get("input_tokens"))
        output_tokens = _nonnegative_int(usage_data.get("output_tokens"))
        total_tokens = _nonnegative_int(usage_data.get("total_tokens")) or input_tokens + output_tokens
        details = _object_dict(usage_data.get("input_tokens_details"))
        cached_tokens = _nonnegative_int(details.get("cached_tokens"))
        state = self._usage_state()
        state["calls"] = _nonnegative_int(state.get("calls")) + 1
        state["input_tokens"] = _nonnegative_int(state.get("input_tokens")) + input_tokens
        state["output_tokens"] = _nonnegative_int(state.get("output_tokens")) + output_tokens
        state["total_tokens"] = _nonnegative_int(state.get("total_tokens")) + total_tokens
        state["cached_input_tokens"] = _nonnegative_int(state.get("cached_input_tokens")) + cached_tokens
        state["last_model"] = model
        state["updated_at"] = datetime.now().astimezone().isoformat()
        worker_key = _worker_hash(slack_user_id)
        worker = state["by_worker"].get(worker_key) or {}
        worker["calls"] = _nonnegative_int(worker.get("calls")) + 1
        worker["total_tokens"] = _nonnegative_int(worker.get("total_tokens")) + total_tokens
        state["by_worker"][worker_key] = worker
        self.state_store.set_operational_state(self._usage_key(), state)

    def _record_error(self, slack_user_id: str, error: Exception | None) -> None:
        state = self._usage_state()
        state["errors"] = _nonnegative_int(state.get("errors")) + 1
        state["last_error_type"] = type(error).__name__ if error else "UnknownError"
        state["last_error_at"] = datetime.now().astimezone().isoformat()
        worker_key = _worker_hash(slack_user_id)
        worker = state["by_worker"].get(worker_key) or {}
        worker["errors"] = _nonnegative_int(worker.get("errors")) + 1
        state["by_worker"][worker_key] = worker
        self.state_store.set_operational_state(self._usage_key(), state)

    @staticmethod
    def _usage_key() -> str:
        return _USAGE_STATE_PREFIX + datetime.now().astimezone().date().isoformat()


def _worker_hash(slack_user_id: str) -> str:
    normalized = str(slack_user_id or "unknown").strip().lower()
    return "dp-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _object_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump()
        return result if isinstance(result, dict) else {}
    return {}

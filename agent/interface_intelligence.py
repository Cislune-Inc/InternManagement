from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from openai import AsyncOpenAI

from .openai_models import ModelFallbackChain
from .signals import MessageSignals


@dataclass(slots=True)
class AdminCommandMatch:
    canonical_command: str
    confidence: float
    reason: str | None = None


class InterfaceIntelligence:
    def __init__(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        self.models = ModelFallbackChain(
            "interface intelligence",
            os.environ.get("OPENAI_INTERFACE_MODEL") or "gpt-4.1-mini",
            os.environ.get("BACKUP_OPENAI_MODEL"),
            "gpt-4.1-mini",
        )
        self.model = self.models.active_model or "gpt-4.1-mini"
        self.client = AsyncOpenAI(api_key=api_key) if api_key else None
        self.enabled = self.client is not None

    async def _create_response(self, prompt: str):
        if not self.client:
            raise RuntimeError("OpenAI client is not configured.")
        last_exc: Exception | None = None
        for model in self.models.candidate_models():
            try:
                response = await self.client.responses.create(model=model, input=prompt)
            except Exception as exc:
                last_exc = exc
                continue
            self.models.record_success(model)
            self.model = model
            return response
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No interface intelligence model is configured.")

    async def resolve_admin_command(self, user_text: str, command_templates: list[str]) -> AdminCommandMatch | None:
        if not self.enabled or not self.client or not user_text.strip():
            return None
        prompt = (
            "You are an intent router for a Discord admin console.\n"
            "Map the admin's message to the single best command template from the provided list.\n"
            "If a template contains a concrete name or project term, preserve or fill it from the user's wording.\n"
            "Only choose a command if it is a strong match. Otherwise return no_match.\n"
            "Return strict JSON with keys: match_type, canonical_command, confidence, reason.\n"
            "match_type must be one of exact_match, fuzzy_match, no_match.\n\n"
            f"Admin message:\n{user_text}\n\n"
            "Command templates:\n"
            + "\n".join(f"- {template}" for template in command_templates)
        )
        try:
            response = await self._create_response(prompt)
        except Exception:
            self.enabled = False
            return None
        payload = _extract_json(response.output_text)
        if not isinstance(payload, dict):
            return None
        match_type = str(payload.get("match_type") or "").strip().lower()
        canonical = str(payload.get("canonical_command") or "").strip()
        confidence = _coerce_confidence(payload.get("confidence"))
        reason = str(payload.get("reason") or "").strip() or None
        if match_type == "no_match" or not canonical or confidence < 0.55:
            return None
        return AdminCommandMatch(canonical_command=canonical, confidence=confidence, reason=reason)

    async def enrich_intern_signals(self, text: str, stage: str, signals: MessageSignals) -> MessageSignals:
        if not text.strip() or not self.enabled or not self.client:
            return signals
        should_query = False
        if stage == "awaiting_clock_in" and not signals.clocked_in:
            should_query = True
        elif stage in {"active", "awaiting_clock_out_artifacts"} and not (
            signals.clocking_out
            or signals.blocked_status
            or signals.help_requested
            or signals.help_declined
            or signals.recovered
            or signals.starting_lunch
        ):
            should_query = True
        elif stage == "on_lunch_break" and not signals.ending_lunch:
            should_query = True
        if not should_query:
            return signals

        prompt = (
            "You are classifying a Discord DM from an intern for a workflow state machine.\n"
            "Return strict JSON with boolean keys: clocked_in, clocking_out, blocked_status, help_requested, help_declined, recovered, starting_lunch, ending_lunch.\n"
            "Be conservative. Only set a field to true if the message clearly implies it.\n\n"
            f"Workflow stage: {stage}\n"
            f"Message:\n{text}"
        )
        try:
            response = await self._create_response(prompt)
        except Exception:
            self.enabled = False
            return signals
        payload = _extract_json(response.output_text)
        if not isinstance(payload, dict):
            return signals
        blocked_status = signals.blocked_status or bool(payload.get("blocked_status"))
        help_requested = signals.help_requested or bool(payload.get("help_requested"))
        help_declined = signals.help_declined or bool(payload.get("help_declined"))
        return MessageSignals(
            clocked_in=signals.clocked_in or bool(payload.get("clocked_in")),
            clocking_out=signals.clocking_out or bool(payload.get("clocking_out")),
            blocked_status=blocked_status,
            help_requested=help_requested,
            help_declined=help_declined,
            stuck=signals.stuck or blocked_status or help_requested,
            recovered=signals.recovered or bool(payload.get("recovered")),
            starting_lunch=signals.starting_lunch or bool(payload.get("starting_lunch")),
            ending_lunch=signals.ending_lunch or bool(payload.get("ending_lunch")),
        )


def _extract_json(text: str) -> dict | list | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def _coerce_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))

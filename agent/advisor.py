from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from openai import AsyncOpenAI

from .models import MessageRecord, SessionState, UserProfile
from .openai_models import ModelFallbackChain
from .time_utils import format_admin_datetime


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CheckInAssessment:
    meaningful_progress: bool
    needs_probe: bool
    reason: str
    probe_questions: list[str]


class Advisor:
    async def plan_feedback(self, user: UserProfile, plan: str, clickup_context: str) -> str:
        raise NotImplementedError

    async def summarize_updates(
        self,
        user: UserProfile,
        session: SessionState,
        messages: list[MessageRecord],
        clickup_context: str,
    ) -> str:
        raise NotImplementedError

    async def summarize_slack_progress(
        self,
        user: UserProfile,
        session: SessionState,
        messages: list[MessageRecord],
    ) -> str:
        raise NotImplementedError

    async def assess_check_in_reply(
        self,
        user: UserProfile,
        session: SessionState,
        question_text: str,
        reply_text: str,
        recent_messages: list[MessageRecord],
        *,
        previous_status: str | None = None,
        attachment_count: int = 0,
    ) -> CheckInAssessment:
        raise NotImplementedError


class HeuristicAdvisor(Advisor):
    async def plan_feedback(self, user: UserProfile, plan: str, clickup_context: str) -> str:
        if not clickup_context:
            return (
                "That sounds workable. Keep the scope tight, define the deliverable clearly, "
                "and surface blockers early if the work starts to drift."
            )
        lowered_plan = plan.lower()
        context_keywords = [line[2:].split("|")[0].strip() for line in clickup_context.splitlines() if line.startswith("- ")]
        overlaps = [item for item in context_keywords if any(word in lowered_plan for word in item.lower().split())]
        if overlaps:
            return (
                f"That plan lines up with current ClickUp work, especially {overlaps[0]}. "
                "Stay focused on finishing the highest-value piece before branching out."
            )
        if context_keywords:
            return (
                f"Your plan may need a quick priority check against ClickUp. "
                f"The most obvious open item I see is {context_keywords[0]}. "
                "If today's plan does not advance that or another priority, it may be worth reordering."
            )
        return "The plan is reasonable. Keep it measurable and tied to one concrete result."

    async def summarize_updates(
        self,
        user: UserProfile,
        session: SessionState,
        messages: list[MessageRecord],
        clickup_context: str,
    ) -> str:
        latest_user_updates = [message.content for message in messages if message.direction == "inbound" and message.content.strip()]
        attachment_count = sum(len(message.attachments) for message in messages if message.direction == "inbound")
        latest_excerpt = latest_user_updates[-2:] if latest_user_updates else []
        lines = [
            f"{user.display_name} is currently in stage `{session.stage}`.",
        ]
        if session.latest_plan:
            lines.append(f"Plan: {session.latest_plan}")
        if session.latest_blocker:
            lines.append(f"Blockers: {session.latest_blocker}")
        if session.latest_status:
            lines.append(f"Latest status: {session.latest_status}")
        if session.clocked_out_at:
            lines.append(
                f"They have clocked out at {format_admin_datetime(session.clocked_out_at, include_relative=False)}."
            )
        if attachment_count:
            lines.append(f"They shared {attachment_count} attachment(s) in this update window.")
        if latest_excerpt:
            lines.append("Recent updates:")
            lines.extend(f"- {item}" for item in latest_excerpt)
        return "\n".join(lines)

    async def summarize_slack_progress(
        self,
        user: UserProfile,
        session: SessionState,
        messages: list[MessageRecord],
    ) -> str:
        del user, session
        updates = [
            " ".join(message.content.strip().split())
            for message in messages
            if message.direction == "inbound" and message.content.strip()
        ]
        return " ".join(dict.fromkeys(updates))

    async def assess_check_in_reply(
        self,
        user: UserProfile,
        session: SessionState,
        question_text: str,
        reply_text: str,
        recent_messages: list[MessageRecord],
        *,
        previous_status: str | None = None,
        attachment_count: int = 0,
    ) -> CheckInAssessment:
        del user, session, question_text, recent_messages, previous_status
        normalized = " ".join(reply_text.strip().lower().split())
        if attachment_count > 0:
            return CheckInAssessment(
                meaningful_progress=True,
                needs_probe=False,
                reason="Attachments were included with the reply.",
                probe_questions=[],
            )
        if not normalized:
            return CheckInAssessment(
                meaningful_progress=False,
                needs_probe=True,
                reason="The reply did not contain any concrete project detail.",
                probe_questions=_default_probe_questions(),
            )
        generic_phrases = {
            "ok",
            "okay",
            "k",
            "still working",
            "working on it",
            "same thing",
            "same as before",
            "nothing much",
            "not much",
            "progress",
            "good",
            "fine",
        }
        if normalized in generic_phrases:
            return CheckInAssessment(
                meaningful_progress=False,
                needs_probe=True,
                reason="The reply is too generic to show what changed.",
                probe_questions=_default_probe_questions(),
            )
        words = [word for word in re.split(r"\s+", normalized) if word]
        if len(words) <= 3:
            return CheckInAssessment(
                meaningful_progress=False,
                needs_probe=True,
                reason="The reply is too short to show meaningful project progress.",
                probe_questions=_default_probe_questions(),
            )
        weak_patterns = (
            "still working",
            "keeping at it",
            "same task",
            "same project",
            "on it",
        )
        if any(phrase in normalized for phrase in weak_patterns) and not _looks_concrete(normalized):
            return CheckInAssessment(
                meaningful_progress=False,
                needs_probe=True,
                reason="The reply mentions ongoing work but not what actually changed.",
                probe_questions=_default_probe_questions(),
            )
        return CheckInAssessment(
            meaningful_progress=True,
            needs_probe=False,
            reason="The reply contains enough concrete detail to count as progress.",
            probe_questions=[],
        )


class OpenAIAdvisor(Advisor):
    def __init__(self, api_key: str, model: str, backup_model: str | None = None) -> None:
        self.client = AsyncOpenAI(api_key=api_key)
        self.models = ModelFallbackChain("advisor", model, backup_model, "gpt-4.1-mini")
        self.model = self.models.active_model or model

    async def _response_text(self, prompt: str) -> str:
        last_exc: Exception | None = None
        for model in self.models.candidate_models():
            try:
                response = await self.client.responses.create(model=model, input=prompt)
            except Exception as exc:
                last_exc = exc
                continue
            self.models.record_success(model)
            self.model = model
            return response.output_text.strip()
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No OpenAI advisor model is configured.")

    async def plan_feedback(self, user: UserProfile, plan: str, clickup_context: str) -> str:
        prompt = (
            "You are a project-management assistant helping an intern stay focused.\n"
            "Give concise, practical feedback on whether the plan is a good use of time, "
            "based on the ClickUp context. If it is weak, suggest a better direction.\n\n"
            f"Intern: {user.display_name}\n"
            f"Plan:\n{plan}\n\n"
            f"ClickUp context:\n{clickup_context or 'No ClickUp context available.'}"
        )
        return await self._response_text(prompt)

    async def summarize_updates(
        self,
        user: UserProfile,
        session: SessionState,
        messages: list[MessageRecord],
        clickup_context: str,
    ) -> str:
        transcript = "\n\n".join(
            f"[{message.created_at.isoformat()}] {message.direction}: {message.content}"
            + (f" [attachments={len(message.attachments)}]" if message.attachments else "")
            for message in messages
            if message.content.strip() or message.attachments
        )
        prompt = (
            "Summarize the intern's latest work session for a ClickUp update. "
            "Focus on progress, blockers, risks, next steps, and whether escalation is needed. "
            "Keep it concise and concrete.\n\n"
            f"Intern: {user.display_name}\n"
            f"Session date: {session.session_date}\n"
            f"ClickUp context:\n{clickup_context or 'No ClickUp context available.'}\n\n"
            f"Transcript:\n{transcript}"
        )
        return await self._response_text(prompt)

    async def summarize_slack_progress(
        self,
        user: UserProfile,
        session: SessionState,
        messages: list[MessageRecord],
    ) -> str:
        transcript = "\n".join(
            f"- {message.content.strip()}"
            for message in messages
            if message.direction == "inbound" and message.content.strip()
        )
        prompt = (
            "Extract only factual project progress from the worker messages for a public Slack update. "
            "Keep concrete changes, results, measurements, decisions, blockers, and next actions. "
            "Ignore complaints about the bot, time-clock questions, menu replies, greetings, and workflow chatter. "
            "Do not invent or infer accomplishments. Return at most two concise sentences with no heading. "
            "If there is no factual progress, return exactly NONE.\n\n"
            f"Worker: {user.display_name}\n"
            f"Task: {session.metadata.get('active_clickup_task_name') or 'unknown'}\n"
            f"Messages:\n{transcript or '(none)'}"
        )
        result = (await self._response_text(prompt)).strip()
        return "" if result.upper() == "NONE" else result

    async def assess_check_in_reply(
        self,
        user: UserProfile,
        session: SessionState,
        question_text: str,
        reply_text: str,
        recent_messages: list[MessageRecord],
        *,
        previous_status: str | None = None,
        attachment_count: int = 0,
    ) -> CheckInAssessment:
        recent_excerpt = "\n".join(
            f"- {message.direction}: {message.content.strip()}"
            for message in recent_messages[-6:]
            if message.content.strip()
        )
        active_task_id = str(session.metadata.get("active_clickup_task_id") or "") or "unknown"
        active_task_name = str(session.metadata.get("active_clickup_task_name") or "") or "unknown"
        prompt = (
            "You are reviewing an intern's reply to a scheduled project check-in.\n"
            "Decide whether the reply shows a real attempt to describe progress.\n"
            "Be conservative about probing. Short but concrete replies should count as meaningful.\n"
            "If the reply is too vague, ask for more detail with 1 to 3 direct questions.\n"
            "Return strict JSON with keys: meaningful_progress, needs_probe, reason, probe_questions.\n"
            "probe_questions must be an array of short strings.\n\n"
            f"Intern: {user.display_name}\n"
            f"Scheduled follow-up question: {question_text}\n"
            f"Combined reply:\n{reply_text or '(empty)'}\n\n"
            f"Attachment count in reply window: {attachment_count}\n"
            f"Active task: {active_task_name} ({active_task_id})\n"
            f"Latest plan: {session.latest_plan or 'n/a'}\n"
            f"Previous status: {previous_status or 'n/a'}\n"
            f"Recent transcript:\n{recent_excerpt or 'n/a'}"
        )
        raw = await self._response_text(prompt)
        payload = _extract_json(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("Advisor returned invalid check-in assessment JSON.")
        meaningful_progress = bool(payload.get("meaningful_progress"))
        needs_probe = bool(payload.get("needs_probe")) and not meaningful_progress
        probe_questions = [
            str(item).strip()
            for item in payload.get("probe_questions", [])
            if str(item).strip()
        ][:3]
        if needs_probe and not probe_questions:
            probe_questions = _default_probe_questions()
        reason = str(payload.get("reason") or "").strip() or (
            "The reply looks too vague to count as meaningful progress."
            if needs_probe
            else "The reply looks concrete enough to count as progress."
        )
        return CheckInAssessment(
            meaningful_progress=meaningful_progress and not needs_probe,
            needs_probe=needs_probe,
            reason=reason,
            probe_questions=probe_questions,
        )


class ResilientAdvisor(Advisor):
    def __init__(self, primary: Advisor, fallback: Advisor) -> None:
        self.primary = primary
        self.fallback = fallback
        self.primary_enabled = True

    async def plan_feedback(self, user: UserProfile, plan: str, clickup_context: str) -> str:
        if self.primary_enabled:
            try:
                return await self.primary.plan_feedback(user, plan, clickup_context)
            except Exception as exc:
                self._disable_primary(exc, "plan feedback")
        return await self.fallback.plan_feedback(user, plan, clickup_context)

    async def summarize_updates(
        self,
        user: UserProfile,
        session: SessionState,
        messages: list[MessageRecord],
        clickup_context: str,
    ) -> str:
        if self.primary_enabled:
            try:
                return await self.primary.summarize_updates(user, session, messages, clickup_context)
            except Exception as exc:
                self._disable_primary(exc, "session summary")
        return await self.fallback.summarize_updates(user, session, messages, clickup_context)

    async def summarize_slack_progress(
        self,
        user: UserProfile,
        session: SessionState,
        messages: list[MessageRecord],
    ) -> str:
        if self.primary_enabled:
            try:
                return await self.primary.summarize_slack_progress(user, session, messages)
            except Exception as exc:
                self._disable_primary(exc, "Slack progress summary")
        return await self.fallback.summarize_slack_progress(user, session, messages)

    async def assess_check_in_reply(
        self,
        user: UserProfile,
        session: SessionState,
        question_text: str,
        reply_text: str,
        recent_messages: list[MessageRecord],
        *,
        previous_status: str | None = None,
        attachment_count: int = 0,
    ) -> CheckInAssessment:
        if self.primary_enabled:
            try:
                return await self.primary.assess_check_in_reply(
                    user,
                    session,
                    question_text,
                    reply_text,
                    recent_messages,
                    previous_status=previous_status,
                    attachment_count=attachment_count,
                )
            except Exception as exc:
                self._disable_primary(exc, "check-in assessment")
        return await self.fallback.assess_check_in_reply(
            user,
            session,
            question_text,
            reply_text,
            recent_messages,
            previous_status=previous_status,
            attachment_count=attachment_count,
        )

    def _disable_primary(self, exc: Exception, context: str) -> None:
        self.primary_enabled = False
        logger.warning(
            "Disabling OpenAI advisor after %s failure: %s: %s",
            context,
            type(exc).__name__,
            exc,
        )


def build_advisor(openai_api_key: str | None, model: str | None, backup_model: str | None) -> Advisor:
    fallback = HeuristicAdvisor()
    if openai_api_key:
        return ResilientAdvisor(OpenAIAdvisor(openai_api_key, model or "gpt-5-mini", backup_model), fallback)
    return fallback


def _default_probe_questions() -> list[str]:
    return [
        "What specifically changed since the last check-in?",
        "What exact part, file, component, or task did you work on?",
        "What is the next step, or what is blocking you right now?",
    ]


def _looks_concrete(normalized: str) -> bool:
    if any(char.isdigit() for char in normalized):
        return True
    concrete_terms = (
        "file",
        "component",
        "module",
        "motor",
        "controller",
        "wired",
        "flashed",
        "tested",
        "printed",
        "cad",
        "board",
        "firmware",
        "code",
        "can",
        "sensor",
        "mount",
        "solder",
        "configured",
        "assembled",
    )
    return any(term in normalized for term in concrete_terms)


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

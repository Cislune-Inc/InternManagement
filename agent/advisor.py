from __future__ import annotations

import logging
from datetime import datetime

from openai import AsyncOpenAI

from .models import MessageRecord, SessionState, UserProfile
from .openai_models import ModelFallbackChain


logger = logging.getLogger(__name__)


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
            lines.append(f"They have clocked out at {session.clocked_out_at}.")
        if attachment_count:
            lines.append(f"They shared {attachment_count} attachment(s) in this update window.")
        if latest_excerpt:
            lines.append("Recent updates:")
            lines.extend(f"- {item}" for item in latest_excerpt)
        return "\n".join(lines)


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

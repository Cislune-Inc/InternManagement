from __future__ import annotations

import base64
import csv
from difflib import SequenceMatcher
import hashlib
import hmac
import io
import json
import re
import secrets
import shutil
import stat
import time
from datetime import datetime, timedelta, timezone
from typing import Any, TypeAlias
from urllib.parse import quote, urlsplit

from .config import parse_roster_bytes
from .models import AdminProfile, MessageRecord, SessionState, UserProfile
from .persistence import atomic_write_text
from .portal_intelligence import PortalIntelligence


_PORTAL_SECRET_STATE_KEY = "worker_portal_signing_secret"
_PORTAL_STATE_PREFIX = "worker_portal_beta:"
PortalActor: TypeAlias = AdminProfile | UserProfile
_GENERIC_WORK_REPLIES = {
    "continue",
    "continue working",
    "do the task",
    "finish it",
    "get it done",
    "keep working",
    "make progress",
    "same as yesterday",
    "work on it",
    "working on it",
}
_DEFAULT_WORKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
_VALID_ESTIMATES = {"30 minutes", "1 hour", "2 hours", "half day", "full day", "multi-day"}
_VALID_CHECKPOINTS = {"30 minutes", "60 minutes", "90 minutes", "2 hours", "when result is ready"}
_VALID_PROGRESS_KINDS = {"made_progress", "result_ready", "blocked", "changed_plan", "still_working"}
_QUALITY_WARNING_KEY = "portal_quality_warning"
_QUALITY_RESTART_BLOCK_KEY = "portal_quality_restart_blocked"
_QUALITY_WARNING_MINUTES = 10
_OVERHEAD_LANES = [
    {
        "id": "shop_facilities_safety",
        "name": "Shop, facilities & safety",
        "search": "shop safety facilities organize clean maintenance",
        "example": "A labeled, cleared work area with hazards and missing supplies recorded.",
    },
    {
        "id": "internal_systems_automation",
        "name": "Internal systems & automation",
        "search": "internal automation software systems process",
        "example": "A tested workflow improvement with the before/after behavior documented.",
    },
    {
        "id": "bid_proposal",
        "name": "Bid & proposal",
        "search": "proposal bid solicitation sbir sttr",
        "example": "A reviewed proposal section, compliance item, budget input, or submission decision.",
    },
    {
        "id": "internal_rd",
        "name": "Internal R&D & capability",
        "search": "research development capability prototype experiment",
        "example": "A prototype, experiment result, trade study, or reusable technical capability.",
    },
    {
        "id": "finance_legal_admin",
        "name": "Finance, legal & administration",
        "search": "finance accounting legal admin invoice compliance",
        "example": "A reconciled record, filed document, reviewed agreement, or resolved admin action.",
    },
    {
        "id": "people_company_management",
        "name": "People & company management",
        "search": "people management hiring training schedule program management",
        "example": "A decision, schedule, assignment, review, or documented follow-up with an owner.",
    },
    {
        "id": "business_development",
        "name": "Business development & outreach",
        "search": "business development outreach customer partner sales",
        "example": "A qualified lead, customer follow-up, partner decision, or recorded next action.",
    },
    {
        "id": "conference_travel",
        "name": "Conferences & proposal travel",
        "search": "conference travel event proposal meeting",
        "example": "A booked or completed event action tied to a purpose, contact, or proposal result.",
    },
]


def build_worker_portal_link(
    runtime: Any,
    actor: PortalActor,
    *,
    now: datetime | None = None,
    ttl_hours: int = 72,
) -> str:
    if not actor.slack_user_id:
        raise ValueError("The beta tester needs a Slack member ID.")
    if not _is_worker_portal_beta_tester(runtime, actor.slack_user_id):
        raise ValueError("The worker portal is limited to the configured beta testers.")
    if resolve_worker_portal_actor(runtime, actor.slack_user_id) is None:
        raise ValueError("The beta tester is not an active Don Pollo Slack user.")
    reference = now or datetime.now(timezone.utc)
    token = _issue_token(runtime, actor.slack_user_id, reference, ttl_hours=ttl_hours)
    manager_url = str(runtime.config.slack.manager_queue_url or "http://127.0.0.1:8765/exceptions")
    parsed = urlsplit(manager_url)
    origin = f"{parsed.scheme or 'http'}://{parsed.netloc or '127.0.0.1:8765'}"
    return f"{origin}/portal?token={quote(token)}"


def validate_worker_portal_token(
    runtime: Any,
    token: str,
    *,
    now: datetime | None = None,
) -> str:
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload_bytes = _decode_urlsafe(encoded_payload)
        signature = _decode_urlsafe(encoded_signature)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("This portal link is invalid. Ask Don Pollo for a new `portal` link in Slack.") from exc
    state = runtime.state_store.get_operational_state(_PORTAL_SECRET_STATE_KEY) or {}
    secret = str(state.get("secret") or "")
    expected = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    if not secret or not hmac.compare_digest(signature, expected):
        raise ValueError("This portal link is invalid. Ask Don Pollo for a new `portal` link in Slack.")
    subject = str(payload.get("sub") or "").strip()
    expires_at = int(payload.get("exp") or 0)
    reference = now or datetime.now(timezone.utc)
    if not subject or int(reference.timestamp()) >= expires_at:
        raise ValueError("This portal link expired. DM `portal` to Don Pollo in Slack for a fresh link.")
    actor = resolve_worker_portal_actor(runtime, subject)
    if actor is None or not _is_worker_portal_beta_tester(runtime, subject):
        raise ValueError("This beta link is not assigned to a current Don Pollo beta tester.")
    return subject


def resolve_worker_portal_actor(runtime: Any, slack_user_id: str) -> PortalActor | None:
    normalized = str(slack_user_id or "").strip()
    if not normalized:
        return None
    admin_lookup = getattr(runtime, "admin_profile_by_slack_user_id", None)
    if callable(admin_lookup):
        admin = admin_lookup(normalized)
        if admin is not None:
            return admin
    roster = getattr(runtime, "roster_by_slack_id", {})
    if isinstance(roster, dict):
        worker = roster.get(normalized)
        if isinstance(worker, UserProfile) and worker.active:
            return worker
    return None


def _is_worker_portal_beta_tester(runtime: Any, slack_user_id: str) -> bool:
    config = getattr(runtime, "config", None)
    slack_config = getattr(config, "slack", None)
    allowed = getattr(slack_config, "worker_portal_beta_slack_user_ids", [])
    normalized = str(slack_user_id or "").strip()
    return bool(normalized and normalized in {str(item).strip() for item in allowed})


def _actor_name(actor: PortalActor) -> str:
    return actor.name if isinstance(actor, AdminProfile) else actor.display_name


def _actor_user_key(actor: PortalActor) -> str:
    if isinstance(actor, UserProfile):
        return actor.user_key
    return f"portal-admin-{_actor_name(actor).lower().replace(' ', '-')}"


def validate_work_commitment(
    outcome: str,
    first_step: str,
    *,
    evidence: str | None = None,
    previous_fingerprint: str = "",
    recent_details: list[str] | None = None,
) -> tuple[list[str], str]:
    outcome = _clean_text(outcome, limit=800)
    first_step = _clean_text(first_step, limit=500)
    evidence_text = _clean_text(evidence, limit=500) if evidence is not None else ""
    issues: list[str] = []
    if _is_vague(outcome, minimum_words=6, minimum_characters=28):
        issues.append("Describe the result you expect to point to—not just that you will ‘work on’ the task.")
    if _is_vague(first_step, minimum_words=4, minimum_characters=18):
        issues.append("Name the first concrete action, file, part, test, or person you will start with.")
    if evidence is not None and _is_vague(evidence_text, minimum_words=4, minimum_characters=18):
        issues.append("Say what evidence will show the result, such as a file, photo, measurement, test, decision, or sent message.")
    combined = "\n".join(part for part in (outcome, first_step, evidence_text) if part)
    fingerprint = hashlib.sha256(combined.lower().encode("utf-8")).hexdigest()
    if previous_fingerprint and hmac.compare_digest(previous_fingerprint, fingerprint):
        issues.append("This is the same answer as the last rejected attempt; add the missing specifics before trying again.")
    if duplicate := _duplicate_detail_issue(combined, recent_details or []):
        issues.append(duplicate)
    return issues, fingerprint


def validate_meaningful_work_detail(
    value: str,
    *,
    purpose: str,
    previous_fingerprint: str = "",
    recent_details: list[str] | None = None,
) -> tuple[str | None, str]:
    cleaned = _clean_text(value, limit=800)
    fingerprint = hashlib.sha256(cleaned.lower().encode("utf-8")).hexdigest()
    if previous_fingerprint and hmac.compare_digest(previous_fingerprint, fingerprint):
        return (
            "That repeats the last rejected answer. Add the missing specifics instead of resubmitting the same text.",
            fingerprint,
        )
    if _is_vague(cleaned, minimum_words=5, minimum_characters=24):
        return (
            f"Add a specific {purpose}: name the output, change, test, file, part, or decision another person could recognize.",
            fingerprint,
        )
    if duplicate := _duplicate_detail_issue(cleaned, recent_details or []):
        return duplicate, fingerprint
    return None, fingerprint


class WorkerPortalService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._task_source_cache: dict[str, tuple[float, list[dict[str, Any]], set[str]]] = {}
        self.portal_intelligence = getattr(runtime, "portal_intelligence", None) or PortalIntelligence(
            runtime.state_store
        )

    async def build_payload(self, token: str) -> dict[str, Any]:
        slack_user_id = validate_worker_portal_token(self.runtime, token)
        actor = resolve_worker_portal_actor(self.runtime, slack_user_id)
        if actor is None:
            raise ValueError("The beta tester is no longer configured.")
        state = self._load_state(slack_user_id, actor)
        if self._live_enabled(actor):
            await self._enforce_live_deadlines(actor)
            self._sync_state_from_live_session(actor, state)
        else:
            self._advance_deadlines(state)
        self._save_state(slack_user_id, state)
        tasks, task_warning = await self._load_task_options(actor, state)
        return self._payload(actor, state, tasks, task_warning)

    async def apply_action(self, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        slack_user_id = validate_worker_portal_token(self.runtime, token)
        actor = resolve_worker_portal_actor(self.runtime, slack_user_id)
        if actor is None:
            raise ValueError("The beta tester is no longer configured.")
        state = self._load_state(slack_user_id, actor)
        live = self._live_enabled(actor)
        if live:
            await self._enforce_live_deadlines(actor)
            self._sync_state_from_live_session(actor, state)
        else:
            self._advance_deadlines(state)
        # Accept both DOM-style action names (``check-in``) and the API's
        # canonical snake_case names. This also keeps already-open portal pages
        # compatible after a server-side deploy.
        action = str(payload.get("action") or "").strip().lower().replace("-", "_")
        message = ""
        if action == "save_profile":
            self._save_profile(state, payload)
            if live:
                if isinstance(actor, UserProfile):
                    await self._save_live_profile(actor, state["profile"])
                    actor = resolve_worker_portal_actor(self.runtime, slack_user_id) or actor
                    message = "Schedule and worker context saved to the live Don Pollo roster."
                else:
                    message = "Schedule and project context saved for live admin tracking."
            else:
                message = "Profile saved. Don Pollo can now use this context when ranking work and timing prompts."
        elif action == "select_task":
            task_id = _clean_text(payload.get("task_id"), limit=100)
            task_name = _clean_text(payload.get("task_name"), limit=240)
            if not task_id or not task_name:
                raise ValueError("Choose one of the task options first.")
            state["work"]["selected_task_id"] = task_id
            state["work"]["selected_task_name"] = task_name
            state["work"]["selected_task_location"] = _clean_text(payload.get("task_location"), limit=240)
            state["work"]["destination_type"] = "clickup_task"
            state["work"]["overhead_lane_id"] = ""
            state["work"]["overhead_lane_name"] = ""
            message = f"Selected {task_name}. Add a concrete outcome and first step before starting."
        elif action == "select_overhead_lane":
            lane_id = _clean_text(payload.get("lane_id"), limit=80)
            lane = next((item for item in _OVERHEAD_LANES if item["id"] == lane_id), None)
            if lane is None:
                raise ValueError("Choose one of the listed non-contract work categories.")
            state["work"].update(
                {
                    "destination_type": "overhead",
                    "overhead_lane_id": lane["id"],
                    "overhead_lane_name": lane["name"],
                    "selected_task_id": "",
                    "selected_task_name": "",
                    "selected_task_location": "",
                }
            )
            message = (
                f"Selected {lane['name']}. Choose a matching existing ClickUp task below, "
                "or propose a new one for manager approval."
            )
        elif action == "claim_task":
            message = await self._claim_task(actor, state, payload)
        elif action == "coach_plan":
            message = await self._coach_plan(slack_user_id, actor, state, payload)
        elif action == "coach_checkpoint":
            message = await self._coach_checkpoint(slack_user_id, actor, state, payload)
        elif action == "start":
            try:
                message = (
                    await self._start_live_work(actor, state, payload)
                    if live
                    else self._start_work(state, payload)
                )
            except ValueError as exc:
                self._record_history(state, "start_rejected", str(exc))
                if live:
                    self._sync_state_from_live_session(actor, state)
                self._save_state(slack_user_id, state)
                raise
        elif action == "check_in":
            try:
                message = (
                    await self._check_in_live_work(actor, state, payload)
                    if live
                    else self._check_in(state, payload)
                )
            except ValueError as exc:
                self._record_history(state, "check_in_rejected", str(exc))
                if live:
                    self._sync_state_from_live_session(actor, state)
                self._save_state(slack_user_id, state)
                raise
        elif action == "short_rest":
            message = (
                await self._start_live_short_rest(actor)
                if live
                else self._start_short_rest(state)
            )
        elif action == "lunch":
            message = (
                await self._start_live_lunch(actor)
                if live
                else self._start_lunch(state)
            )
        elif action == "back":
            message = (
                await self._return_from_live_break(actor)
                if live
                else self._return_from_break(state)
            )
        elif action == "clock_out":
            message = (
                await self._clock_out_live(actor, payload)
                if live
                else self._clock_out(state)
            )
        elif action == "request_task":
            message = await self._request_task(slack_user_id, state, payload)
        elif action == "share_slack":
            message = await self._share_to_slack(slack_user_id, state)
        elif action == "reset_beta":
            if live:
                raise ValueError("Live time records cannot be reset from the worker portal. Ask a manager for a correction.")
            state = self._default_state(actor)
            message = "Beta workday reset. No live workforce records were changed."
        else:
            raise ValueError("Unsupported portal action.")
        self._record_history(state, action, message)
        if live:
            self._sync_state_from_live_session(actor, state)
        self._save_state(slack_user_id, state)
        tasks, task_warning = await self._load_task_options(actor, state)
        result = self._payload(actor, state, tasks, task_warning)
        result["message"] = message
        return result

    def render_html(self, payload: dict[str, Any]) -> str:
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":")).replace("</", "<\\/")
        return _portal_template().replace("__PORTAL_PAYLOAD__", payload_json)

    def _load_state(self, slack_user_id: str, actor: PortalActor) -> dict[str, Any]:
        state = self.runtime.state_store.get_operational_state(_PORTAL_STATE_PREFIX + slack_user_id)
        if not isinstance(state, dict):
            return self._default_state(actor)
        default = self._default_state(actor)
        for key in ("profile", "work", "quality", "ai"):
            if not isinstance(state.get(key), dict):
                state[key] = default[key]
            else:
                state[key] = {**default[key], **state[key]}
        if not isinstance(state.get("history"), list):
            state["history"] = []
        if not isinstance(state.get("task_requests"), list):
            state["task_requests"] = []
        return state

    def _default_state(self, actor: PortalActor) -> dict[str, Any]:
        if isinstance(actor, UserProfile):
            weekly_target_hours = actor.weekly_target_hours
            regular_workdays = actor.regular_workdays or list(_DEFAULT_WORKDAYS)
            typical_start_time = actor.typical_start_time
            typical_end_time = actor.typical_end_time
            planned_time_off = actor.planned_time_off
            interests = actor.interests
            skills = actor.skills
        else:
            weekly_target_hours = 40
            regular_workdays = list(_DEFAULT_WORKDAYS)
            typical_start_time = "09:00"
            typical_end_time = "17:00"
            planned_time_off = []
            interests = ["project delivery", "process improvement"]
            skills = ["program management"]
        return {
            "version": 1,
            "profile": {
                "display_name": _actor_name(actor),
                "saved_at": "",
                "weekly_target_hours": weekly_target_hours,
                "regular_workdays": list(regular_workdays),
                "typical_start_time": typical_start_time,
                "typical_end_time": typical_end_time,
                "planned_time_off": list(planned_time_off),
                "interests": list(interests),
                "skills": list(skills),
            },
            "work": {
                "status": "ready",
                "selected_task_id": "",
                "selected_task_name": "",
                "selected_task_location": "",
                "destination_type": "",
                "overhead_lane_id": "",
                "overhead_lane_name": "",
                "plan_intent": "",
                "outcome": "",
                "first_step": "",
                "evidence": "",
                "estimate": "1 hour",
                "checkpoint": "60 minutes",
                "checkpoint_due_at": "",
                "progress_kind": "made_progress",
                "started_at": "",
                "break_started_at": "",
                "break_minimum_end_at": "",
                "break_cutoff_at": "",
                "latest_progress": "",
                "latest_blocker": "",
                "notice": "",
            },
            "quality": {
                "date": "",
                "weak_attempts": 0,
                "consecutive_rejections": 0,
                "last_rejected_fingerprint": "",
                "strong_plans": 0,
                "recent_details": [],
                "warning_deadline_at": "",
                "warning_reasons": [],
                "auto_clocked_out_at": "",
            },
            "ai": {
                "last_plan_note": "",
                "last_plan_question": "",
                "last_plan_ready": False,
                "last_checkpoint_note": "",
                "last_checkpoint_question": "",
                "last_checkpoint_ready": False,
            },
            "task_requests": [],
            "history": [],
        }

    async def _load_task_options(
        self,
        actor: PortalActor,
        state: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str]:
        if not self.runtime.clickup:
            return self._fallback_tasks(), "ClickUp is unavailable, so these are safe beta examples."
        profile = state["profile"]
        portal_user = UserProfile(
            user_key=_actor_user_key(actor),
            display_name=_actor_name(actor),
            discord_user_id=actor.discord_user_id,
            slack_user_id=actor.slack_user_id,
            clickup_user_id=actor.clickup_user_id,
            clickup_user_email=actor.clickup_user_email,
            interests=list(profile.get("interests") or []),
            skills=list(profile.get("skills") or []),
        )
        cached = self._task_source_cache.get(str(actor.slack_user_id))
        if cached and time.monotonic() - cached[0] < 180:
            candidates = list(cached[1])
            assigned_ids = set(cached[2])
        else:
            candidates: list[dict[str, Any]] = []
            assigned_ids: set[str] = set()
            try:
                assigned = await self.runtime.clickup.list_assigned_tasks(portal_user, limit=500)
                candidates.extend(task for task in assigned if isinstance(task, dict))
                assigned_ids = {str(task.get("id") or "") for task in candidates}
                list_workspace = getattr(self.runtime.clickup, "list_workspace_tasks", None)
                if callable(list_workspace):
                    workspace = await list_workspace(limit=500, include_closed=False)
                    candidates.extend(task for task in workspace if isinstance(task, dict))
                self._task_source_cache[str(actor.slack_user_id)] = (
                    time.monotonic(),
                    list(candidates),
                    set(assigned_ids),
                )
            except Exception as exc:
                if cached:
                    candidates = list(cached[1])
                    assigned_ids = set(cached[2])
                else:
                    return self._fallback_tasks(), f"Live ClickUp options could not load: {exc}"
        deduped: dict[str, dict[str, Any]] = {}
        for task in candidates:
            task_id = str(task.get("id") or "").strip()
            status = str((task.get("status") or {}).get("status") or "").lower()
            if not task_id or status in {"closed", "complete", "completed", "done"}:
                continue
            deduped.setdefault(task_id, task)
        ranked = sorted(
            deduped.values(),
            key=lambda task: self._task_rank(task, assigned_ids, profile),
            reverse=True,
        )
        selected_id = str(state["work"].get("selected_task_id") or "")
        if selected_id and selected_id in deduped:
            ranked = [deduped[selected_id]] + [task for task in ranked if str(task.get("id")) != selected_id]
        options = [self._task_payload(task, assigned_ids, profile, index) for index, task in enumerate(ranked)]
        return options or self._fallback_tasks(), ""

    def _task_rank(
        self,
        task: dict[str, Any],
        assigned_ids: set[str],
        profile: dict[str, Any],
    ) -> tuple[int, int, int, str]:
        task_id = str(task.get("id") or "")
        status = str((task.get("status") or {}).get("status") or "").lower()
        priority = str((task.get("priority") or {}).get("priority") or "").lower()
        score = {"urgent": 55, "high": 40, "normal": 22, "low": 8}.get(priority, 12)
        if task_id in assigned_ids:
            score += 35
        if status in {"in progress", "in_progress", "active"}:
            score += 28
        elif status in {"to do", "todo", "open", "ready"}:
            score += 15
        haystack = " ".join(
            [
                str(task.get("name") or ""),
                str(task.get("description") or ""),
                str((task.get("list") or {}).get("name") or ""),
                str((task.get("folder") or {}).get("name") or ""),
                str((task.get("space") or {}).get("name") or ""),
            ]
        ).lower()
        context_terms = [
            str(term).strip().lower()
            for term in list(profile.get("skills") or []) + list(profile.get("interests") or [])
            if str(term).strip()
        ]
        matches = sum(1 for term in context_terms if term in haystack)
        score += min(matches, 4) * 9
        due_at = _task_due_datetime(task)
        deadline_tiebreak = -int(due_at.timestamp() * 1000) if due_at else -(10**30)
        if due_at:
            remaining = due_at - datetime.now(timezone.utc)
            if remaining.total_seconds() < 0:
                overdue_by = -remaining
                if overdue_by <= timedelta(days=7):
                    score += 70
                elif overdue_by <= timedelta(days=30):
                    score += 45
                else:
                    score += 18
            elif remaining <= timedelta(days=1):
                score += 60
            elif remaining <= timedelta(days=3):
                score += 48
            elif remaining <= timedelta(days=7):
                score += 34
            elif remaining <= timedelta(days=14):
                score += 20
        return score, matches, deadline_tiebreak, str(task.get("name") or "").lower()

    def _task_payload(
        self,
        task: dict[str, Any],
        assigned_ids: set[str],
        profile: dict[str, Any],
        index: int,
    ) -> dict[str, Any]:
        task_id = str(task.get("id") or "")
        priority = str((task.get("priority") or {}).get("priority") or "none")
        status = str((task.get("status") or {}).get("status") or "open")
        location_parts = [
            str((task.get(key) or {}).get("name") or "").strip()
            for key in ("space", "folder", "list")
        ]
        location = " / ".join(part for part in location_parts if part) or "ClickUp workspace"
        space_name = str((task.get("space") or {}).get("name") or "Other").strip() or "Other"
        folder_name = str((task.get("folder") or {}).get("name") or "").strip()
        list_name = str((task.get("list") or {}).get("name") or "Open work").strip() or "Open work"
        contract_name = _task_contract_name(task) or folder_name or "General / overhead"
        due_at = _task_due_datetime(task)
        assignee_names = [
            _clean_text(assignee.get("username") or assignee.get("email") or assignee.get("id"), limit=100)
            for assignee in (task.get("assignees") or [])
            if isinstance(assignee, dict)
        ]
        reasons: list[str] = []
        if task_id in assigned_ids:
            reasons.append("assigned to you")
        if status.lower() in {"in progress", "in_progress", "active"}:
            reasons.append("already moving")
        if priority.lower() in {"urgent", "high"}:
            reasons.append(f"{priority.lower()} priority")
        if due_at:
            remaining = due_at - datetime.now(timezone.utc)
            if remaining.total_seconds() < 0:
                reasons.append(f"overdue since {due_at.date().isoformat()}")
            elif remaining <= timedelta(days=7):
                reasons.append(f"due {due_at.date().isoformat()}")
        text = f"{task.get('name') or ''} {task.get('description') or ''}".lower()
        matches = [
            str(term)
            for term in list(profile.get("skills") or []) + list(profile.get("interests") or [])
            if str(term).strip() and str(term).lower() in text
        ]
        if matches:
            reasons.append("fits " + ", ".join(matches[:2]))
        return {
            "id": task_id,
            "name": str(task.get("name") or "Untitled task"),
            "status": status,
            "priority": priority,
            "location": location,
            "url": str(task.get("url") or ""),
            "reason": ", ".join(reasons) or "recent open workspace work",
            "recommended": index == 0,
            "assigned": task_id in assigned_ids,
            "assignees": assignee_names,
            "due_date": due_at.date().isoformat() if due_at else "",
            "contract": contract_name,
            "space": space_name,
            "list": list_name,
        }

    def _task_catalog(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
        for task in tasks:
            contract_name = str(task.get("contract") or "General / overhead")
            space_name = str(task.get("space") or "Other")
            list_name = str(task.get("list") or "Open work")
            grouped.setdefault(contract_name, {}).setdefault(space_name, {}).setdefault(list_name, []).append(task)

        def group_key(name: str) -> tuple[bool, str]:
            return name == "General / overhead", name.lower()

        catalog: list[dict[str, Any]] = []
        for contract_name in sorted(grouped, key=group_key):
            spaces: list[dict[str, Any]] = []
            for space_name in sorted(grouped[contract_name], key=str.lower):
                lists = [
                    {"name": list_name, "tasks": grouped[contract_name][space_name][list_name]}
                    for list_name in sorted(grouped[contract_name][space_name], key=str.lower)
                ]
                spaces.append(
                    {
                        "name": space_name,
                        "task_count": sum(len(item["tasks"]) for item in lists),
                        "lists": lists,
                    }
                )
            catalog.append(
                {
                    "name": contract_name,
                    "task_count": sum(space["task_count"] for space in spaces),
                    "spaces": spaces,
                }
            )
        return catalog

    async def _coach_plan(
        self,
        slack_user_id: str,
        actor: PortalActor,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        work = state["work"]
        task_name = _clean_text(work.get("selected_task_name"), limit=240)
        if not work.get("selected_task_id") or not task_name:
            raise ValueError("Choose a task first so Don Pollo can shape the plan in the right project context.")
        intent = _clean_text(payload.get("intent"), limit=1_200)
        existing_draft = {
            "outcome": _clean_text(payload.get("outcome"), limit=800),
            "first_step": _clean_text(payload.get("first_step"), limit=500),
            "evidence": _clean_text(payload.get("evidence"), limit=500),
            "estimate": _clean_text(payload.get("estimate"), limit=40),
            "checkpoint": _clean_text(payload.get("checkpoint"), limit=40),
        }
        if not intent and not any(existing_draft.values()):
            raise ValueError("Tell Don Pollo what you plan to accomplish, even in rough language.")
        suggestion = await self.portal_intelligence.coach_plan(
            slack_user_id=slack_user_id,
            worker_name=_actor_name(actor),
            task_name=task_name,
            task_location=_clean_text(work.get("selected_task_location"), limit=240),
            profile=state["profile"],
            intent=intent,
            existing_draft=existing_draft,
        )
        outcome = _clean_text(suggestion.get("outcome"), limit=800) or existing_draft["outcome"]
        first_step = _clean_text(suggestion.get("first_step"), limit=500) or existing_draft["first_step"]
        evidence = _clean_text(suggestion.get("evidence"), limit=500) or existing_draft["evidence"]
        estimate = _clean_text(suggestion.get("estimate"), limit=40).lower()
        checkpoint = _clean_text(suggestion.get("checkpoint"), limit=40).lower()
        if estimate not in _VALID_ESTIMATES:
            estimate = existing_draft["estimate"].lower()
        if estimate not in _VALID_ESTIMATES:
            estimate = "1 hour"
        if checkpoint not in _VALID_CHECKPOINTS:
            checkpoint = existing_draft["checkpoint"].lower()
        if checkpoint not in _VALID_CHECKPOINTS:
            checkpoint = "60 minutes"
        work.update(
            {
                "plan_intent": intent,
                "outcome": outcome,
                "first_step": first_step,
                "evidence": evidence,
                "estimate": estimate,
                "checkpoint": checkpoint,
            }
        )
        issues, _ = validate_work_commitment(outcome, first_step, evidence=evidence)
        # The model may recommend a follow-up, but only the audited validator decides
        # whether the generated fields are sufficient. AI must not become a second,
        # opaque gate after producing a plan that already passes the real rules.
        ready = not issues
        note = _clean_text(suggestion.get("coaching_note"), limit=500)
        question = _clean_text(suggestion.get("follow_up_question"), limit=500)
        if issues and not question:
            question = issues[0]
        if ready:
            question = ""
        state["ai"].update(
            {
                "last_plan_note": note,
                "last_plan_question": question,
                "last_plan_ready": ready,
            }
        )
        if ready:
            return "Don Pollo shaped a concrete draft. Review the details, then start work when they are accurate. No time started yet."
        return (
            "Don Pollo drafted what the facts support, but one detail still needs your answer. "
            + (question or "Review and add the missing concrete detail before starting.")
        )

    async def _coach_checkpoint(
        self,
        slack_user_id: str,
        actor: PortalActor,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        work = state["work"]
        task_name = _clean_text(work.get("selected_task_name"), limit=240)
        if not work.get("selected_task_id") or not task_name:
            raise ValueError("Choose and start a task before shaping a checkpoint.")
        rough_progress = _clean_text(payload.get("progress"), limit=1_200)
        rough_blocker = _clean_text(payload.get("blocker"), limit=600)
        if not rough_progress:
            raise ValueError("Write a rough update first—what changed, failed, passed, or was learned?")
        suggestion = await self.portal_intelligence.coach_checkpoint(
            slack_user_id=slack_user_id,
            worker_name=_actor_name(actor),
            task_name=task_name,
            plan={
                "outcome": _clean_text(work.get("outcome"), limit=800),
                "first_step": _clean_text(work.get("first_step"), limit=500),
                "evidence": _clean_text(work.get("evidence"), limit=500),
            },
            progress=rough_progress,
            blocker=rough_blocker,
        )
        progress = _clean_text(suggestion.get("progress"), limit=800) or rough_progress
        evidence = _clean_text(suggestion.get("evidence"), limit=350)
        next_step = _clean_text(suggestion.get("next_step"), limit=350)
        blocker = _clean_text(suggestion.get("blocker"), limit=500) or rough_blocker
        progress_parts = [progress]
        if evidence:
            progress_parts.append(f"Evidence: {evidence}")
        if next_step:
            progress_parts.append(f"Next: {next_step}")
        structured_progress = _clean_text(" ".join(progress_parts), limit=800)
        issue, _ = validate_meaningful_work_detail(
            structured_progress,
            purpose="progress update",
        )
        ready = issue is None
        note = _clean_text(suggestion.get("coaching_note"), limit=500)
        question = _clean_text(suggestion.get("follow_up_question"), limit=500)
        if issue and not question:
            question = issue
        if ready:
            question = ""
        work["latest_progress"] = structured_progress
        work["latest_blocker"] = blocker
        state["ai"].update(
            {
                "last_checkpoint_note": note,
                "last_checkpoint_question": question,
                "last_checkpoint_ready": ready,
            }
        )
        if ready:
            return "Don Pollo shaped a useful checkpoint draft. Review it, then save it to the work record."
        return (
            "Don Pollo preserved your rough update, but it needs one more fact before saving. "
            + (question or "Add the observable change or result, then save the checkpoint.")
        )

    async def _claim_task(
        self,
        actor: PortalActor,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        if not self.runtime.clickup:
            raise ValueError("ClickUp is unavailable, so this task could not be claimed.")
        task_id = _clean_text(payload.get("task_id"), limit=100)
        if not task_id:
            raise ValueError("Choose a task before claiming it in ClickUp.")
        available_tasks, warning = await self._load_task_options(actor, state)
        if warning:
            raise ValueError("The live ClickUp catalog is not available, so assignment was not changed.")
        candidate = next((task for task in available_tasks if task.get("id") == task_id), None)
        if candidate is None:
            raise ValueError("That task is not in the current open Cislune task catalog.")
        clickup_user_id = actor.clickup_user_id
        if not clickup_user_id:
            resolve_member = getattr(self.runtime.clickup, "resolve_workspace_member_id", None)
            if callable(resolve_member):
                clickup_user_id = await resolve_member(
                    name=_actor_name(actor),
                    email=actor.clickup_user_email,
                )
        if not clickup_user_id:
            raise ValueError("Your Slack profile is not mapped to a ClickUp member yet, so assignment was not changed.")
        if candidate.get("assigned"):
            message = f"{candidate['name']} is already assigned to you in ClickUp."
        else:
            await self.runtime.clickup.update_task_assignees(task_id, add_user_ids=[str(clickup_user_id)])
            self._task_source_cache.pop(str(actor.slack_user_id), None)
            message = f"Claimed {candidate['name']} in ClickUp without removing its other assignees."
        state["work"]["selected_task_id"] = task_id
        state["work"]["selected_task_name"] = str(candidate.get("name") or "Untitled task")
        state["work"]["selected_task_location"] = str(candidate.get("location") or "")
        return message

    def _fallback_tasks(self) -> list[dict[str, Any]]:
        examples = [
            ("beta-project-plan", "Turn the current project into milestones and next actions", "Projects / Planning"),
            ("beta-shop", "Improve shop organization and label the next work area", "Overhead / Shop"),
            ("beta-docs", "Document a repeatable process from recent work", "Overhead / Operations"),
            ("beta-review", "Review a blocked task and propose the smallest unblocker", "Projects / Review"),
            ("beta-cleanup", "Close stale task details and add a clear owner and deadline", "Overhead / Cleanup"),
        ]
        return [
            {
                "id": task_id,
                "name": name,
                "status": "beta example",
                "priority": "normal",
                "location": location,
                "url": "",
                "reason": "safe fallback while ClickUp is unavailable",
                "recommended": index == 0,
            }
            for index, (task_id, name, location) in enumerate(examples)
        ]

    def _save_profile(self, state: dict[str, Any], payload: dict[str, Any]) -> None:
        profile = state["profile"]
        weekly_hours = float(payload.get("weekly_target_hours") or 0)
        if weekly_hours <= 0 or weekly_hours > 80:
            raise ValueError("Weekly target hours must be between 1 and 80.")
        workdays = _clean_list(payload.get("regular_workdays"), limit=7)
        if not workdays:
            raise ValueError("Choose at least one regular workday.")
        profile.update(
            {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "weekly_target_hours": weekly_hours,
                "regular_workdays": workdays,
                "typical_start_time": _clean_clock(payload.get("typical_start_time")),
                "typical_end_time": _clean_clock(payload.get("typical_end_time")),
                "planned_time_off": _clean_list(payload.get("planned_time_off"), limit=30),
                "interests": _clean_list(payload.get("interests"), limit=20),
                "skills": _clean_list(payload.get("skills"), limit=30),
            }
        )

    def _live_enabled(self, actor: PortalActor) -> bool:
        return isinstance(actor, (AdminProfile, UserProfile)) and callable(
            getattr(self.runtime, "get_user_session_for_moment", None)
        )

    def _live_user(self, actor: PortalActor) -> UserProfile:
        if isinstance(actor, UserProfile):
            return actor
        raw_state = self.runtime.state_store.get_operational_state(
            _PORTAL_STATE_PREFIX + str(actor.slack_user_id or "")
        )
        profile = raw_state.get("profile") if isinstance(raw_state, dict) else {}
        profile = profile if isinstance(profile, dict) else {}
        return UserProfile(
            user_key=_actor_user_key(actor),
            display_name=actor.name,
            discord_user_id=actor.discord_user_id,
            storage_folder_name=f"Admin-{actor.name}",
            clickup_user_id=actor.clickup_user_id,
            clickup_user_email=actor.clickup_user_email,
            slack_user_id=actor.slack_user_id,
            preferred_transport="slack",
            worker_type="admin",
            compensation_plan="salary",
            time_tracking_required=True,
            meal_tracking_required=False,
            overtime_approval_required=False,
            weekly_target_hours=float(profile.get("weekly_target_hours") or 40),
            regular_workdays=list(profile.get("regular_workdays") or _DEFAULT_WORKDAYS),
            typical_start_time=str(profile.get("typical_start_time") or "09:00"),
            typical_end_time=str(profile.get("typical_end_time") or "17:00"),
            planned_time_off=list(profile.get("planned_time_off") or []),
            interests=list(profile.get("interests") or ["program management", "project delivery"]),
            skills=list(profile.get("skills") or ["program management"]),
        )

    async def _save_live_profile(
        self,
        actor: PortalActor,
        profile: dict[str, Any],
    ) -> None:
        if not isinstance(actor, UserProfile):
            return
        bootstrap = getattr(self.runtime, "bootstrap", None)
        config = getattr(self.runtime, "config", None)
        config_path = getattr(bootstrap, "agent_config_path", None)
        roster_file_name = str(getattr(config, "roster_file_name", "") or "")
        if config_path is None or not roster_file_name:
            self._update_actor_profile(actor, profile)
            return
        roster_path = config_path.parent / roster_file_name
        if roster_path.suffix.lower() != ".csv":
            raise ValueError("Live worker-context editing currently requires a CSV roster.")
        raw = roster_path.read_bytes()
        rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
        if not rows:
            raise ValueError("The live roster has no worker rows to update.")
        fieldnames = list(rows[0].keys())
        row = next(
            (item for item in rows if str(item.get("user_key") or "").strip() == actor.user_key),
            None,
        )
        if row is None:
            raise ValueError("This worker is no longer present in the live roster.")
        updates = {
            "weekly_target_hours": str(profile["weekly_target_hours"]),
            "regular_workdays": ";".join(profile["regular_workdays"]),
            "typical_start_time": str(profile["typical_start_time"]),
            "typical_end_time": str(profile["typical_end_time"]),
            "planned_time_off": ";".join(profile["planned_time_off"]),
            "interests": ";".join(profile["interests"]),
            "skills": ";".join(profile["skills"]),
        }
        missing = sorted(set(updates) - set(fieldnames))
        if missing:
            raise ValueError("The live roster is missing fields needed by the portal: " + ", ".join(missing))
        row.update(updates)
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        encoded = output.getvalue().encode("utf-8")
        parse_roster_bytes(roster_path.name, encoded)
        prior_mode = stat.S_IMODE(roster_path.stat().st_mode)
        shutil.copy2(roster_path, roster_path.with_suffix(roster_path.suffix + ".portal.bak"))
        atomic_write_text(roster_path, output.getvalue())
        roster_path.chmod(prior_mode)
        refresh = getattr(self.runtime, "refresh_configuration", None)
        if callable(refresh):
            await refresh(force=True)
        else:
            self._update_actor_profile(actor, profile)

    @staticmethod
    def _update_actor_profile(actor: UserProfile, profile: dict[str, Any]) -> None:
        actor.weekly_target_hours = float(profile["weekly_target_hours"])
        actor.regular_workdays = list(profile["regular_workdays"])
        actor.typical_start_time = str(profile["typical_start_time"])
        actor.typical_end_time = str(profile["typical_end_time"])
        actor.planned_time_off = list(profile["planned_time_off"])
        actor.interests = list(profile["interests"])
        actor.skills = list(profile["skills"])

    def _live_session(self, actor: UserProfile) -> tuple[SessionState, datetime]:
        session, now = self.runtime.get_user_session_for_moment(actor)
        return session, now

    async def _start_live_work(
        self,
        actor: PortalActor,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        user = self._live_user(actor)
        work = state["work"]
        task_id = str(work.get("selected_task_id") or "").strip()
        task_name = str(work.get("selected_task_name") or "").strip()
        if not task_id or not task_name:
            raise ValueError("Choose a task before starting work.")
        outcome = _clean_text(payload.get("outcome"), limit=800)
        first_step = _clean_text(payload.get("first_step"), limit=500)
        evidence_provided = "evidence" in payload
        evidence = _clean_text(payload.get("evidence"), limit=500)
        estimate = _clean_text(payload.get("estimate"), limit=40).lower()
        checkpoint = (
            _clean_text(payload.get("checkpoint"), limit=40)
            or _clean_text(state["work"].get("checkpoint"), limit=40)
            or "60 minutes"
        ).lower()
        if estimate not in _VALID_ESTIMATES or checkpoint not in _VALID_CHECKPOINTS:
            raise ValueError("Choose a time estimate and a checkpoint from the available options.")
        session, _ = self._live_session(user)
        if (
            session.stage == "active"
            and not session.clocked_out_at
            and self.runtime._active_task_id(session) == task_id
            and str(session.metadata.get("task_onboarding_tangible_result") or "") == outcome
            and str(session.metadata.get("task_onboarding_plan") or "") == first_step
            and str(session.metadata.get("task_onboarding_evidence") or "") == evidence
        ):
            return "This live task and plan are already running; no duplicate time or Slack update was created."
        if session.metadata.get(_QUALITY_RESTART_BLOCK_KEY):
            raise ValueError(
                "A prior quality warning expired. Submit the corrected plan to a manager and ask Erik or George to run the quality restart approval before work resumes."
            )
        today = session.session_date
        quality = state["quality"]
        if quality.get("date") != today:
            quality.update(
                {
                    "date": today,
                    "weak_attempts": 0,
                    "consecutive_rejections": 0,
                    "last_rejected_fingerprint": "",
                }
            )
        issues, fingerprint = validate_work_commitment(
            outcome,
            first_step,
            evidence=evidence if evidence_provided else None,
            previous_fingerprint=str(quality.get("last_rejected_fingerprint") or ""),
            recent_details=list(quality.get("recent_details") or []),
        )
        if issues:
            warning_started = await self._record_live_quality_rejection(
                actor,
                state,
                fingerprint=fingerprint,
                reasons=issues,
                context="work plan",
            )
            lead = (
                "QUALITY WARNING: correct this within 10 minutes or Don Pollo will clock you out. "
                if warning_started
                else "A little more detail will make this useful. "
            )
            raise ValueError(lead + " ".join(issues))

        # Starting an open task is also the deliberate claim action. Existing co-owners remain assigned.
        await self._claim_task(actor, state, {"task_id": task_id})
        task_name = str(state["work"].get("selected_task_name") or task_name)
        lock = self.runtime._user_session_lock(user.user_key)
        async with lock:
            session, now = self._live_session(user)
            if session.stage == "on_lunch_break":
                raise ValueError("Finish the unpaid lunch period before resuming work.")
            if self.runtime._active_short_rest_break(session):
                raise ValueError("Check back in from the short rest before changing work.")
            if self.runtime._overtime_restart_blocked(session):
                raise ValueError("The overtime limit was reached. A manager must approve more time before work restarts.")
            prior_task_id = self.runtime._active_task_id(session)
            previous = self.runtime._clone_session_state(session)
            if prior_task_id and prior_task_id != task_id:
                await self.runtime._pause_current_task_tracking(
                    user,
                    session,
                    now,
                    set_hold=True,
                    end_reason="portal_task_switch",
                )
            if not session.clocked_in_at:
                session.clocked_in_at = now.isoformat()
            if session.clocked_out_at or session.stage == "clocked_out":
                self.runtime._clear_clock_out_state(session)
                self.runtime._clear_auto_clock_out_metadata(session)
            self.runtime._start_new_work_segment(session, now)
            session.stage = "active"
            session.first_sign_of_life_at = session.first_sign_of_life_at or now.isoformat()
            session.intake_completed_at = session.intake_completed_at or now.isoformat()
            session.latest_plan = f"Result: {outcome}\nFirst move: {first_step}"
            session.metadata.update(
                {
                    "task_onboarding_tangible_result": outcome,
                    "task_onboarding_plan": first_step,
                    "task_onboarding_evidence": evidence,
                    "task_onboarding_estimate": estimate,
                    "task_onboarding_estimated_duration": estimate,
                    "task_onboarding_checkpoint": checkpoint,
                    "clickup_selection_reason": "Selected in live worker portal",
                    "portal_last_started_at": now.isoformat(),
                }
            )
            await self.runtime._activate_clickup_task(user, session, now, task_id, task_name)
            self._schedule_live_checkpoint(session, now, checkpoint, source="portal_start")
            self._accept_quality_detail(
                state,
                session,
                "\n".join((outcome, first_step, evidence)),
            )
            self._append_live_event(
                user,
                session,
                now,
                f"Started {task_name}. Result: {outcome} First move: {first_step} Evidence: {evidence} Estimate: {estimate}; checkpoint: {checkpoint}.",
            )
            await self._notify_live(
                user,
                session,
                now,
                f"Live work started on `{task_name}`. Your ClickUp task timer is running. Planned result: {outcome}",
            )
            await self._persist_live_action(user, session, previous, now, "portal_start")
        quality["strong_plans"] = int(quality.get("strong_plans") or 0) + 1
        return "Live work started. The durable time record and ClickUp task timer are running."

    async def _check_in_live_work(
        self,
        actor: PortalActor,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        user = self._live_user(actor)
        progress = _clean_text(payload.get("progress"), limit=800)
        blocker = _clean_text(payload.get("blocker"), limit=500)
        progress_kind = _clean_text(payload.get("progress_kind"), limit=40).lower() or "made_progress"
        checkpoint = (
            _clean_text(payload.get("checkpoint"), limit=40)
            or _clean_text(state["work"].get("checkpoint"), limit=40)
            or "60 minutes"
        ).lower()
        if progress_kind not in _VALID_PROGRESS_KINDS:
            raise ValueError("Choose one of the quick update options.")
        if checkpoint not in _VALID_CHECKPOINTS:
            raise ValueError("Choose when Don Pollo should check in next.")
        quality = state["quality"]
        current_session, _ = self._live_session(user)
        if quality.get("date") != current_session.session_date:
            quality.update(
                {
                    "date": current_session.session_date,
                    "weak_attempts": 0,
                    "consecutive_rejections": 0,
                    "last_rejected_fingerprint": "",
                }
            )
        if progress_kind == "still_working":
            lock = self.runtime._user_session_lock(user.user_key)
            async with lock:
                session, now = self._live_session(user)
                if session.stage != "active" or session.clocked_out_at:
                    raise ValueError("Start or resume live work before posting a checkpoint.")
                if isinstance(session.metadata.get("still_working_ack"), dict):
                    raise ValueError(
                        "Still working is okay once between concrete updates. This time choose Made progress, Result ready, Blocked, or Changed plan and add one useful fact."
                    )
                previous = self.runtime._clone_session_state(session)
                session.metadata["still_working_ack"] = {
                    "recorded_at": now.isoformat(),
                    "source": "worker_portal",
                }
                self._schedule_live_checkpoint(session, now, checkpoint, source="portal_still_working")
                self._append_live_event(
                    user,
                    session,
                    now,
                    f"Still working acknowledged; next checkpoint: {checkpoint}.",
                )
                await self._persist_live_action(user, session, previous, now, "portal_still_working")
            state["work"]["progress_kind"] = progress_kind
            state["work"]["checkpoint"] = checkpoint
            return "Thanks for checking in. Keep going—no penalty and no extra reminder before your next checkpoint."
        if progress_kind == "blocked":
            if not blocker:
                raise ValueError("Name the blocker, dependency, or decision you need help with.")
            progress = progress or f"Blocked: {blocker}"
        issue, fingerprint = validate_meaningful_work_detail(
            progress,
            purpose="progress update",
            previous_fingerprint=str(quality.get("last_rejected_fingerprint") or ""),
            recent_details=list(quality.get("recent_details") or []),
        )
        if issue:
            warning_started = await self._record_live_quality_rejection(
                actor,
                state,
                fingerprint=fingerprint,
                reasons=[issue],
                context="checkpoint",
            )
            lead = (
                "QUALITY WARNING: correct this within 10 minutes or Don Pollo will clock you out. "
                if warning_started
                else "Please make this checkpoint useful. "
            )
            raise ValueError(lead + issue)
        lock = self.runtime._user_session_lock(user.user_key)
        async with lock:
            session, now = self._live_session(user)
            if session.stage != "active" or session.clocked_out_at:
                raise ValueError("Start or resume live work before posting a checkpoint.")
            previous = self.runtime._clone_session_state(session)
            session.latest_status = progress
            session.latest_blocker = blocker or None
            session.metadata["checkpoint_status"] = progress_kind
            if progress_kind == "changed_plan":
                session.metadata["latest_plan_change"] = {
                    "changed_at": now.isoformat(),
                    "detail": progress,
                }
            self._schedule_live_checkpoint(session, now, checkpoint, source="portal_checkpoint")
            self._record_live_checkpoint_quality(session, now, meaningful=True)
            self._accept_quality_detail(state, session, progress)
            self._append_live_event(
                user,
                session,
                now,
                f"Checkpoint ({progress_kind.replace('_', ' ')}): {progress}"
                + (f" Blocker: {blocker}" if blocker else "")
                + f" Next checkpoint: {checkpoint}.",
            )
            await self._persist_live_action(user, session, previous, now, "portal_checkpoint")
        state["work"]["progress_kind"] = progress_kind
        state["work"]["checkpoint"] = checkpoint
        return {
            "result_ready": "Result-ready update saved. The task stays live until you finish or switch it deliberately.",
            "blocked": "Blocker saved to the durable work record so Don Pollo can route useful help.",
            "changed_plan": "Plan change saved with the reason and next checkpoint.",
        }.get(progress_kind, "Checkpoint saved to the durable work record and queued for ClickUp sync.")

    async def _record_live_quality_rejection(
        self,
        actor: PortalActor,
        state: dict[str, Any],
        *,
        fingerprint: str,
        reasons: list[str],
        context: str,
    ) -> bool:
        quality = state["quality"]
        quality["weak_attempts"] = int(quality.get("weak_attempts") or 0) + 1
        quality["consecutive_rejections"] = int(quality.get("consecutive_rejections") or 0) + 1
        quality["last_rejected_fingerprint"] = fingerprint
        repeated = any(
            "same answer" in reason.lower()
            or "copy" in reason.lower()
            or "identical" in reason.lower()
            for reason in reasons
        )
        user = self._live_user(actor)
        lock = self.runtime._user_session_lock(user.user_key)
        async with lock:
            session, now = self._live_session(user)
            if session.stage != "active" or session.clocked_out_at:
                return False
            previous = self.runtime._clone_session_state(session)
            existing = session.metadata.get(_QUALITY_WARNING_KEY)
            existing = existing if isinstance(existing, dict) else {}
            if existing.get("deadline_at"):
                quality["warning_deadline_at"] = str(existing.get("deadline_at") or "")
                quality["warning_reasons"] = list(existing.get("reasons") or reasons)
                return True
            # One coached miss per workday is allowed. A second miss, or a
            # high-confidence repeat/copy, opens the visible correction window.
            if int(quality["weak_attempts"]) < 2 and not repeated:
                return False
            deadline = now + timedelta(minutes=_QUALITY_WARNING_MINUTES)
            warning = {
                "status": "pending_correction",
                "started_at": now.isoformat(),
                "deadline_at": deadline.isoformat(),
                "context": context,
                "reasons": list(dict.fromkeys(reasons))[:4],
            }
            session.metadata[_QUALITY_WARNING_KEY] = warning
            quality["warning_deadline_at"] = deadline.isoformat()
            quality["warning_reasons"] = list(warning["reasons"])
            self._append_live_event(
                user,
                session,
                now,
                f"Quality warning opened for rejected {context}: " + " ".join(reasons),
            )
            await self._notify_live(
                user,
                session,
                now,
                (
                    "QUALITY WARNING: your submitted work detail was rejected because it was vague or repeated. "
                    "Submit a specific result/checkpoint and evidence in the worker website within 10 minutes. "
                    "If it is not corrected, Don Pollo will stop future time at the deadline. "
                    "Time already recorded remains intact. Stop work at the deadline and contact Erik or George."
                ),
            )
            await self._persist_live_action(user, session, previous, now, "portal_quality_warning")
            return True

    @staticmethod
    def _accept_quality_detail(
        state: dict[str, Any],
        session: SessionState,
        detail: str,
    ) -> None:
        quality = state["quality"]
        cleaned = _clean_text(detail, limit=1600)
        recent = [cleaned] + [
            str(item)
            for item in list(quality.get("recent_details") or [])
            if str(item).strip() and _normalize_detail(str(item)) != _normalize_detail(cleaned)
        ]
        quality["recent_details"] = recent[:20]
        quality["consecutive_rejections"] = 0
        quality["last_rejected_fingerprint"] = ""
        quality["warning_deadline_at"] = ""
        quality["warning_reasons"] = []
        session.metadata.pop(_QUALITY_WARNING_KEY, None)

    async def _start_live_short_rest(self, actor: PortalActor) -> str:
        user = self._live_user(actor)
        lock = self.runtime._user_session_lock(user.user_key)
        async with lock:
            session, now = self._live_session(user)
            previous = self.runtime._clone_session_state(session)
            if session.stage != "active":
                raise ValueError("Short rest is available only while actively working.")
            await self.runtime._maybe_start_short_rest_break(None, user, session, now)
            await self._persist_live_action(user, session, previous, now, "portal_short_rest")
        return "Paid short rest started. Check back in within 10 minutes or Don Pollo will clock you out at the cutoff."

    async def _start_live_lunch(self, actor: PortalActor) -> str:
        user = self._live_user(actor)
        lock = self.runtime._user_session_lock(user.user_key)
        async with lock:
            session, now = self._live_session(user)
            previous = self.runtime._clone_session_state(session)
            if session.stage != "active":
                raise ValueError("Lunch is available only while actively working.")
            await self.runtime._maybe_start_lunch_break(None, user, session, now)
            await self._persist_live_action(user, session, previous, now, "portal_lunch")
        return "Unpaid lunch started. Work and ClickUp time are paused for at least 30 minutes."

    async def _return_from_live_break(self, actor: PortalActor) -> str:
        user = self._live_user(actor)
        lock = self.runtime._user_session_lock(user.user_key)
        async with lock:
            session, now = self._live_session(user)
            previous = self.runtime._clone_session_state(session)
            if self.runtime._active_short_rest_break(session):
                self.runtime._finish_short_rest_break(session, now, outcome="returned_via_portal")
                await self._notify_live(
                    user,
                    session,
                    now,
                    "Welcome back. Your paid short rest ended and live task time continues.",
                )
                result = "Checked back in. Live task time continues."
            elif session.stage == "on_lunch_break":
                await self.runtime._end_lunch_break(None, user, session, now)
                if session.stage == "on_lunch_break":
                    raise ValueError("The unpaid lunch minimum has not ended yet. Don Pollo sent the remaining time in Slack.")
                result = "Checked back in. Live work and ClickUp task time resumed."
            else:
                raise ValueError("There is no active break to return from.")
            await self._persist_live_action(user, session, previous, now, "portal_break_return")
        return result

    async def _clock_out_live(self, actor: PortalActor, payload: dict[str, Any]) -> str:
        user = self._live_user(actor)
        lock = self.runtime._user_session_lock(user.user_key)
        async with lock:
            session, now = self._live_session(user)
            if session.stage == "clocked_out" or session.clocked_out_at:
                raise ValueError("You are already clocked out.")
            progress = _clean_text(payload.get("progress"), limit=800) or str(session.latest_status or "")
            blocker = _clean_text(payload.get("blocker"), limit=500) or str(session.latest_blocker or "")
            issue, _ = validate_meaningful_work_detail(progress, purpose="clock-out result")
            if issue:
                raise ValueError("Before clocking out, " + issue[0].lower() + issue[1:])
            previous = self.runtime._clone_session_state(session)
            session.latest_status = progress
            session.latest_blocker = blocker or None
            if self.runtime._active_short_rest_break(session):
                self.runtime._finish_short_rest_break(session, now, outcome="manual_clock_out_via_portal")
            note = await self.runtime._finalize_clickup_day(
                user,
                session,
                now,
                allow_status_completion=False,
                include_next_task_suggestion=False,
                pause_reason="portal_clock_out",
            )
            session.clocked_out_at = now.isoformat()
            self.runtime._close_current_work_segment(session, now)
            session.stage = "clocked_out"
            session.awaiting_clock_out_photo = False
            session.awaiting_clock_out_summary = False
            session.pending_clickup_sync = True
            session.metadata["clock_out_source"] = "worker_portal"
            self._append_live_event(
                user,
                session,
                now,
                f"Clocked out. Result: {progress}" + (f" Blocker: {blocker}" if blocker else ""),
            )
            await self._notify_live(
                user,
                session,
                now,
                "You are clocked out. Your result is saved and the task timer is stopped."
                + (f"\n\n{note}" if note else ""),
            )
            await self._persist_live_action(user, session, previous, now, "portal_clock_out")
        return "Clocked out. The durable time record and task timer are closed."

    async def _enforce_live_deadlines(self, actor: PortalActor) -> None:
        user = self._live_user(actor)
        lock = self.runtime._user_session_lock(user.user_key)
        async with lock:
            session, now = self._live_session(user)
            previous = self.runtime._clone_session_state(session)
            enforce_quality = getattr(self.runtime, "_maybe_enforce_portal_quality_warning", None)
            changed = bool(
                callable(enforce_quality)
                and await enforce_quality(None, user, session, now)
            )
            if not changed:
                changed = await self.runtime._maybe_check_short_rest_break(None, user, session, now)
            if changed:
                await self._persist_live_action(
                    user,
                    session,
                    previous,
                    now,
                    "portal_deadline_enforcement",
                )

    def _sync_state_from_live_session(
        self,
        actor: PortalActor,
        state: dict[str, Any],
    ) -> None:
        user = self._live_user(actor)
        session, _ = self._live_session(user)
        work = state["work"]
        active_short_rest = self.runtime._active_short_rest_break(session)
        if session.stage == "on_lunch_break":
            status_value = "lunch"
        elif active_short_rest:
            status_value = "short_rest"
        elif session.stage == "clocked_out" or session.clocked_out_at:
            status_value = "clocked_out"
        elif session.stage == "active" and session.clocked_in_at:
            status_value = "active"
        else:
            status_value = "ready"
        active_task_id = str(self.runtime._active_task_id(session) or "")
        active_task_name = str(session.metadata.get("active_clickup_task_name") or "")
        lunch_started = str(session.metadata.get("lunch_started_at") or "")
        quality_warning = session.metadata.get(_QUALITY_WARNING_KEY)
        quality_warning = quality_warning if isinstance(quality_warning, dict) else {}
        minimum_end = ""
        if lunch_started:
            parsed = _parse_datetime(lunch_started)
            if parsed:
                minimum_end = (
                    parsed + timedelta(minutes=self.runtime.config.labor.meal_minimum_minutes)
                ).isoformat()
        work.update(
            {
                "status": status_value,
                "selected_task_id": (
                    active_task_id
                    if status_value in {"active", "short_rest", "lunch"}
                    else work.get("selected_task_id", "")
                ),
                "selected_task_name": (
                    active_task_name
                    if status_value in {"active", "short_rest", "lunch"}
                    else work.get("selected_task_name", "")
                ),
                "outcome": str(session.metadata.get("task_onboarding_tangible_result") or work.get("outcome") or ""),
                "first_step": str(session.metadata.get("task_onboarding_plan") or work.get("first_step") or ""),
                "evidence": str(session.metadata.get("task_onboarding_evidence") or work.get("evidence") or ""),
                "estimate": str(session.metadata.get("task_onboarding_estimate") or work.get("estimate") or "1 hour"),
                "checkpoint": str(session.metadata.get("task_onboarding_checkpoint") or work.get("checkpoint") or "60 minutes"),
                "checkpoint_due_at": str(
                    (session.metadata.get("worker_checkpoint") or {}).get("due_at") or ""
                    if isinstance(session.metadata.get("worker_checkpoint"), dict)
                    else ""
                ),
                "progress_kind": str(session.metadata.get("checkpoint_status") or work.get("progress_kind") or "made_progress"),
                "started_at": str(session.clocked_in_at or ""),
                "break_started_at": str((active_short_rest or {}).get("started_at") or lunch_started),
                "break_cutoff_at": str((active_short_rest or {}).get("deadline_at") or ""),
                "break_minimum_end_at": minimum_end,
                "latest_progress": str(session.latest_status or ""),
                "latest_blocker": str(session.latest_blocker or ""),
                "notice": self._live_notice(status_value, quality_warning=quality_warning),
            }
        )
        quality = state["quality"]
        quality["warning_deadline_at"] = str(quality_warning.get("deadline_at") or "")
        quality["warning_reasons"] = list(quality_warning.get("reasons") or [])
        quality["auto_clocked_out_at"] = str(
            (session.metadata.get(_QUALITY_RESTART_BLOCK_KEY) or {}).get("clocked_out_at")
            if isinstance(session.metadata.get(_QUALITY_RESTART_BLOCK_KEY), dict)
            else ""
        )

    @staticmethod
    def _live_notice(status_value: str, *, quality_warning: dict[str, Any] | None = None) -> str:
        if quality_warning and quality_warning.get("deadline_at"):
            return "A quality correction is due now. Submit concrete progress before the visible deadline."
        return {
            "active": "Live work and ClickUp task time are running.",
            "short_rest": "Paid short rest is live. Check back in before the 10-minute cutoff.",
            "lunch": "Unpaid lunch is live. Work and ClickUp time are paused.",
            "clocked_out": "You are clocked out; task time is stopped.",
        }.get(status_value, "Choose a task and write a concrete result to begin live work.")

    def _append_live_event(
        self,
        actor: UserProfile,
        session: SessionState,
        now: datetime,
        content: str,
    ) -> None:
        author_id = self.runtime._stable_external_author_id(
            f"portal:{actor.slack_user_id or actor.user_key}"
        )
        self.runtime.state_store.append_message(
            actor.user_key,
            session.session_date,
            MessageRecord(
                message_id=f"portal:{secrets.token_hex(12)}",
                direction="inbound",
                author_id=author_id,
                created_at=now,
                content=content,
                attachments=[],
            ),
        )
        self.runtime._touch_inbound_session(session, now)
        session.first_sign_of_life_at = session.first_sign_of_life_at or now.isoformat()

    async def _notify_live(
        self,
        actor: UserProfile,
        session: SessionState,
        now: datetime,
        message: str,
    ) -> None:
        try:
            await self.runtime._send_dm(None, actor, session, message, now)
        except Exception as exc:
            session.metadata["portal_slack_notification_error"] = str(exc)[:300]

    async def _persist_live_action(
        self,
        actor: UserProfile,
        session: SessionState,
        previous: SessionState,
        now: datetime,
        trigger: str,
    ) -> None:
        await self.runtime._persist_session_state(
            actor,
            session,
            now=now,
            previous_session=previous,
            trigger=trigger,
            details={"source": "worker_portal"},
        )
        write_dashboard = getattr(self.runtime, "write_dashboard", None)
        if callable(write_dashboard):
            await write_dashboard()

    def _schedule_live_checkpoint(
        self,
        session: SessionState,
        now: datetime,
        checkpoint: str,
        *,
        source: str,
    ) -> None:
        scheduler = getattr(self.runtime, "_set_worker_checkpoint", None)
        if callable(scheduler):
            scheduler(session, now, checkpoint, source=source)
            return
        normalized = " ".join(str(checkpoint or "").strip().lower().split())
        minutes = {
            "30 minutes": 30,
            "60 minutes": 60,
            "90 minutes": 90,
            "2 hours": 120,
            "when result is ready": 120,
        }.get(normalized, 60)
        session.metadata["worker_checkpoint"] = {
            "choice": normalized or "60 minutes",
            "set_at": now.isoformat(),
            "due_at": (now + timedelta(minutes=minutes)).isoformat(),
            "interval_minutes": minutes,
            "source": source,
            "reminder_sent": False,
        }
        session.metadata["task_onboarding_checkpoint"] = normalized or "60 minutes"
        session.metadata.pop("pending_follow_up", None)
        session.metadata.pop("follow_up_response_aggregation", None)

    def _record_live_checkpoint_quality(
        self,
        session: SessionState,
        now: datetime,
        *,
        meaningful: bool,
    ) -> None:
        recorder = getattr(self.runtime, "_record_checkpoint_quality", None)
        if callable(recorder):
            recorder(
                session,
                now,
                meaningful=meaningful,
                source="worker_portal",
            )
            return
        history = session.metadata.get("checkpoint_quality_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "recorded_at": now.isoformat(),
                "meaningful": bool(meaningful),
                "source": "worker_portal",
            }
        )
        session.metadata["checkpoint_quality_history"] = history[-10:]
        if meaningful:
            session.metadata.pop("still_working_ack", None)

    def _start_work(self, state: dict[str, Any], payload: dict[str, Any]) -> str:
        work = state["work"]
        if not work.get("selected_task_id"):
            raise ValueError("Choose a task before starting the timer.")
        outcome = _clean_text(payload.get("outcome"), limit=800)
        first_step = _clean_text(payload.get("first_step"), limit=500)
        evidence_provided = "evidence" in payload
        evidence = _clean_text(payload.get("evidence"), limit=500)
        estimate = _clean_text(payload.get("estimate"), limit=40).lower()
        checkpoint = (
            _clean_text(payload.get("checkpoint"), limit=40)
            or _clean_text(work.get("checkpoint"), limit=40)
            or "60 minutes"
        ).lower()
        if estimate not in _VALID_ESTIMATES or checkpoint not in _VALID_CHECKPOINTS:
            raise ValueError("Choose a time estimate and a checkpoint from the available options.")
        today = datetime.now(timezone.utc).date().isoformat()
        quality = state["quality"]
        if quality.get("date") != today:
            quality.update({"date": today, "weak_attempts": 0, "last_rejected_fingerprint": ""})
        issues, fingerprint = validate_work_commitment(
            outcome,
            first_step,
            evidence=evidence if evidence_provided else None,
            previous_fingerprint=str(quality.get("last_rejected_fingerprint") or ""),
            recent_details=list(quality.get("recent_details") or []),
        )
        if issues:
            quality["weak_attempts"] = int(quality.get("weak_attempts") or 0) + 1
            quality["last_rejected_fingerprint"] = fingerprint
            attempts = int(quality["weak_attempts"])
            prefix = "A little more detail will make this useful."
            if attempts >= 2:
                prefix = "I’m seeing another vague or repeated answer."
            if attempts >= 3:
                prefix = "This is the third incomplete attempt today; a manager review would be requested before starting."
            explanation = (
                " Clear details give the project a finish line, make handoffs possible, and let Don Pollo help instead of merely collecting time."
            )
            raise ValueError(prefix + explanation + " " + " ".join(issues))
        work.update(
            {
                "status": "active",
                "outcome": outcome,
                "first_step": first_step,
                "evidence": evidence,
                "estimate": estimate,
                "checkpoint": checkpoint,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "break_started_at": "",
                "break_minimum_end_at": "",
                "break_cutoff_at": "",
                "notice": "Beta timer started. Live worker and ClickUp time were not changed.",
            }
        )
        quality["strong_plans"] = int(quality.get("strong_plans") or 0) + 1
        quality["last_rejected_fingerprint"] = ""
        quality["consecutive_rejections"] = 0
        quality["recent_details"] = (["\n".join((outcome, first_step, evidence))] + list(quality.get("recent_details") or []))[:20]
        return "Strong plan. The beta timer is running, and your next checkpoint is visible."

    def _check_in(self, state: dict[str, Any], payload: dict[str, Any]) -> str:
        work = state["work"]
        if work.get("status") != "active":
            raise ValueError("Start or resume work before posting a checkpoint.")
        progress = _clean_text(payload.get("progress"), limit=800)
        blocker = _clean_text(payload.get("blocker"), limit=500)
        progress_kind = _clean_text(payload.get("progress_kind"), limit=40).lower() or "made_progress"
        checkpoint = (
            _clean_text(payload.get("checkpoint"), limit=40)
            or _clean_text(work.get("checkpoint"), limit=40)
            or "60 minutes"
        ).lower()
        if progress_kind not in _VALID_PROGRESS_KINDS:
            raise ValueError("Choose one of the quick update options.")
        if checkpoint not in _VALID_CHECKPOINTS:
            raise ValueError("Choose when Don Pollo should check in next.")
        if progress_kind == "still_working":
            if work.get("still_working_acknowledged"):
                raise ValueError(
                    "Still working is okay once between concrete updates. Add one useful fact this time."
                )
            work["still_working_acknowledged"] = True
            work["progress_kind"] = progress_kind
            work["checkpoint"] = checkpoint
            work["checkpoint_due_at"] = (
                datetime.now(timezone.utc)
                + timedelta(
                    minutes={
                        "30 minutes": 30,
                        "60 minutes": 60,
                        "90 minutes": 90,
                        "2 hours": 120,
                        "when result is ready": 120,
                    }.get(checkpoint, 60)
                )
            ).isoformat()
            work["notice"] = "Still working acknowledged. No extra reminder before the next checkpoint."
            return "Thanks for checking in. Keep going—no penalty."
        if progress_kind == "blocked":
            if not blocker:
                raise ValueError("Name the blocker, dependency, or decision you need help with.")
            progress = progress or f"Blocked: {blocker}"
        if _is_vague(progress, minimum_words=5, minimum_characters=24):
            raise ValueError(
                "Name what changed, what now exists, or what you learned. This keeps updates useful to the next person reading them."
            )
        work["latest_progress"] = progress
        work["latest_blocker"] = blocker
        work["progress_kind"] = progress_kind
        work["checkpoint"] = checkpoint
        work["still_working_acknowledged"] = False
        work["checkpoint_due_at"] = (
            datetime.now(timezone.utc)
            + timedelta(
                minutes={
                    "30 minutes": 30,
                    "60 minutes": 60,
                    "90 minutes": 90,
                    "2 hours": 120,
                    "when result is ready": 120,
                }.get(checkpoint, 60)
            )
        ).isoformat()
        work["notice"] = "Checkpoint captured."
        return "Checkpoint captured with meaningful progress detail."

    def _start_short_rest(self, state: dict[str, Any]) -> str:
        work = state["work"]
        if work.get("status") != "active":
            raise ValueError("Short rest is available while actively working.")
        now = datetime.now(timezone.utc)
        work.update(
            {
                "status": "short_rest",
                "break_started_at": now.isoformat(),
                "break_cutoff_at": (now + timedelta(minutes=10)).isoformat(),
                "break_minimum_end_at": "",
                "notice": "Paid short rest started. Check back in within 10 minutes.",
            }
        )
        return "Paid short rest started. Return within 10 minutes or the beta clock stops at the cutoff."

    def _start_lunch(self, state: dict[str, Any]) -> str:
        work = state["work"]
        if work.get("status") != "active":
            raise ValueError("Lunch is available while actively working.")
        now = datetime.now(timezone.utc)
        work.update(
            {
                "status": "lunch",
                "break_started_at": now.isoformat(),
                "break_minimum_end_at": (now + timedelta(minutes=30)).isoformat(),
                "break_cutoff_at": "",
                "notice": "Unpaid lunch started. Project time is paused for at least 30 minutes.",
            }
        )
        return "Unpaid lunch started. The beta timer is paused and cannot resume for 30 minutes."

    def _return_from_break(self, state: dict[str, Any]) -> str:
        work = state["work"]
        status = str(work.get("status") or "")
        if status not in {"short_rest", "lunch"}:
            raise ValueError("There is no active break to return from.")
        if status == "lunch":
            minimum = _parse_datetime(work.get("break_minimum_end_at"))
            now = datetime.now(timezone.utc)
            if minimum and now < minimum:
                remaining = max(1, int((minimum - now).total_seconds() // 60) + 1)
                raise ValueError(f"Lunch is unpaid and must remain paused for {remaining} more minute(s).")
        work.update(
            {
                "status": "active",
                "break_started_at": "",
                "break_minimum_end_at": "",
                "break_cutoff_at": "",
                "notice": "Checked back in. Beta project time resumed.",
            }
        )
        return "Checked back in. The beta project timer is running again."

    def _clock_out(self, state: dict[str, Any]) -> str:
        work = state["work"]
        if work.get("status") in {"ready", "clocked_out"}:
            raise ValueError("The beta timer is not running.")
        work.update(
            {
                "status": "clocked_out",
                "break_started_at": "",
                "break_minimum_end_at": "",
                "break_cutoff_at": "",
                "notice": "Beta clock stopped. No live payroll or ClickUp entry was created.",
            }
        )
        return "Beta clock stopped. Share the summary to Slack if you want to test the handoff."

    async def _request_task(self, slack_user_id: str, state: dict[str, Any], payload: dict[str, Any]) -> str:
        title = _clean_text(payload.get("title"), limit=180)
        reason = _clean_text(payload.get("reason"), limit=600)
        task_type = _clean_text(payload.get("task_type"), limit=40).lower()
        if len(title) < 12 or len(reason) < 24:
            raise ValueError("Give the proposed task a clear title and explain the result or problem it addresses.")
        if task_type not in {"project", "overhead"}:
            raise ValueError("Choose project or overhead for the proposed task.")
        request = {
            "id": secrets.token_hex(5),
            "title": title,
            "reason": reason,
            "task_type": task_type,
            "status": "pending approval",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        state["task_requests"] = [request] + list(state.get("task_requests") or [])[:9]
        await self._post_slack(
            slack_user_id,
            (
                f"Don Pollo portal task request (pending approval; no ClickUp task created yet)\n"
                f"• {title}\n• Type: {task_type}\n• Why: {reason}\n"
                "Erik or George can approve this before it becomes tracked work."
            ),
        )
        return "Task request saved and sent to Slack for Erik or George to approve."

    async def _share_to_slack(self, slack_user_id: str, state: dict[str, Any]) -> str:
        work = state["work"]
        task_name = str(work.get("selected_task_name") or "No task selected")
        actor = resolve_worker_portal_actor(self.runtime, slack_user_id)
        live = actor is not None and self._live_enabled(actor)
        lines = [
            "Don Pollo live work summary" if live else "Don Pollo admin-preview summary",
            f"• Task: {task_name}",
            f"• Intended result: {work.get('outcome') or 'not entered'}",
            f"• First step: {work.get('first_step') or 'not entered'}",
            f"• Evidence: {work.get('evidence') or 'not entered'}",
            f"• Estimate / checkpoint: {work.get('estimate') or '—'} / {work.get('checkpoint') or '—'}",
        ]
        if work.get("latest_progress"):
            lines.append(f"• Progress: {work['latest_progress']}")
        if work.get("latest_blocker"):
            lines.append(f"• Blocker: {work['latest_blocker']}")
        if live:
            lines.append("This reflects the same durable work session and ClickUp timer used by Don Pollo in Slack.")
        else:
            lines.append("Admin preview only: no live time or payroll record was changed.")
        await self._post_slack(slack_user_id, "\n".join(lines))
        return "A concise live summary was sent to your Slack DM." if live else "A preview summary was sent to Slack."

    async def _post_slack(self, slack_user_id: str, message: str) -> None:
        if not self.runtime.slack:
            raise ValueError("Slack is unavailable right now; the portal update was not sent.")
        await self.runtime.slack.post_message(slack_user_id, message)

    def _advance_deadlines(self, state: dict[str, Any]) -> None:
        work = state["work"]
        if work.get("status") != "short_rest":
            return
        cutoff = _parse_datetime(work.get("break_cutoff_at"))
        if cutoff and datetime.now(timezone.utc) >= cutoff:
            work.update(
                {
                    "status": "clocked_out",
                    "break_started_at": "",
                    "break_minimum_end_at": "",
                    "break_cutoff_at": "",
                    "notice": "The 10-minute short-rest window passed without check-in, so the beta clock stopped at the cutoff.",
                }
            )

    def _payload(
        self,
        actor: PortalActor,
        state: dict[str, Any],
        tasks: list[dict[str, Any]],
        task_warning: str,
    ) -> dict[str, Any]:
        work = state["work"]
        now = datetime.now(timezone.utc)
        minimum = _parse_datetime(work.get("break_minimum_end_at"))
        cutoff = _parse_datetime(work.get("break_cutoff_at"))
        ai_status = self.portal_intelligence.snapshot(str(actor.slack_user_id or ""))
        return {
            "beta": True,
            "live": self._live_enabled(actor),
            "actor": {"name": _actor_name(actor), "slack_user_id": actor.slack_user_id},
            "profile": state["profile"],
            "work": work,
            "time": self._time_payload(actor, work, now),
            "quality": state["quality"],
            "ai": {**ai_status, **state["ai"]},
            "overhead_lanes": _OVERHEAD_LANES,
            "task_options": tasks[:5],
            "task_catalog": self._task_catalog(tasks),
            "task_total": len(tasks),
            "task_warning": task_warning,
            "task_requests": list(state.get("task_requests") or [])[:5],
            "history": list(state.get("history") or [])[:8],
            "break": {
                "can_return": not minimum or now >= minimum,
                "minimum_end_at": minimum.isoformat() if minimum else "",
                "cutoff_at": cutoff.isoformat() if cutoff else "",
            },
            "generated_at": now.isoformat(),
        }

    def _time_payload(
        self,
        actor: PortalActor,
        work: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        empty = {
            "session_date": "",
            "clocked_in_at": "",
            "clocked_out_at": "",
            "worked_seconds": 0,
            "gross_seconds": 0,
            "unpaid_lunch_seconds": 0,
            "tracked_seconds": 0,
            "current_task_seconds": 0,
            "work_clock_running": False,
            "task_timer_running": False,
            "current_task_name": str(work.get("selected_task_name") or ""),
        }
        if not self._live_enabled(actor):
            return empty

        session, _ = self._live_session(self._live_user(actor))
        refresh_summary = getattr(self.runtime, "_refresh_session_time_summary", None)
        if callable(refresh_summary):
            refresh_summary(session, now)
        summary = session.time_summary if isinstance(session.time_summary, dict) else {}
        tracking = session.metadata.get("clickup_time_tracking")
        tracking = tracking if isinstance(tracking, dict) and not tracking.get("closed_at") else {}
        current_started_at = _parse_datetime(tracking.get("started_at"))
        current_task_seconds = (
            max(0, int((now - current_started_at).total_seconds()))
            if current_started_at
            else 0
        )
        status_value = str(work.get("status") or "ready")
        work_segment_open = bool(summary.get("has_open_work_segment")) or any(
            isinstance(segment, dict)
            and segment.get("clocked_in_at")
            and not segment.get("clocked_out_at")
            for segment in session.work_segments
        )
        task_timer_running = (
            bool(summary.get("active_task_timer_running") or tracking)
            and status_value in {"active", "short_rest"}
        )

        def whole_seconds(key: str) -> int:
            try:
                return max(0, int(float(summary.get(key) or 0)))
            except (TypeError, ValueError):
                return 0

        return {
            "session_date": str(session.session_date or ""),
            "clocked_in_at": str(session.clocked_in_at or ""),
            "clocked_out_at": str(session.clocked_out_at or ""),
            "worked_seconds": whole_seconds("clocked_in_total_seconds"),
            "gross_seconds": whole_seconds("gross_clocked_in_total_seconds"),
            "unpaid_lunch_seconds": whole_seconds("unpaid_lunch_deducted_seconds"),
            "tracked_seconds": whole_seconds("task_tracked_total_seconds"),
            "current_task_seconds": current_task_seconds,
            "work_clock_running": work_segment_open and status_value in {"active", "short_rest"},
            "task_timer_running": task_timer_running,
            "current_task_name": str(
                tracking.get("task_name")
                or work.get("selected_task_name")
                or session.metadata.get("active_clickup_task_name")
                or ""
            ),
        }

    def _record_history(self, state: dict[str, Any], action: str, message: str) -> None:
        state["history"] = [
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "action": action,
                "message": message,
            }
        ] + list(state.get("history") or [])[:39]

    def _save_state(self, slack_user_id: str, state: dict[str, Any]) -> None:
        self.runtime.state_store.set_operational_state(_PORTAL_STATE_PREFIX + slack_user_id, state)


def _issue_token(runtime: Any, subject: str, now: datetime, *, ttl_hours: int) -> str:
    state = runtime.state_store.get_operational_state(_PORTAL_SECRET_STATE_KEY) or {}
    secret = str(state.get("secret") or "")
    if not secret:
        secret = secrets.token_urlsafe(48)
        runtime.state_store.set_operational_state(
            _PORTAL_SECRET_STATE_KEY,
            {"secret": secret, "created_at": now.isoformat()},
        )
    payload_bytes = json.dumps(
        {"sub": subject, "exp": int((now + timedelta(hours=max(1, ttl_hours))).timestamp())},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    return f"{_encode_urlsafe(payload_bytes)}.{_encode_urlsafe(signature)}"


def _encode_urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_urlsafe(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _clean_text(value: Any, *, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_list(value: Any, *, limit: int) -> list[str]:
    raw = value if isinstance(value, list) else re.split(r"[,;\n]", str(value or ""))
    return list(dict.fromkeys(_clean_text(item, limit=100) for item in raw if _clean_text(item, limit=100)))[:limit]


def _clean_clock(value: Any) -> str:
    text = _clean_text(value, limit=5)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError("Use a 24-hour time such as 09:00 or 17:30.")
    return text


def _task_contract_name(task: dict[str, Any]) -> str:
    for field in task.get("custom_fields") or []:
        if not isinstance(field, dict):
            continue
        field_name = str(field.get("name") or "").strip().lower()
        if not any(term in field_name for term in ("contract", "program", "award", "customer")):
            continue
        value = field.get("value")
        options = ((field.get("type_config") or {}).get("options") or [])
        for option in options:
            if not isinstance(option, dict):
                continue
            option_values = {str(option.get(key)) for key in ("id", "orderindex")}
            if str(value) in option_values:
                return _clean_text(option.get("name") or option.get("label"), limit=100)
        if isinstance(value, dict):
            return _clean_text(value.get("name") or value.get("label"), limit=100)
        if isinstance(value, (str, int, float)):
            return _clean_text(value, limit=100)
    return ""


def _task_due_datetime(task: dict[str, Any]) -> datetime | None:
    try:
        timestamp = int(str(task.get("due_date") or ""))
    except ValueError:
        return None
    if timestamp < 100_000_000_000:
        timestamp *= 1000
    try:
        return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _is_vague(value: str, *, minimum_words: int, minimum_characters: int) -> bool:
    normalized = _clean_text(value, limit=1000).lower().strip(" .!?")
    words = re.findall(r"[a-z0-9][a-z0-9'-]*", normalized)
    generic_pattern = re.fullmatch(
        r"(?:i (?:will|am going to|plan to) )?(?:continue (?:to )?)?(?:work(?:ing)? on|make progress on|do|finish|complete) (?:it|this|the )?(?:task|project|work)?",
        normalized,
    )
    return (
        not normalized
        or normalized in _GENERIC_WORK_REPLIES
        or generic_pattern is not None
        or len(normalized) < minimum_characters
        or len(set(words)) < minimum_words
    )


def _normalize_detail(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9][a-z0-9'-]*", _clean_text(value, limit=2000).lower()))


def _duplicate_detail_issue(value: str, recent_details: list[str]) -> str | None:
    normalized = _normalize_detail(value)
    if not normalized:
        return None
    current_tokens = set(normalized.split())
    for prior in recent_details[-20:]:
        prior_normalized = _normalize_detail(str(prior))
        if not prior_normalized:
            continue
        if normalized == prior_normalized:
            return (
                "This copies a previous submission. Describe what is different now and name the new evidence."
            )
        prior_tokens = set(prior_normalized.split())
        union = current_tokens | prior_tokens
        overlap = len(current_tokens & prior_tokens) / len(union) if union else 0.0
        sequence = SequenceMatcher(None, normalized, prior_normalized).ratio()
        if min(len(current_tokens), len(prior_tokens)) >= 7 and overlap >= 0.86 and sequence >= 0.86:
            return (
                "This is nearly identical to a previous submission. State the new result, change, or evidence instead of reusing boilerplate."
            )
    return None


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _portal_template() -> str:
    return r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>Don Pollo Workday</title>
  <style>
    :root { --ink:#152821; --muted:#607069; --paper:#f5f1e8; --card:#fffdf8; --line:#d8ded8; --teal:#176e64; --teal-dark:#0d4f48; --orange:#e47d35; --soft:#e8f2ef; --danger:#a83f32; }
    * { box-sizing:border-box; }
    body { margin:0; color:var(--ink); background:var(--paper); font:16px/1.45 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    button,input,textarea,select { font:inherit; }
    button { cursor:pointer; }
    .shell { width:min(1180px,calc(100% - 28px)); margin:20px auto 64px; }
    .topbar { display:flex; gap:16px; justify-content:space-between; align-items:center; padding:18px 20px; border:1px solid var(--line); background:var(--card); border-radius:22px; position:sticky; top:10px; z-index:4; box-shadow:0 8px 24px rgba(21,40,33,.08); }
    .brand { display:flex; gap:12px; align-items:center; }
    .mark { width:42px; height:42px; display:grid; place-items:center; border-radius:14px; background:var(--teal); color:white; font-weight:900; }
    .eyebrow { color:var(--teal-dark); text-transform:uppercase; letter-spacing:.1em; font-size:.72rem; font-weight:800; }
    h1,h2,h3,p { margin-top:0; }
    h1 { margin-bottom:4px; font-size:clamp(1.2rem,3vw,1.65rem); letter-spacing:-.03em; }
    h2 { font-size:1.12rem; margin-bottom:6px; }
    h3 { font-size:1rem; margin-bottom:5px; }
    .muted { color:var(--muted); }
    .status { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
    .pill { border:1px solid var(--line); border-radius:999px; padding:7px 11px; background:white; font-size:.85rem; }
    .pill.beta { color:#7a431e; border-color:#eab181; background:#fff3e8; font-weight:800; }
    .pill.running { color:white; border-color:var(--teal); background:var(--teal); }
    .panel { border:1px solid var(--line); background:var(--card); border-radius:22px; padding:22px; box-shadow:0 8px 28px rgba(21,40,33,.05); }
    .section-space { margin-top:18px; }
    .work-now { margin-top:18px; border-top:5px solid var(--teal); }
    .time-strip { display:grid; grid-template-columns:1.15fr 1fr 1fr; gap:10px; margin:2px 0 14px; }
    .time-card { min-width:0; padding:13px 14px; border:1px solid var(--line); border-radius:15px; background:var(--soft); }
    .time-card.primary { background:var(--ink); border-color:var(--ink); color:white; }
    .time-card small { display:block; margin-bottom:2px; color:var(--muted); font-size:.7rem; font-weight:900; letter-spacing:.07em; text-transform:uppercase; }
    .time-card.primary small,.time-card.primary .time-detail { color:#d4e0da; }
    .time-value { display:block; overflow:hidden; font-size:clamp(1.3rem,3.2vw,1.75rem); font-variant-numeric:tabular-nums; letter-spacing:-.03em; text-overflow:ellipsis; white-space:nowrap; }
    .time-detail { display:block; overflow:hidden; margin-top:2px; color:var(--muted); font-size:.78rem; text-overflow:ellipsis; white-space:nowrap; }
    .hero-main { background:var(--ink); color:white; border-color:var(--ink); }
    .hero-main p { color:#d4e0da; max-width:66ch; }
    .hero-main strong { color:#ffd3ad; }
    .notice { border-left:4px solid var(--orange); padding:12px 14px; border-radius:10px; background:#fff3e8; color:#653819; }
    .quality-warning { display:none; margin:0 0 14px; border:2px solid var(--danger); border-radius:15px; padding:15px; background:#fff0ed; color:#6d2019; }
    .quality-warning.show { display:block; }
    .quality-warning strong { display:block; font-size:1.02rem; }
    .quality-countdown { font-size:1.5rem; font-weight:900; font-variant-numeric:tabular-nums; }
    .lane-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:9px; margin-top:12px; }
    .lane { min-height:68px; text-align:left; border:1px solid var(--line); border-radius:13px; padding:10px 12px; background:white; color:var(--ink); font-weight:800; }
    .lane.selected { border-color:var(--teal); background:var(--soft); }
    .section-head { display:flex; align-items:flex-end; justify-content:space-between; gap:12px; margin-bottom:14px; }
    .task-grid { display:grid; grid-template-columns:repeat(5,minmax(180px,1fr)); gap:12px; overflow-x:auto; padding:2px 2px 8px; }
    .task { min-height:218px; text-align:left; border:1px solid var(--line); border-radius:17px; padding:16px; background:white; display:flex; flex-direction:column; gap:8px; color:var(--ink); }
    .task:hover,.task.selected { border-color:var(--teal); box-shadow:0 0 0 2px rgba(23,110,100,.12); }
    .task.selected { background:var(--soft); }
    .task .number { width:30px; height:30px; display:grid; place-items:center; border-radius:10px; background:var(--ink); color:white; font-weight:900; }
    .task .why { margin-top:auto; font-size:.82rem; color:var(--teal-dark); }
    .tagrow { display:flex; gap:6px; flex-wrap:wrap; }
    .tag { font-size:.73rem; border:1px solid var(--line); border-radius:999px; padding:4px 7px; color:var(--muted); }
    .recommended { border-color:#eab181; background:#fff3e8; color:#7a431e; }
    .catalog-tools { display:grid; grid-template-columns:minmax(220px,1fr) auto; gap:12px; align-items:end; margin:12px 0 16px; }
    .inline-check { display:flex; align-items:center; gap:8px; min-height:44px; padding:0 4px; white-space:nowrap; }
    .inline-check input { width:auto; }
    .catalog { display:grid; gap:10px; }
    .catalog details { border:1px solid var(--line); border-radius:14px; padding:0; background:white; overflow:hidden; }
    .catalog summary { padding:13px 14px; list-style-position:inside; }
    .catalog .space-group { margin:0 12px 10px; border-radius:11px; background:var(--card); }
    .catalog .space-group > summary { padding:10px 12px; color:var(--teal-dark); }
    .catalog-list { padding:0 12px 12px; }
    .catalog-list h3 { margin:8px 0; color:var(--muted); font-size:.8rem; text-transform:uppercase; letter-spacing:.06em; }
    .catalog-task { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:10px; align-items:center; padding:10px 0; border-top:1px solid var(--line); }
    .catalog-task:first-of-type { border-top:0; }
    .catalog-task strong { display:block; }
    .catalog-task .meta { color:var(--muted); font-size:.79rem; margin-top:3px; }
    .catalog-empty { padding:14px; color:var(--muted); text-align:center; }
    .chooser-details { border:0; padding:0; }
    .chooser-summary { display:flex; align-items:flex-end; justify-content:space-between; gap:12px; list-style:none; }
    .chooser-summary::-webkit-details-marker { display:none; }
    .chooser-summary h2 { display:inline; }
    .chooser-summary::after { content:'Expand'; color:var(--teal-dark); font-size:.8rem; margin-left:auto; }
    .chooser-details[open] > .chooser-summary::after { content:'Collapse'; }
    .chooser-body { margin-top:14px; }
    .layout { display:grid; grid-template-columns:1.45fr .75fr; gap:18px; margin-top:18px; }
    .stack { display:grid; gap:18px; }
    .form-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
    label { display:grid; gap:6px; color:var(--muted); font-size:.84rem; font-weight:700; }
    input,textarea,select { width:100%; border:1px solid var(--line); border-radius:12px; background:white; color:var(--ink); padding:11px 12px; }
    textarea { min-height:92px; resize:vertical; }
    input:focus,textarea:focus,select:focus { outline:3px solid rgba(23,110,100,.15); border-color:var(--teal); }
    .actions { display:flex; flex-wrap:wrap; gap:9px; align-items:center; margin-top:15px; }
    .button { border:1px solid var(--line); background:white; color:var(--ink); padding:10px 14px; border-radius:12px; font-weight:800; }
    .button.primary { background:var(--teal); border-color:var(--teal); color:white; }
    .button.orange { background:var(--orange); border-color:var(--orange); color:white; }
    .button.subtle { padding:7px 10px; font-size:.82rem; color:var(--muted); }
    .button.danger { color:var(--danger); }
    .button:disabled { opacity:.45; cursor:not-allowed; }
    .selected-task { padding:12px 14px; border:1px solid var(--line); border-radius:14px; background:var(--soft); margin-bottom:14px; }
    .ai-composer { padding:14px; border:1px solid rgba(23,110,100,.3); border-radius:16px; background:linear-gradient(135deg,var(--soft),#fff); margin-bottom:14px; }
    .ai-composer textarea { min-height:76px; }
    .ai-guidance { margin:9px 0 0; color:var(--teal-dark); font-size:.84rem; }
    .progress-choices { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:8px; margin:12px 0 14px; }
    .progress-choice { border:1px solid var(--line); border-radius:12px; padding:10px 8px; background:white; color:var(--ink); font-weight:800; min-height:46px; }
    .progress-choice.selected { border-color:var(--teal); background:var(--soft); color:var(--teal-dark); box-shadow:inset 0 0 0 1px var(--teal); }
    .checkpoint-meta { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:10px; color:var(--muted); font-size:.84rem; }
    .plan-details { border:1px solid var(--line); border-radius:14px; padding:12px; }
    .plan-details > summary { color:var(--ink); }
    .plan-details .form-grid { margin-top:12px; }
    .checklist { margin:10px 0 0; padding-left:20px; color:var(--muted); }
    .checklist li { margin:5px 0; }
    details { border-top:1px solid var(--line); padding-top:13px; }
    summary { cursor:pointer; font-weight:800; }
    .profile-summary { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; margin:12px 0; }
    .fact { padding:11px; border-radius:12px; background:var(--soft); }
    .fact small { display:block; color:var(--muted); text-transform:uppercase; letter-spacing:.07em; }
    .days { display:flex; gap:6px; flex-wrap:wrap; }
    .day { position:relative; }
    .day input { position:absolute; opacity:0; pointer-events:none; }
    .day span { display:block; border:1px solid var(--line); border-radius:10px; padding:7px 9px; background:white; }
    .day input:checked + span { border-color:var(--teal); background:var(--soft); color:var(--teal-dark); font-weight:800; }
    .history { display:grid; gap:8px; }
    .history article { border-left:3px solid var(--line); padding-left:10px; font-size:.86rem; }
    .history time { color:var(--muted); }
    #toast { position:fixed; right:18px; bottom:18px; width:min(420px,calc(100% - 36px)); padding:14px 16px; border-radius:14px; background:var(--ink); color:white; box-shadow:0 12px 40px rgba(0,0,0,.2); transform:translateY(140%); transition:.2s ease; z-index:10; }
    #toast.show { transform:translateY(0); }
    #toast.error { background:var(--danger); }
    @media (max-width:900px) { .layout { grid-template-columns:1fr; } .topbar { position:static; } .task-grid { grid-template-columns:repeat(5,240px); } .lane-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
    @media (max-width:600px) { .shell { width:min(100% - 16px,100%); margin:8px auto 40px; } .topbar,.panel { border-radius:16px; padding:16px; } .topbar { align-items:flex-start; } .status { display:none; } .time-strip { grid-template-columns:1fr 1fr; } .time-card.primary { grid-column:1/-1; } .form-grid,.profile-summary,.catalog-tools,.lane-grid { grid-template-columns:1fr; } .task-grid { grid-template-columns:1fr; overflow:visible; } .task { min-height:0; } .actions .button { min-height:44px; } .work-now .actions .primary { flex:1 1 100%; } .catalog-task { grid-template-columns:1fr; } .catalog-task .button { width:100%; } .progress-choices { grid-template-columns:1fr 1fr; } .progress-choice:last-child { grid-column:1/-1; } .checkpoint-meta { align-items:flex-start; flex-direction:column; } }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div class="brand"><div class="mark">DP</div><div><div class="eyebrow">Cislune work system</div><h1>Don Pollo Workday</h1></div></div>
      <div class="status"><span class="pill beta" id="mode-pill">Portal</span><span class="pill" id="clock-pill">Ready</span><span class="pill">Slack connected</span></div>
    </header>
    <section class="panel work-now" id="work-now">
      <div class="section-head"><div><div class="eyebrow">Work now</div><h2>Start or continue useful work</h2></div><span class="pill" id="quality-pill">0 strong plans</span></div>
      <div class="quality-warning" id="quality-warning" role="alert"><strong>Specific correction required</strong><span class="quality-countdown" id="quality-countdown">10:00</span><p id="quality-reasons" style="margin:6px 0"></p><p style="margin:0">Submit a concrete result or checkpoint before the deadline. Otherwise Don Pollo will stop future time. Time already recorded stays intact; stop work and contact Erik or George if the clock stops.</p></div>
      <div class="time-strip" aria-live="polite">
        <div class="time-card primary"><small>Today worked</small><strong class="time-value" id="today-time">0:00:00</strong><span class="time-detail" id="session-state">Not clocked in</span></div>
        <div class="time-card"><small>Current task timer</small><strong class="time-value" id="current-task-time">0:00:00</strong><span class="time-detail" id="current-task-label">No task running</span></div>
        <div class="time-card"><small>Task time today</small><strong class="time-value" id="tracked-time">0:00:00</strong><span class="time-detail" id="lunch-time">No unpaid lunch recorded</span></div>
      </div>
      <div id="work-notice"></div>
      <div class="selected-task"><small class="muted">SELECTED TASK</small><div id="selected-task">Choose an option below.</div></div>
      <div class="ai-composer">
        <label>Explain what you plan to do in your own words<textarea id="plan-intent" placeholder="Example: I want to fix the bracket hole spacing, test the revision against the measurements, and leave the drawing ready for review."></textarea></label>
        <div class="actions"><button class="button orange" id="coach-plan" data-action="coach-plan">Turn this into a work plan</button><span class="muted" id="ai-usage">Writing helper loading…</span></div>
        <p class="ai-guidance" id="ai-plan-guidance">Don Pollo will organize your facts into a result, first move, evidence, estimate, and checkpoint for you to review.</p>
      </div>
      <details class="plan-details" id="plan-details" open><summary>Review / edit the structured work plan</summary><div class="form-grid">
          <label>By the next checkpoint, what will exist or be demonstrably different?<textarea id="outcome" placeholder="Example: A tested mounting bracket CAD revision with the hole pattern corrected and a screenshot attached."></textarea></label>
          <label>What is the first concrete move?<textarea id="first-step" placeholder="Example: Open revision 3, measure the current hole spacing, and update the sketch constraints."></textarea></label>
          <label style="grid-column:1/-1">What evidence will show the result?<textarea id="evidence" placeholder="Example: Upload the revised CAD screenshot and record the measured hole spacing in the task."></textarea></label>
          <label>Rough estimate<select id="estimate"><option>30 minutes</option><option selected>1 hour</option><option>2 hours</option><option>half day</option><option>full day</option><option>multi-day</option></select></label>
          <label>Reconsider / ask for help after<select id="checkpoint"><option>30 minutes</option><option selected>60 minutes</option><option>2 hours</option><option value="when result is ready">When result is ready</option></select></label>
      </div></details>
      <div class="actions"><button class="button primary" id="start-button" data-action="start">Start work</button><button class="button orange" id="claim-selected" data-action="claim-task">Claim in ClickUp</button><button class="button subtle" data-action="short-rest">Short rest</button><button class="button subtle" data-action="lunch">Lunch</button><button class="button subtle" data-action="back">Back</button><button class="button danger subtle" data-action="clock-out">Clock out</button></div>
      <p class="muted" id="mode-note" style="margin:12px 0 0">A recognizable result and concrete first move are required.</p>
    </section>
    <section class="panel section-space" id="checkpoint-panel" hidden>
      <div class="section-head"><div><div class="eyebrow">Quick checkpoint</div><h2>What is true now?</h2></div><span class="pill" id="next-checkpoint-label">Next check-in not set</span></div>
      <div class="progress-choices" role="group" aria-label="Update type">
        <button class="progress-choice selected" data-progress-kind="made_progress">Made progress</button>
        <button class="progress-choice" data-progress-kind="result_ready">Result ready</button>
        <button class="progress-choice" data-progress-kind="blocked">Blocked</button>
        <button class="progress-choice" data-progress-kind="changed_plan">Changed plan</button>
        <button class="progress-choice" data-progress-kind="still_working">Still working</button>
      </div>
      <div class="form-grid"><label>What changed, or what will you do differently?<textarea id="progress" placeholder="Name the output, test result, file, part, decision, or lesson another person could recognize."></textarea></label><label>Blocker or help needed<textarea id="blocker" placeholder="Optional unless Blocked is selected. Name the dependency, decision, or failed approach."></textarea></label></div>
      <div class="checkpoint-meta"><label>Check in next<select id="next-checkpoint"><option>30 minutes</option><option selected>60 minutes</option><option>2 hours</option><option value="when result is ready">When result is ready</option></select></label><span id="checkpoint-help">One concise, concrete update is enough. Still working is okay once between useful updates.</span></div>
      <div class="actions"><button class="button orange" id="coach-checkpoint" data-action="coach-checkpoint">Improve with Don Pollo</button><button class="button primary" data-action="check-in">Save update</button><button class="button" data-action="share-slack">Send concise summary to Slack</button></div>
      <p class="ai-guidance" id="ai-checkpoint-guidance">Write rough facts first. Don Pollo can organize them, but only Save update changes the work record.</p>
    </section>
    <section class="panel section-space">
      <details class="chooser-details" id="recommended-details" open>
        <summary class="chooser-summary"><span><span class="eyebrow">Quick choice</span><br><h2>Five best next options</h2></span><span class="pill" id="task-count">5 options</span></summary>
        <div class="chooser-body"><p class="muted">Ranked from your assignments, priorities, active work, interests, and skills.</p><div class="notice" id="task-warning" hidden></div><div class="task-grid" id="task-grid"></div></div>
      </details>
    </section>
    <section class="panel section-space">
      <details class="chooser-details" id="overhead-details">
        <summary class="chooser-summary"><span><span class="eyebrow">Non-contract work</span><br><h2>Choose where overhead time belongs</h2></span><span class="pill">8 lanes</span></summary>
        <div class="chooser-body"><p class="muted">Use these for necessary company work. Don Pollo will help find an existing ClickUp task; a genuinely new task still goes to Erik or George for approval before timing.</p><div class="lane-grid" id="overhead-lanes"></div></div>
      </details>
    </section>
    <section class="panel section-space">
      <details class="chooser-details" id="catalog-details" open>
        <summary class="chooser-summary"><span><span class="eyebrow">Explore Cislune work</span><br><h2>Browse all potential tasks</h2></span><span class="pill" id="task-total">0 open tasks</span></summary>
        <div class="chooser-body"><p class="muted">Open ClickUp work organized by contract or program, then space and list.</p><div class="catalog-tools"><label>Search tasks, contracts, or spaces<input id="task-search" type="search" placeholder="Search all open work"></label><label class="inline-check"><input id="assigned-only" type="checkbox"> Assigned to me only</label></div><div class="catalog" id="task-catalog"></div><details><summary>Nothing fits? Propose work for approval</summary><div class="form-grid" style="margin-top:12px"><label>Task title<input id="request-title" placeholder="Label and organize the electronics bench"></label><label>Type<select id="request-type"><option value="project">Project</option><option value="overhead">Overhead / shop</option></select></label><label style="grid-column:1/-1">Why this should be done<textarea id="request-reason" placeholder="What result or problem does this address?"></textarea></label></div><div class="actions"><button class="button" data-action="request-task">Request approval in Slack</button></div></details></div>
      </details>
    </section>
    <div class="layout">
      <aside class="stack" style="grid-column:1/-1">
        <section class="panel"><div class="eyebrow">Worker context</div><h2 id="welcome">Welcome</h2><p class="muted" id="schedule-line"></p><div class="profile-summary"><div class="fact"><small>Skills</small><span id="skills-line">—</span></div><div class="fact"><small>Interests</small><span id="interests-line">—</span></div></div><details id="profile-details" open><summary id="profile-summary-label">Set schedule & fit</summary><div style="margin-top:12px" class="stack"><label>Weekly target hours<input id="weekly-hours" type="number" min="1" max="80" step="0.5"></label><label>Regular workdays<div class="days" id="workdays"></div></label><div class="form-grid"><label>Typical start<input id="start-time" type="time"></label><label>Typical end<input id="end-time" type="time"></label></div><label>Planned time off<input id="time-off" placeholder="2026-08-21, 2026-08-24"></label><label>Interests<input id="interests" placeholder="robotics, fabrication, flight testing"></label><label>Skills<input id="skills" placeholder="CAD, Python, assembly"></label><button class="button" data-action="save-profile">Save worker context</button></div></details></section>
        <section class="panel"><div class="section-head"><div><div class="eyebrow" id="history-eyebrow">Recent portal activity</div><h2>Audit trail</h2></div></div><div class="history" id="history"></div><div class="actions"><button class="button subtle danger" id="reset-beta" data-action="reset-beta">Reset preview</button></div></section>
      </aside>
    </div>
  </main>
  <div id="toast" role="status" aria-live="polite"></div>
  <script id="portal-data" type="application/json">__PORTAL_PAYLOAD__</script>
  <script>
    let data = JSON.parse(document.getElementById('portal-data').textContent || '{}');
    const token = new URLSearchParams(location.search).get('token') || '';
    const days = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday'];
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
    let renderedProfileSavedAt = null;
    let chooserInitialized = false;
    let selectedProgressKind = null;
    const dirtyFields = new Set();
    const editableFieldIds = new Set(['plan-intent','outcome','first-step','evidence','estimate','checkpoint','progress','blocker','next-checkpoint','weekly-hours','start-time','end-time','time-off','interests','skills']);
    function syncInput(id, value) {
      if (!dirtyFields.has(id)) $(id).value = value ?? '';
    }
    function clearSubmittedDraft(action) {
      const submittedFields = {
        start: ['outcome','first-step','evidence','estimate','checkpoint'],
        coach_plan: ['plan-intent','outcome','first-step','evidence','estimate','checkpoint'],
        coach_checkpoint: ['progress','blocker'],
        check_in: ['progress','blocker','next-checkpoint'],
        clock_out: ['progress','blocker'],
        save_profile: ['weekly-hours','workdays','start-time','end-time','time-off','interests','skills'],
      }[action] || [];
      submittedFields.forEach((id) => dirtyFields.delete(id));
      if (action === 'check_in') selectedProgressKind = null;
    }
    function formatDuration(value) {
      const total = Math.max(0, Math.floor(Number(value) || 0));
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      const seconds = total % 60;
      return `${hours}:${String(minutes).padStart(2,'0')}:${String(seconds).padStart(2,'0')}`;
    }
    function formatClock(value) {
      const parsed = Date.parse(value || '');
      return Number.isFinite(parsed) ? new Date(parsed).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'}) : '';
    }
    function renderTimers() {
      const time = data.time || {}, work = data.work || {};
      const generatedAt = Date.parse(data.generated_at || '');
      const elapsed = Number.isFinite(generatedAt) ? Math.max(0, (Date.now() - generatedAt) / 1000) : 0;
      const worked = Number(time.worked_seconds || 0) + (time.work_clock_running ? elapsed : 0);
      const tracked = Number(time.tracked_seconds || 0) + (time.task_timer_running ? elapsed : 0);
      const current = Number(time.current_task_seconds || 0) + (time.task_timer_running ? elapsed : 0);
      $('today-time').textContent = formatDuration(worked);
      $('tracked-time').textContent = formatDuration(tracked);
      $('current-task-time').textContent = formatDuration(current);
      const clockedIn = formatClock(time.clocked_in_at);
      const clockedOut = formatClock(time.clocked_out_at);
      const stateLabels = {
        active: clockedIn ? `Working since ${clockedIn}` : 'Work clock is running',
        short_rest: 'Paid short rest · work clock is running',
        lunch: 'Unpaid lunch · work clock is paused',
        clocked_out: clockedOut ? `Clocked out at ${clockedOut}` : 'Clocked out',
        ready: 'Not clocked in',
      };
      $('session-state').textContent = stateLabels[work.status] || 'Not clocked in';
      $('current-task-label').textContent = time.current_task_name || work.selected_task_name || 'No task running';
      const lunchSeconds = Number(time.unpaid_lunch_seconds || 0);
      $('lunch-time').textContent = lunchSeconds > 0 ? `Unpaid lunch excluded: ${formatDuration(lunchSeconds)}` : 'No unpaid lunch recorded';
      const quality = data.quality || {};
      const deadline = Date.parse(quality.warning_deadline_at || '');
      const remaining = Number.isFinite(deadline) ? Math.max(0, Math.ceil((deadline - Date.now()) / 1000)) : 0;
      $('quality-warning').classList.toggle('show', Number.isFinite(deadline) && remaining >= 0);
      $('quality-countdown').textContent = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2,'0')}`;
    }
    function renderTaskCatalog() {
      const query = String($('task-search').value || '').trim().toLowerCase();
      const queryTerms = query.split(/\s+/).filter(Boolean);
      const assignedOnly = $('assigned-only').checked;
      const openGroups = new Set([...document.querySelectorAll('#task-catalog details[open]')].map(node => node.dataset.groupKey));
      const contracts = (data.task_catalog || []).map((contract) => {
        const flattenOnlyOtherSpace = (contract.spaces || []).length === 1 && String(contract.spaces[0].name || '').toLowerCase() === 'other';
        const spaces = (contract.spaces || []).map((space) => {
          const lists = (space.lists || []).map((taskList) => {
            const visibleTasks = (taskList.tasks || []).filter((task) => {
              if (assignedOnly && !task.assigned) return false;
              const searchable = [task.name,task.status,task.priority,task.location,contract.name,space.name,taskList.name].join(' ').toLowerCase();
              return !query || searchable.includes(query) || queryTerms.some(term => searchable.includes(term));
            });
            if (!visibleTasks.length) return '';
            const rows = visibleTasks.map((task) => { const owners=(task.assignees || []).join(', ') || 'Unassigned'; const due=task.due_date ? ` · Due ${esc(task.due_date)}` : ''; return `<div class="catalog-task"><div><strong>${esc(task.name)}</strong><div class="meta">${task.assigned ? 'Assigned to you · ' : ''}${esc(task.priority)} · ${esc(task.status)}${due} · Owners: ${esc(owners)}</div></div><div class="actions" style="margin:0"><button class="button" data-select-task="${esc(task.id)}" data-task-name="${esc(task.name)}" data-task-location="${esc(task.location)}">Choose</button><button class="button orange" data-claim-task="${esc(task.id)}" data-task-name="${esc(task.name)}" data-task-location="${esc(task.location)}"${task.assigned ? ' disabled' : ''}>${task.assigned ? 'Already mine' : 'Claim'}</button></div></div>`; }).join('');
            return `<div class="catalog-list"><h3>${esc(taskList.name)} · ${visibleTasks.length}</h3>${rows}</div>`;
          }).filter(Boolean);
          if (!lists.length) return '';
          if (flattenOnlyOtherSpace) return lists.join('');
          const groupKey = `${contract.name}::${space.name}`;
          const isOpen = query || openGroups.has(groupKey);
          return `<details class="space-group" data-group-key="${esc(groupKey)}"${isOpen ? ' open' : ''}><summary>${esc(space.name)} · ${lists.length} active list${lists.length === 1 ? '' : 's'}</summary>${lists.join('')}</details>`;
        }).filter(Boolean);
        if (!spaces.length) return '';
        const groupKey = String(contract.name || 'General / overhead');
        const isOpen = query || openGroups.has(groupKey);
        return `<details class="contract-group" data-group-key="${esc(groupKey)}"${isOpen ? ' open' : ''}><summary>${esc(contract.name)} · ${contract.task_count} open</summary>${spaces.join('')}</details>`;
      }).filter(Boolean);
      $('task-catalog').innerHTML = contracts.join('') || '<div class="catalog-empty">No open tasks match this view.</div>';
    }
    function render() {
      const work = data.work || {}, profile = data.profile || {}, quality = data.quality || {}, ai = data.ai || {};
      const live = Boolean(data.live);
      $('welcome').textContent = `Welcome, ${(data.actor || {}).name || 'worker'}`;
      $('mode-pill').textContent = live ? 'Live workday' : 'Admin preview';
      $('mode-pill').classList.toggle('running', live);
      $('start-button').textContent = live ? 'Start / update live work' : 'Start preview work';
      $('mode-note').textContent = live
        ? 'This is the official work record. Starting also claims the task without removing co-owners, clocks you in, and runs the ClickUp timer. Slack uses this same session—never a duplicate.'
        : 'Admin preview only: time and payroll stay isolated. The explicit Claim action can still add you as a ClickUp assignee.';
      $('history-eyebrow').textContent = live ? 'Recent live activity' : 'Recent preview activity';
      $('reset-beta').hidden = live;
      $('clock-pill').textContent = String(work.status || 'ready').replaceAll('_',' ');
      $('clock-pill').classList.toggle('running', ['active','short_rest'].includes(work.status));
      renderTimers();
      $('work-notice').innerHTML = work.notice ? `<div class="notice">${esc(work.notice)}</div>` : '';
      $('quality-reasons').textContent = (quality.warning_reasons || []).join(' ');
      $('schedule-line').textContent = `${Number(profile.weekly_target_hours || 0)} hours/week · ${(profile.regular_workdays || []).map(day => day.slice(0,3)).join(', ')} · ${profile.typical_start_time || '—'}–${profile.typical_end_time || '—'}`;
      $('skills-line').textContent = (profile.skills || []).join(', ') || 'Not set';
      $('interests-line').textContent = (profile.interests || []).join(', ') || 'Not set';
      $('task-warning').hidden = !data.task_warning;
      $('task-warning').textContent = data.task_warning || '';
      const tasks = data.task_options || [];
      $('task-count').textContent = `${tasks.length} option${tasks.length === 1 ? '' : 's'}`;
      $('task-total').textContent = `${Number(data.task_total || 0)} open tasks`;
      $('task-grid').innerHTML = tasks.map((task,index) => `<button class="task ${task.id === work.selected_task_id ? 'selected' : ''}" data-select-task="${esc(task.id)}" data-task-name="${esc(task.name)}" data-task-location="${esc(task.location)}"><span class="number">${index+1}</span><h3>${esc(task.name)}</h3><div class="tagrow">${task.recommended ? '<span class="tag recommended">Recommended</span>' : ''}${task.assigned ? '<span class="tag">Assigned to you</span>' : ''}<span class="tag">${esc(task.priority)}</span><span class="tag">${esc(task.status)}</span>${task.due_date ? `<span class="tag">Due ${esc(task.due_date)}</span>` : ''}</div><div class="muted">${esc(task.location)}</div><div class="why">Why: ${esc(task.reason)}</div></button>`).join('');
      $('overhead-lanes').innerHTML = (data.overhead_lanes || []).map((lane,index) => `<button class="lane ${lane.id === work.overhead_lane_id ? 'selected' : ''}" data-overhead-lane="${esc(lane.id)}" data-lane-search="${esc(lane.search)}" data-lane-name="${esc(lane.name)}"><span class="number">${index+1}</span> ${esc(lane.name)}</button>`).join('');
      renderTaskCatalog();
      $('selected-task').innerHTML = work.selected_task_name ? `<strong>${esc(work.selected_task_name)}</strong><br><span class="muted">${esc(work.selected_task_location || '')}</span>` : 'Choose an option above.';
      syncInput('plan-intent', work.plan_intent || '');
      syncInput('outcome', work.outcome || '');
      syncInput('first-step', work.first_step || '');
      syncInput('evidence', work.evidence || '');
      syncInput('estimate', work.estimate || '1 hour');
      const checkpointChoice = work.checkpoint === '90 minutes' ? '2 hours' : (work.checkpoint || '60 minutes');
      syncInput('checkpoint', checkpointChoice);
      syncInput('next-checkpoint', checkpointChoice);
      syncInput('progress', work.latest_progress || '');
      syncInput('blocker', work.latest_blocker || '');
      $('checkpoint-panel').hidden = work.status !== 'active';
      document.querySelectorAll('[data-progress-kind]').forEach((button) => button.classList.toggle('selected', button.dataset.progressKind === (selectedProgressKind || work.progress_kind || 'made_progress')));
      const checkpointDue = Date.parse(work.checkpoint_due_at || '');
      $('next-checkpoint-label').textContent = Number.isFinite(checkpointDue) ? `Next ${new Date(checkpointDue).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'})}` : 'Choose next check-in';
      const aiAvailable = Boolean(ai.request_available);
      $('coach-plan').disabled = !aiAvailable || !work.selected_task_id;
      $('coach-checkpoint').disabled = !aiAvailable || work.status !== 'active';
      $('ai-usage').textContent = ai.enabled
        ? `${Number(ai.worker_calls_today || 0)} helper use${Number(ai.worker_calls_today || 0) === 1 ? '' : 's'} today · ${Number(ai.tokens_used_today || 0).toLocaleString()} tokens`
        : 'AI helper unavailable · manual fields still work';
      $('ai-plan-guidance').textContent = ai.last_plan_question || ai.last_plan_note || 'Don Pollo will organize your facts into a result, first move, evidence, estimate, and checkpoint for you to review.';
      $('ai-checkpoint-guidance').textContent = ai.last_checkpoint_question || ai.last_checkpoint_note || 'Write rough facts first. Don Pollo can organize them, but only Save update changes the work record.';
      $('quality-pill').textContent = `${Number(quality.strong_plans || 0)} strong plan${Number(quality.strong_plans || 0) === 1 ? '' : 's'}`;
      syncInput('weekly-hours', profile.weekly_target_hours || 40);
      syncInput('start-time', profile.typical_start_time || '09:00');
      syncInput('end-time', profile.typical_end_time || '17:00');
      syncInput('time-off', (profile.planned_time_off || []).join(', '));
      syncInput('interests', (profile.interests || []).join(', '));
      syncInput('skills', (profile.skills || []).join(', '));
      if (!dirtyFields.has('workdays')) $('workdays').innerHTML = days.map(day => `<label class="day"><input type="checkbox" value="${day}" ${(profile.regular_workdays || []).includes(day) ? 'checked' : ''}><span>${day.slice(0,3)}</span></label>`).join('');
      const savedAt = String(profile.saved_at || '');
      if (renderedProfileSavedAt === null || renderedProfileSavedAt !== savedAt) $('profile-details').open = !savedAt;
      renderedProfileSavedAt = savedAt;
      $('profile-summary-label').textContent = savedAt ? 'Profile saved · Edit schedule & fit' : 'Set schedule & fit';
      if (!chooserInitialized) {
        $('recommended-details').open = !work.selected_task_id;
        $('catalog-details').open = !work.selected_task_id;
        $('plan-details').open = work.status !== 'active';
        chooserInitialized = true;
      }
      $('history').innerHTML = (data.history || []).length ? (data.history || []).map(item => `<article><strong>${esc(String(item.action || '').replaceAll('_',' '))}</strong><br>${esc(item.message)}<br><time>${new Date(item.at).toLocaleString()}</time></article>`).join('') : '<p class="muted">No portal actions yet.</p>';
      document.querySelector('[data-action="back"]').disabled = !['short_rest','lunch'].includes(work.status);
      document.querySelector('[data-action="short-rest"]').disabled = work.status !== 'active';
      document.querySelector('[data-action="lunch"]').disabled = work.status !== 'active';
      document.querySelector('[data-action="clock-out"]').disabled = ['ready','clocked_out'].includes(work.status);
      const selectedOption = (data.task_options || []).find(task => task.id === work.selected_task_id) || (data.task_catalog || []).flatMap(contract => contract.spaces || []).flatMap(space => space.lists || []).flatMap(taskList => taskList.tasks || []).find(task => task.id === work.selected_task_id);
      $('claim-selected').disabled = !work.selected_task_id || Boolean(selectedOption && selectedOption.assigned);
      $('claim-selected').textContent = selectedOption && selectedOption.assigned ? 'Already mine in ClickUp' : 'Claim in ClickUp';
    }
    function actionPayload(action) {
      const apiAction = action.replaceAll('-','_');
      if (apiAction === 'start') return {action:apiAction, outcome:$('outcome').value, first_step:$('first-step').value, evidence:$('evidence').value, estimate:$('estimate').value, checkpoint:$('checkpoint').value};
      if (apiAction === 'coach_plan') return {action:apiAction, intent:$('plan-intent').value, outcome:$('outcome').value, first_step:$('first-step').value, evidence:$('evidence').value, estimate:$('estimate').value, checkpoint:$('checkpoint').value};
      if (apiAction === 'coach_checkpoint') return {action:apiAction, progress:$('progress').value, blocker:$('blocker').value};
      if (apiAction === 'check_in') return {action:apiAction, progress_kind:selectedProgressKind || (data.work || {}).progress_kind || 'made_progress', progress:$('progress').value, blocker:$('blocker').value, checkpoint:$('next-checkpoint').value};
      if (apiAction === 'clock_out') return {action:apiAction, progress:$('progress').value, blocker:$('blocker').value};
      if (apiAction === 'claim_task') return {action:apiAction, task_id:(data.work || {}).selected_task_id};
      if (apiAction === 'save_profile') return {action:apiAction, weekly_target_hours:$('weekly-hours').value, regular_workdays:[...document.querySelectorAll('#workdays input:checked')].map(node => node.value), typical_start_time:$('start-time').value, typical_end_time:$('end-time').value, planned_time_off:$('time-off').value, interests:$('interests').value, skills:$('skills').value};
      if (apiAction === 'request_task') return {action:apiAction, title:$('request-title').value, task_type:$('request-type').value, reason:$('request-reason').value};
      return {action:apiAction};
    }
    async function post(payload, button) {
      const original = button ? button.textContent : '';
      if (button) { button.disabled = true; button.textContent = 'Working…'; }
      try {
        const response = await fetch(`/api/portal/action?token=${encodeURIComponent(token)}`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
        clearSubmittedDraft(payload.action);
        data = result; render(); showToast(result.message || 'Saved.'); return true;
      } catch (error) { showToast(error.message || String(error), true); return false; }
      finally { if (button && button.isConnected) { button.disabled = false; button.textContent = original; } }
    }
    document.addEventListener('click', (event) => {
      const progressChoice = event.target.closest('[data-progress-kind]');
      if (progressChoice) {
        selectedProgressKind = progressChoice.dataset.progressKind;
        render();
        if (progressChoice.dataset.progressKind === 'blocked') $('blocker').focus();
        else if (progressChoice.dataset.progressKind !== 'still_working') $('progress').focus();
        return;
      }
      const lane = event.target.closest('[data-overhead-lane]');
      if (lane) return post({action:'select_overhead_lane',lane_id:lane.dataset.overheadLane},lane).then((saved) => { if (!saved) return; $('task-search').value=lane.dataset.laneSearch || lane.dataset.laneName || ''; $('catalog-details').open=true; $('request-type').value='overhead'; $('request-title').placeholder=`Concrete result for ${lane.dataset.laneName || 'overhead work'}`; renderTaskCatalog(); $('catalog-details').scrollIntoView({behavior:'smooth',block:'start'}); });
      const claim = event.target.closest('[data-claim-task]');
      if (claim) return post({action:'claim_task',task_id:claim.dataset.claimTask},claim).then((saved) => { if (!saved) return; $('recommended-details').open=false; $('catalog-details').open=false; if (window.innerWidth <= 600) $('work-now').scrollIntoView({behavior:'smooth',block:'start'}); });
      const task = event.target.closest('[data-select-task]');
      if (task) return post({action:'select_task',task_id:task.dataset.selectTask,task_name:task.dataset.taskName,task_location:task.dataset.taskLocation},task).then((saved) => { if (!saved) return; $('recommended-details').open=false; $('catalog-details').open=false; if (window.innerWidth <= 600) $('work-now').scrollIntoView({behavior:'smooth',block:'start'}); });
      const button = event.target.closest('[data-action]');
      if (button) post(actionPayload(button.dataset.action), button);
    });
    document.addEventListener('input', (event) => {
      if (editableFieldIds.has(event.target.id)) dirtyFields.add(event.target.id);
    });
    document.addEventListener('change', (event) => {
      if (editableFieldIds.has(event.target.id)) dirtyFields.add(event.target.id);
      if (event.target.closest('#workdays')) dirtyFields.add('workdays');
    });
    $('task-search').addEventListener('input', renderTaskCatalog);
    $('assigned-only').addEventListener('change', renderTaskCatalog);
    let toastTimer;
    function showToast(message,error=false) { const node=$('toast'); node.textContent=message; node.className=error?'show error':'show'; clearTimeout(toastTimer); toastTimer=setTimeout(()=>node.className='',7000); }
    render();
    window.setInterval(renderTimers, 1000);
    window.setInterval(async () => {
      try {
        const response = await fetch(`/api/portal-data?token=${encodeURIComponent(token)}`, {cache:'no-store'});
        if (!response.ok) return;
        data = await response.json();
        render();
      } catch (_error) {}
    }, 30000);
  </script>
</body>
</html>'''

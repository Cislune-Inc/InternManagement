from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit

from .models import AdminProfile, UserProfile


_PORTAL_SECRET_STATE_KEY = "worker_portal_signing_secret"
_PORTAL_STATE_PREFIX = "worker_portal_beta:"
_PORTAL_BETA_ADMIN_NAMES = {"erik"}
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
_VALID_CHECKPOINTS = {"30 minutes", "60 minutes", "90 minutes", "2 hours"}


def build_worker_portal_link(
    runtime: Any,
    admin: AdminProfile,
    *,
    now: datetime | None = None,
    ttl_hours: int = 72,
) -> str:
    if not admin.slack_user_id:
        raise ValueError("The beta tester needs a Slack member ID.")
    if admin.name.strip().casefold() not in _PORTAL_BETA_ADMIN_NAMES:
        raise ValueError("The worker portal is still limited to the current beta tester.")
    reference = now or datetime.now(timezone.utc)
    token = _issue_token(runtime, admin.slack_user_id, reference, ttl_hours=ttl_hours)
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
    admin = runtime.admin_profile_by_slack_user_id(subject)
    if admin is None or admin.name.strip().casefold() not in _PORTAL_BETA_ADMIN_NAMES:
        raise ValueError("This beta link is not assigned to a current Don Pollo administrator.")
    return subject


def validate_work_commitment(
    outcome: str,
    first_step: str,
    *,
    previous_fingerprint: str = "",
) -> tuple[list[str], str]:
    outcome = _clean_text(outcome, limit=800)
    first_step = _clean_text(first_step, limit=500)
    issues: list[str] = []
    if _is_vague(outcome, minimum_words=6, minimum_characters=28):
        issues.append("Describe the result you expect to point to—not just that you will ‘work on’ the task.")
    if _is_vague(first_step, minimum_words=4, minimum_characters=18):
        issues.append("Name the first concrete action, file, part, test, or person you will start with.")
    fingerprint = hashlib.sha256(f"{outcome.lower()}\n{first_step.lower()}".encode("utf-8")).hexdigest()
    if previous_fingerprint and hmac.compare_digest(previous_fingerprint, fingerprint):
        issues.append("This is the same answer as the last rejected attempt; add the missing specifics before trying again.")
    return issues, fingerprint


def validate_meaningful_work_detail(
    value: str,
    *,
    purpose: str,
    previous_fingerprint: str = "",
) -> tuple[str | None, str]:
    cleaned = _clean_text(value, limit=800)
    fingerprint = hashlib.sha256(cleaned.lower().encode("utf-8")).hexdigest()
    if _is_vague(cleaned, minimum_words=5, minimum_characters=24):
        return (
            f"Add a specific {purpose}: name the output, change, test, file, part, or decision another person could recognize.",
            fingerprint,
        )
    if previous_fingerprint and hmac.compare_digest(previous_fingerprint, fingerprint):
        return (
            "That repeats the last rejected answer. Add the missing specifics instead of resubmitting the same text.",
            fingerprint,
        )
    return None, fingerprint


class WorkerPortalService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._task_source_cache: dict[str, tuple[float, list[dict[str, Any]], set[str]]] = {}

    async def build_payload(self, token: str) -> dict[str, Any]:
        slack_user_id = validate_worker_portal_token(self.runtime, token)
        admin = self.runtime.admin_profile_by_slack_user_id(slack_user_id)
        if admin is None:
            raise ValueError("The beta tester is no longer configured.")
        state = self._load_state(slack_user_id, admin)
        self._advance_deadlines(state)
        self._save_state(slack_user_id, state)
        tasks, task_warning = await self._load_task_options(admin, state)
        return self._payload(admin, state, tasks, task_warning)

    async def apply_action(self, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        slack_user_id = validate_worker_portal_token(self.runtime, token)
        admin = self.runtime.admin_profile_by_slack_user_id(slack_user_id)
        if admin is None:
            raise ValueError("The beta tester is no longer configured.")
        state = self._load_state(slack_user_id, admin)
        self._advance_deadlines(state)
        action = str(payload.get("action") or "").strip().lower()
        message = ""
        if action == "save_profile":
            self._save_profile(state, payload)
            message = "Profile saved. Don Pollo can now use this context when ranking work and timing prompts."
        elif action == "select_task":
            task_id = _clean_text(payload.get("task_id"), limit=100)
            task_name = _clean_text(payload.get("task_name"), limit=240)
            if not task_id or not task_name:
                raise ValueError("Choose one of the task options first.")
            state["work"]["selected_task_id"] = task_id
            state["work"]["selected_task_name"] = task_name
            state["work"]["selected_task_location"] = _clean_text(payload.get("task_location"), limit=240)
            message = f"Selected {task_name}. Add a concrete outcome and first step before starting."
        elif action == "claim_task":
            message = await self._claim_task(admin, state, payload)
        elif action == "start":
            try:
                message = self._start_work(state, payload)
            except ValueError as exc:
                self._record_history(state, "start_rejected", str(exc))
                self._save_state(slack_user_id, state)
                raise
        elif action == "check_in":
            message = self._check_in(state, payload)
        elif action == "short_rest":
            message = self._start_short_rest(state)
        elif action == "lunch":
            message = self._start_lunch(state)
        elif action == "back":
            message = self._return_from_break(state)
        elif action == "clock_out":
            message = self._clock_out(state)
        elif action == "request_task":
            message = await self._request_task(slack_user_id, state, payload)
        elif action == "share_slack":
            message = await self._share_to_slack(slack_user_id, state)
        elif action == "reset_beta":
            state = self._default_state(admin)
            message = "Beta workday reset. No live workforce records were changed."
        else:
            raise ValueError("Unsupported portal action.")
        self._record_history(state, action, message)
        self._save_state(slack_user_id, state)
        tasks, task_warning = await self._load_task_options(admin, state)
        result = self._payload(admin, state, tasks, task_warning)
        result["message"] = message
        return result

    def render_html(self, payload: dict[str, Any]) -> str:
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":")).replace("</", "<\\/")
        return _portal_template().replace("__PORTAL_PAYLOAD__", payload_json)

    def _load_state(self, slack_user_id: str, admin: AdminProfile) -> dict[str, Any]:
        state = self.runtime.state_store.get_operational_state(_PORTAL_STATE_PREFIX + slack_user_id)
        if not isinstance(state, dict):
            return self._default_state(admin)
        default = self._default_state(admin)
        for key in ("profile", "work", "quality"):
            if not isinstance(state.get(key), dict):
                state[key] = default[key]
            else:
                state[key] = {**default[key], **state[key]}
        if not isinstance(state.get("history"), list):
            state["history"] = []
        if not isinstance(state.get("task_requests"), list):
            state["task_requests"] = []
        return state

    def _default_state(self, admin: AdminProfile) -> dict[str, Any]:
        return {
            "version": 1,
            "profile": {
                "display_name": admin.name,
                "saved_at": "",
                "weekly_target_hours": 40,
                "regular_workdays": list(_DEFAULT_WORKDAYS),
                "typical_start_time": "09:00",
                "typical_end_time": "17:00",
                "planned_time_off": [],
                "interests": ["project delivery", "process improvement"],
                "skills": ["program management"],
            },
            "work": {
                "status": "ready",
                "selected_task_id": "",
                "selected_task_name": "",
                "selected_task_location": "",
                "outcome": "",
                "first_step": "",
                "estimate": "1 hour",
                "checkpoint": "60 minutes",
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
                "last_rejected_fingerprint": "",
                "strong_plans": 0,
            },
            "task_requests": [],
            "history": [],
        }

    async def _load_task_options(
        self,
        admin: AdminProfile,
        state: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str]:
        if not self.runtime.clickup:
            return self._fallback_tasks(), "ClickUp is unavailable, so these are safe beta examples."
        profile = state["profile"]
        portal_user = UserProfile(
            user_key=f"portal-beta-{admin.name.lower().replace(' ', '-')}",
            display_name=admin.name,
            discord_user_id=admin.discord_user_id,
            slack_user_id=admin.slack_user_id,
            clickup_user_id=admin.clickup_user_id,
            clickup_user_email=admin.clickup_user_email,
            interests=list(profile.get("interests") or []),
            skills=list(profile.get("skills") or []),
        )
        cached = self._task_source_cache.get(admin.slack_user_id)
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
                self._task_source_cache[admin.slack_user_id] = (
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

    async def _claim_task(
        self,
        admin: AdminProfile,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        if not self.runtime.clickup:
            raise ValueError("ClickUp is unavailable, so this task could not be claimed.")
        task_id = _clean_text(payload.get("task_id"), limit=100)
        if not task_id:
            raise ValueError("Choose a task before claiming it in ClickUp.")
        available_tasks, warning = await self._load_task_options(admin, state)
        if warning:
            raise ValueError("The live ClickUp catalog is not available, so assignment was not changed.")
        candidate = next((task for task in available_tasks if task.get("id") == task_id), None)
        if candidate is None:
            raise ValueError("That task is not in the current open Cislune task catalog.")
        clickup_user_id = admin.clickup_user_id
        if not clickup_user_id:
            resolve_member = getattr(self.runtime.clickup, "resolve_workspace_member_id", None)
            if callable(resolve_member):
                clickup_user_id = await resolve_member(name=admin.name, email=admin.clickup_user_email)
        if not clickup_user_id:
            raise ValueError("Your Slack profile is not mapped to a ClickUp member yet, so assignment was not changed.")
        if candidate.get("assigned"):
            message = f"{candidate['name']} is already assigned to you in ClickUp."
        else:
            await self.runtime.clickup.update_task_assignees(task_id, add_user_ids=[str(clickup_user_id)])
            self._task_source_cache.pop(admin.slack_user_id, None)
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

    def _start_work(self, state: dict[str, Any], payload: dict[str, Any]) -> str:
        work = state["work"]
        if not work.get("selected_task_id"):
            raise ValueError("Choose a task before starting the timer.")
        outcome = _clean_text(payload.get("outcome"), limit=800)
        first_step = _clean_text(payload.get("first_step"), limit=500)
        estimate = _clean_text(payload.get("estimate"), limit=40).lower()
        checkpoint = _clean_text(payload.get("checkpoint"), limit=40).lower()
        if estimate not in _VALID_ESTIMATES or checkpoint not in _VALID_CHECKPOINTS:
            raise ValueError("Choose a time estimate and a checkpoint from the available options.")
        today = datetime.now(timezone.utc).date().isoformat()
        quality = state["quality"]
        if quality.get("date") != today:
            quality.update({"date": today, "weak_attempts": 0, "last_rejected_fingerprint": ""})
        issues, fingerprint = validate_work_commitment(
            outcome,
            first_step,
            previous_fingerprint=str(quality.get("last_rejected_fingerprint") or ""),
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
        return "Strong plan. The beta timer is running, and your next checkpoint is visible."

    def _check_in(self, state: dict[str, Any], payload: dict[str, Any]) -> str:
        work = state["work"]
        if work.get("status") != "active":
            raise ValueError("Start or resume work before posting a checkpoint.")
        progress = _clean_text(payload.get("progress"), limit=800)
        blocker = _clean_text(payload.get("blocker"), limit=500)
        if _is_vague(progress, minimum_words=5, minimum_characters=24):
            raise ValueError(
                "Name what changed, what now exists, or what you learned. This keeps updates useful to the next person reading them."
            )
        work["latest_progress"] = progress
        work["latest_blocker"] = blocker
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
            "status": "beta pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        state["task_requests"] = [request] + list(state.get("task_requests") or [])[:9]
        await self._post_slack(
            slack_user_id,
            (
                f"Don Pollo web beta task request (no ClickUp task created)\n"
                f"• {title}\n• Type: {task_type}\n• Why: {reason}\n"
                "This demonstrates the future Erik/George approval handoff."
            ),
        )
        return "Beta task request saved and sent to your Slack DM for approval-flow review."

    async def _share_to_slack(self, slack_user_id: str, state: dict[str, Any]) -> str:
        work = state["work"]
        task_name = str(work.get("selected_task_name") or "No task selected")
        lines = [
            "Don Pollo web beta summary (preview only)",
            f"• Task: {task_name}",
            f"• Intended result: {work.get('outcome') or 'not entered'}",
            f"• First step: {work.get('first_step') or 'not entered'}",
            f"• Estimate / checkpoint: {work.get('estimate') or '—'} / {work.get('checkpoint') or '—'}",
        ]
        if work.get("latest_progress"):
            lines.append(f"• Progress: {work['latest_progress']}")
        if work.get("latest_blocker"):
            lines.append(f"• Blocker: {work['latest_blocker']}")
        lines.append("No live time or payroll records were changed; only an explicit Claim action can add the tester as a ClickUp assignee.")
        await self._post_slack(slack_user_id, "\n".join(lines))
        return "A concise beta summary was sent to your Slack DM."

    async def _post_slack(self, slack_user_id: str, message: str) -> None:
        if not self.runtime.slack:
            raise ValueError("Slack is unavailable right now; the beta state was not sent.")
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
        admin: AdminProfile,
        state: dict[str, Any],
        tasks: list[dict[str, Any]],
        task_warning: str,
    ) -> dict[str, Any]:
        work = state["work"]
        now = datetime.now(timezone.utc)
        minimum = _parse_datetime(work.get("break_minimum_end_at"))
        cutoff = _parse_datetime(work.get("break_cutoff_at"))
        return {
            "beta": True,
            "actor": {"name": admin.name, "slack_user_id": admin.slack_user_id},
            "profile": state["profile"],
            "work": work,
            "quality": state["quality"],
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
  <title>Don Pollo Workday Beta</title>
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
    .hero-main { background:var(--ink); color:white; border-color:var(--ink); }
    .hero-main p { color:#d4e0da; max-width:66ch; }
    .hero-main strong { color:#ffd3ad; }
    .notice { border-left:4px solid var(--orange); padding:12px 14px; border-radius:10px; background:#fff3e8; color:#653819; }
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
    @media (max-width:900px) { .layout { grid-template-columns:1fr; } .topbar { position:static; } .task-grid { grid-template-columns:repeat(5,240px); } }
    @media (max-width:600px) { .shell { width:min(100% - 16px,100%); margin:8px auto 40px; } .topbar,.panel { border-radius:16px; padding:16px; } .topbar { align-items:flex-start; } .status { display:none; } .form-grid,.profile-summary,.catalog-tools { grid-template-columns:1fr; } .task-grid { grid-template-columns:1fr; overflow:visible; } .task { min-height:0; } .actions .button { min-height:44px; } .work-now .actions .primary { flex:1 1 100%; } .catalog-task { grid-template-columns:1fr; } .catalog-task .button { width:100%; } }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div class="brand"><div class="mark">DP</div><div><div class="eyebrow">Cislune work system</div><h1>Don Pollo Workday</h1></div></div>
      <div class="status"><span class="pill beta">Erik-only beta</span><span class="pill" id="clock-pill">Ready</span><span class="pill">Slack connected</span></div>
    </header>
    <section class="panel work-now" id="work-now">
      <div class="section-head"><div><div class="eyebrow">Work now</div><h2>Start or continue useful work</h2></div><span class="pill" id="quality-pill">0 strong plans</span></div>
      <div id="work-notice"></div>
      <div class="selected-task"><small class="muted">SELECTED TASK</small><div id="selected-task">Choose an option below.</div></div>
      <div class="form-grid">
        <label>By the next checkpoint, what will exist or be demonstrably different?<textarea id="outcome" placeholder="Example: A tested mounting bracket CAD revision with the hole pattern corrected and a screenshot attached."></textarea></label>
        <label>What is the first concrete move?<textarea id="first-step" placeholder="Example: Open revision 3, measure the current hole spacing, and update the sketch constraints."></textarea></label>
        <label>Rough estimate<select id="estimate"><option>30 minutes</option><option selected>1 hour</option><option>2 hours</option><option>half day</option><option>full day</option><option>multi-day</option></select></label>
        <label>Reconsider / ask for help after<select id="checkpoint"><option>30 minutes</option><option selected>60 minutes</option><option>90 minutes</option><option>2 hours</option></select></label>
      </div>
      <div class="actions"><button class="button primary" data-action="start">Start beta work</button><button class="button orange" id="claim-selected" data-action="claim-task">Claim in ClickUp</button><button class="button subtle" data-action="short-rest">Short rest</button><button class="button subtle" data-action="lunch">Lunch</button><button class="button subtle" data-action="back">Back</button><button class="button danger subtle" data-action="clock-out">Clock out</button></div>
      <p class="muted" style="margin:12px 0 0">A recognizable result and concrete first move are required. Beta time and payroll stay isolated; only the clearly labeled Claim action changes ClickUp by adding you as an assignee.</p>
    </section>
    <section class="panel section-space">
      <details class="chooser-details" id="recommended-details" open>
        <summary class="chooser-summary"><span><span class="eyebrow">Quick choice</span><br><h2>Five best next options</h2></span><span class="pill" id="task-count">5 options</span></summary>
        <div class="chooser-body"><p class="muted">Ranked from your assignments, priorities, active work, interests, and skills.</p><div class="notice" id="task-warning" hidden></div><div class="task-grid" id="task-grid"></div></div>
      </details>
    </section>
    <section class="panel section-space">
      <details class="chooser-details" id="catalog-details" open>
        <summary class="chooser-summary"><span><span class="eyebrow">Explore Cislune work</span><br><h2>Browse all potential tasks</h2></span><span class="pill" id="task-total">0 open tasks</span></summary>
        <div class="chooser-body"><p class="muted">Open ClickUp work organized by contract or program, then space and list.</p><div class="catalog-tools"><label>Search tasks, contracts, or spaces<input id="task-search" type="search" placeholder="Search all open work"></label><label class="inline-check"><input id="assigned-only" type="checkbox"> Assigned to me only</label></div><div class="catalog" id="task-catalog"></div><details><summary>Nothing fits? Propose work for approval</summary><div class="form-grid" style="margin-top:12px"><label>Task title<input id="request-title" placeholder="Label and organize the electronics bench"></label><label>Type<select id="request-type"><option value="project">Project</option><option value="overhead">Overhead / shop</option></select></label><label style="grid-column:1/-1">Why this should be done<textarea id="request-reason" placeholder="What result or problem does this address?"></textarea></label></div><div class="actions"><button class="button" data-action="request-task">Send beta approval request to Slack</button></div></details></div>
      </details>
    </section>
    <div class="layout">
      <div class="stack">
        <section class="panel">
          <div class="eyebrow">Checkpoint</div><h2>Say what changed, not just that you worked.</h2>
          <div class="form-grid"><label>Meaningful progress<textarea id="progress" placeholder="What now exists, changed, passed, failed, or was learned?"></textarea></label><label>Blocker or help needed<textarea id="blocker" placeholder="Optional. Name the decision, dependency, or failed approach."></textarea></label></div>
          <div class="actions"><button class="button primary" data-action="check-in">Save checkpoint</button><button class="button" data-action="share-slack">Send concise beta summary to Slack</button></div>
        </section>
      </div>
      <aside class="stack">
        <section class="panel"><div class="eyebrow">Worker context</div><h2 id="welcome">Welcome</h2><p class="muted" id="schedule-line"></p><div class="profile-summary"><div class="fact"><small>Skills</small><span id="skills-line">—</span></div><div class="fact"><small>Interests</small><span id="interests-line">—</span></div></div><details id="profile-details" open><summary id="profile-summary-label">Set schedule & fit</summary><div style="margin-top:12px" class="stack"><label>Weekly target hours<input id="weekly-hours" type="number" min="1" max="80" step="0.5"></label><label>Regular workdays<div class="days" id="workdays"></div></label><div class="form-grid"><label>Typical start<input id="start-time" type="time"></label><label>Typical end<input id="end-time" type="time"></label></div><label>Planned time off<input id="time-off" placeholder="2026-08-21, 2026-08-24"></label><label>Interests<input id="interests" placeholder="robotics, fabrication, flight testing"></label><label>Skills<input id="skills" placeholder="CAD, Python, assembly"></label><button class="button" data-action="save-profile">Save worker context</button></div></details></section>
        <section class="panel"><div class="section-head"><div><div class="eyebrow">Recent beta activity</div><h2>Audit trail</h2></div></div><div class="history" id="history"></div><div class="actions"><button class="button subtle danger" data-action="reset-beta">Reset beta</button></div></section>
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
    function renderTaskCatalog() {
      const query = String($('task-search').value || '').trim().toLowerCase();
      const assignedOnly = $('assigned-only').checked;
      const openGroups = new Set([...document.querySelectorAll('#task-catalog details[open]')].map(node => node.dataset.groupKey));
      const contracts = (data.task_catalog || []).map((contract) => {
        const flattenOnlyOtherSpace = (contract.spaces || []).length === 1 && String(contract.spaces[0].name || '').toLowerCase() === 'other';
        const spaces = (contract.spaces || []).map((space) => {
          const lists = (space.lists || []).map((taskList) => {
            const visibleTasks = (taskList.tasks || []).filter((task) => {
              if (assignedOnly && !task.assigned) return false;
              const searchable = [task.name,task.status,task.priority,task.location,contract.name,space.name,taskList.name].join(' ').toLowerCase();
              return !query || searchable.includes(query);
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
      const work = data.work || {}, profile = data.profile || {}, quality = data.quality || {};
      $('welcome').textContent = `Welcome, ${(data.actor || {}).name || 'beta tester'}`;
      $('clock-pill').textContent = String(work.status || 'ready').replaceAll('_',' ');
      $('clock-pill').classList.toggle('running', work.status === 'active');
      $('work-notice').innerHTML = work.notice ? `<div class="notice">${esc(work.notice)}</div>` : '';
      $('schedule-line').textContent = `${Number(profile.weekly_target_hours || 0)} hours/week · ${(profile.regular_workdays || []).map(day => day.slice(0,3)).join(', ')} · ${profile.typical_start_time || '—'}–${profile.typical_end_time || '—'}`;
      $('skills-line').textContent = (profile.skills || []).join(', ') || 'Not set';
      $('interests-line').textContent = (profile.interests || []).join(', ') || 'Not set';
      $('task-warning').hidden = !data.task_warning;
      $('task-warning').textContent = data.task_warning || '';
      const tasks = data.task_options || [];
      $('task-count').textContent = `${tasks.length} option${tasks.length === 1 ? '' : 's'}`;
      $('task-total').textContent = `${Number(data.task_total || 0)} open tasks`;
      $('task-grid').innerHTML = tasks.map((task,index) => `<button class="task ${task.id === work.selected_task_id ? 'selected' : ''}" data-select-task="${esc(task.id)}" data-task-name="${esc(task.name)}" data-task-location="${esc(task.location)}"><span class="number">${index+1}</span><h3>${esc(task.name)}</h3><div class="tagrow">${task.recommended ? '<span class="tag recommended">Recommended</span>' : ''}${task.assigned ? '<span class="tag">Assigned to you</span>' : ''}<span class="tag">${esc(task.priority)}</span><span class="tag">${esc(task.status)}</span>${task.due_date ? `<span class="tag">Due ${esc(task.due_date)}</span>` : ''}</div><div class="muted">${esc(task.location)}</div><div class="why">Why: ${esc(task.reason)}</div></button>`).join('');
      renderTaskCatalog();
      $('selected-task').innerHTML = work.selected_task_name ? `<strong>${esc(work.selected_task_name)}</strong><br><span class="muted">${esc(work.selected_task_location || '')}</span>` : 'Choose an option above.';
      $('outcome').value = work.outcome || '';
      $('first-step').value = work.first_step || '';
      $('estimate').value = work.estimate || '1 hour';
      $('checkpoint').value = work.checkpoint || '60 minutes';
      $('progress').value = work.latest_progress || '';
      $('blocker').value = work.latest_blocker || '';
      $('quality-pill').textContent = `${Number(quality.strong_plans || 0)} strong plan${Number(quality.strong_plans || 0) === 1 ? '' : 's'}`;
      $('weekly-hours').value = profile.weekly_target_hours || 40;
      $('start-time').value = profile.typical_start_time || '09:00';
      $('end-time').value = profile.typical_end_time || '17:00';
      $('time-off').value = (profile.planned_time_off || []).join(', ');
      $('interests').value = (profile.interests || []).join(', ');
      $('skills').value = (profile.skills || []).join(', ');
      $('workdays').innerHTML = days.map(day => `<label class="day"><input type="checkbox" value="${day}" ${(profile.regular_workdays || []).includes(day) ? 'checked' : ''}><span>${day.slice(0,3)}</span></label>`).join('');
      const savedAt = String(profile.saved_at || '');
      if (renderedProfileSavedAt === null || renderedProfileSavedAt !== savedAt) $('profile-details').open = !savedAt;
      renderedProfileSavedAt = savedAt;
      $('profile-summary-label').textContent = savedAt ? 'Profile saved · Edit schedule & fit' : 'Set schedule & fit';
      if (!chooserInitialized) {
        $('recommended-details').open = !work.selected_task_id;
        $('catalog-details').open = !work.selected_task_id;
        chooserInitialized = true;
      }
      $('history').innerHTML = (data.history || []).length ? (data.history || []).map(item => `<article><strong>${esc(String(item.action || '').replaceAll('_',' '))}</strong><br>${esc(item.message)}<br><time>${new Date(item.at).toLocaleString()}</time></article>`).join('') : '<p class="muted">No beta actions yet.</p>';
      document.querySelector('[data-action="back"]').disabled = !['short_rest','lunch'].includes(work.status);
      document.querySelector('[data-action="short-rest"]').disabled = work.status !== 'active';
      document.querySelector('[data-action="lunch"]').disabled = work.status !== 'active';
      document.querySelector('[data-action="clock-out"]').disabled = ['ready','clocked_out'].includes(work.status);
      const selectedOption = (data.task_options || []).find(task => task.id === work.selected_task_id) || (data.task_catalog || []).flatMap(contract => contract.spaces || []).flatMap(space => space.lists || []).flatMap(taskList => taskList.tasks || []).find(task => task.id === work.selected_task_id);
      $('claim-selected').disabled = !work.selected_task_id || Boolean(selectedOption && selectedOption.assigned);
      $('claim-selected').textContent = selectedOption && selectedOption.assigned ? 'Already mine in ClickUp' : 'Claim in ClickUp';
    }
    function actionPayload(action) {
      if (action === 'start') return {action, outcome:$('outcome').value, first_step:$('first-step').value, estimate:$('estimate').value, checkpoint:$('checkpoint').value};
      if (action === 'check-in') return {action, progress:$('progress').value, blocker:$('blocker').value};
      if (action === 'claim-task') return {action:'claim_task', task_id:(data.work || {}).selected_task_id};
      if (action === 'save-profile') return {action:'save_profile', weekly_target_hours:$('weekly-hours').value, regular_workdays:[...document.querySelectorAll('#workdays input:checked')].map(node => node.value), typical_start_time:$('start-time').value, typical_end_time:$('end-time').value, planned_time_off:$('time-off').value, interests:$('interests').value, skills:$('skills').value};
      if (action === 'request-task') return {action:'request_task', title:$('request-title').value, task_type:$('request-type').value, reason:$('request-reason').value};
      return {action: action.replaceAll('-','_')};
    }
    async function post(payload, button) {
      const original = button ? button.textContent : '';
      if (button) { button.disabled = true; button.textContent = 'Working…'; }
      try {
        const response = await fetch(`/api/portal/action?token=${encodeURIComponent(token)}`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
        data = result; render(); showToast(result.message || 'Saved.'); return true;
      } catch (error) { showToast(error.message || String(error), true); return false; }
      finally { if (button && button.isConnected) { button.disabled = false; button.textContent = original; } }
    }
    document.addEventListener('click', (event) => {
      const claim = event.target.closest('[data-claim-task]');
      if (claim) return post({action:'claim_task',task_id:claim.dataset.claimTask},claim).then((saved) => { if (!saved) return; $('recommended-details').open=false; $('catalog-details').open=false; if (window.innerWidth <= 600) $('work-now').scrollIntoView({behavior:'smooth',block:'start'}); });
      const task = event.target.closest('[data-select-task]');
      if (task) return post({action:'select_task',task_id:task.dataset.selectTask,task_name:task.dataset.taskName,task_location:task.dataset.taskLocation},task).then((saved) => { if (!saved) return; $('recommended-details').open=false; $('catalog-details').open=false; if (window.innerWidth <= 600) $('work-now').scrollIntoView({behavior:'smooth',block:'start'}); });
      const button = event.target.closest('[data-action]');
      if (button) post(actionPayload(button.dataset.action), button);
    });
    $('task-search').addEventListener('input', renderTaskCatalog);
    $('assigned-only').addEventListener('change', renderTaskCatalog);
    let toastTimer;
    function showToast(message,error=false) { const node=$('toast'); node.textContent=message; node.className=error?'show error':'show'; clearTimeout(toastTimer); toastTimer=setTimeout(()=>node.className='',7000); }
    render();
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

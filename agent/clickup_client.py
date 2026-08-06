from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from .models import AgentConfig, ClickUpContextBundle, MessageRecord, SessionState, UserProfile


logger = logging.getLogger(__name__)
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_GET_ATTEMPTS = 3
_CLICKUP_TASK_PAGE_SIZE = 100
_WORKSPACE_TASK_SCAN_LIMIT = 500

_PRIORITY_TO_INT = {
    "urgent": 1,
    "high": 2,
    "normal": 3,
    "low": 4,
}

_STATE_KEYWORDS = {
    "in_progress": ("in progress", "progress", "active", "working"),
    "hold": ("hold", "blocked", "stuck", "waiting"),
    "complete": ("complete", "completed", "done", "closed"),
}

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "have",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "our",
    "so",
    "that",
    "the",
    "this",
    "to",
    "up",
    "we",
    "with",
    "work",
}


class ClickUpClient:
    def __init__(self, api_token: str, config: AgentConfig) -> None:
        self.api_token = api_token
        self.config = config
        self.base_url = "https://api.clickup.com/api/v2"
        self._workspace_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._assignee_timer_query_supported: bool | None = None
        self._assignee_timer_start_supported: bool | None = None

    async def get_context_bundle(
        self,
        user: UserProfile,
        session: SessionState,
        messages: list[MessageRecord],
        *,
        preferred_task_id: str | None = None,
        preferred_task_name: str | None = None,
        preferred_selection_reason: str | None = None,
    ) -> ClickUpContextBundle:
        tasks = await self._load_candidate_tasks(user)
        if not tasks:
            return ClickUpContextBundle()

        scored = self._rank_tasks(tasks, session, messages)
        best_score, best_reason, best_task = scored[0]
        active_task_id: str | None = None
        active_task_name: str | None = None
        selection_reason: str | None = None
        if best_score > 0:
            active_task_id = str(best_task.get("id"))
            active_task_name = best_task.get("name")
            selection_reason = best_reason
        context_task_id = active_task_id
        context_selection_reason = selection_reason
        if preferred_task_id:
            matched_preferred = next(
                (
                    (reason, task)
                    for _, reason, task in scored
                    if str(task.get("id") or "") == preferred_task_id
                ),
                None,
            )
            if matched_preferred is not None:
                matched_reason, matched_task = matched_preferred
                context_task_id = str(matched_task.get("id") or preferred_task_id)
                if preferred_task_name:
                    matched_task["name"] = preferred_task_name
                context_selection_reason = preferred_selection_reason or matched_reason
        context = self._format_ranked_context(scored, context_task_id, context_selection_reason)
        candidate_task_ids = [str(task.get("id")) for _, _, task in scored[:5] if task.get("id")]
        return ClickUpContextBundle(
            context=context,
            active_task_id=active_task_id,
            active_task_name=active_task_name,
            selection_reason=selection_reason,
            candidate_task_ids=candidate_task_ids,
        )

    async def resolve_clickup_user_id(self, user: UserProfile) -> str | None:
        if user.clickup_user_id:
            return user.clickup_user_id
        members = await self._list_workspace_members()
        if not members:
            return None
        matches = [member for member in members if self._member_matches(user, member)]
        if len(matches) == 1:
            return str(matches[0].get("id"))
        if len(matches) > 1:
            logger.warning(
                "Multiple ClickUp members matched roster user %s. Set clickup_user_id or clickup_user_email to disambiguate.",
                user.user_key,
            )
        else:
            logger.warning(
                "Could not match roster user %s to a ClickUp member in workspace %s. "
                "Set clickup_user_id or clickup_user_email in roster.csv.",
                user.user_key,
                self.config.clickup.workspace_id,
            )
        return None

    async def list_workspace_members(self) -> list[dict[str, Any]]:
        return await self._list_workspace_members()

    async def resolve_workspace_member_id(
        self,
        *,
        name: str | None = None,
        email: str | None = None,
    ) -> str | None:
        members = await self._list_workspace_members()
        if not members:
            return None
        if email:
            normalized_email = email.strip().lower()
            for member in members:
                member_email = str(member.get("email") or "").strip().lower()
                if member_email and member_email == normalized_email and member.get("id") is not None:
                    return str(member.get("id"))
        if name:
            normalized_name = self._normalize_identifier(name)
            if normalized_name:
                exact_matches = [
                    member
                    for member in members
                    if self._normalize_identifier(str(member.get("username") or "")) == normalized_name
                ]
                if len(exact_matches) == 1 and exact_matches[0].get("id") is not None:
                    return str(exact_matches[0].get("id"))
                partial_matches = [
                    member
                    for member in members
                    if normalized_name in self._normalize_identifier(str(member.get("username") or ""))
                ]
                if len(partial_matches) == 1 and partial_matches[0].get("id") is not None:
                    return str(partial_matches[0].get("id"))
        return None

    async def post_update(
        self,
        user: UserProfile,
        task_id: str | None,
        comment_text: str,
        summary: str,
        session_timestamp_ms: int,
        blocker_text: str | None = None,
    ) -> str | None:
        if not task_id:
            return None
        try:
            if self.config.clickup.comment_updates:
                await asyncio.to_thread(
                    self._request,
                    "POST",
                    f"/task/{task_id}/comment",
                    json={
                        "comment_text": comment_text,
                        "notify_all": False,
                    },
                )
            if field_id := self.config.clickup.custom_fields.get("last_check_in_at"):
                await asyncio.to_thread(
                    self._request,
                    "POST",
                    f"/task/{task_id}/field/{field_id}",
                    json={"value": session_timestamp_ms, "value_options": {"time": True}},
                )
            if field_id := self.config.clickup.custom_fields.get("status_summary"):
                await asyncio.to_thread(
                    self._request,
                    "POST",
                    f"/task/{task_id}/field/{field_id}",
                    json={"value": summary[:2000]},
                )
            if blocker_text and (field_id := self.config.clickup.custom_fields.get("blockers")):
                await asyncio.to_thread(
                    self._request,
                    "POST",
                    f"/task/{task_id}/field/{field_id}",
                    json={"value": blocker_text[:2000]},
                )
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.warning(
                    "Skipping ClickUp update for missing task %s (user %s).",
                    task_id,
                    user.user_key,
                )
                return None
            raise
        return task_id

    async def comment_on_task(self, task_id: str, comment_text: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._request,
            "POST",
            f"/task/{task_id}/comment",
            json={
                "comment_text": comment_text,
                "notify_all": False,
            },
        )

    async def upload_task_attachment(self, task_id: str, file_path: Path) -> dict:
        return await asyncio.to_thread(self._upload_task_attachment_sync, task_id, file_path)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "GET", f"/task/{task_id}")

    async def list_assigned_tasks(self, user: UserProfile, limit: int = 25) -> list[dict[str, Any]]:
        return (await self._load_candidate_tasks(user))[:limit]

    async def build_assigned_task_hierarchy(
        self,
        user: UserProfile,
        *,
        tasks: list[dict[str, Any]] | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        assigned_tasks = list(tasks) if tasks is not None else await self.list_assigned_tasks(user, limit=limit)
        tasks_by_id: dict[str, dict[str, Any]] = {}
        assigned_task_ids: list[str] = []
        for task in assigned_tasks[:limit]:
            task_id = str(task.get("id") or "").strip()
            if not task_id:
                continue
            tasks_by_id[task_id] = task
            if task_id not in assigned_task_ids:
                assigned_task_ids.append(task_id)
        pending_parent_ids = [
            parent_id
            for task in assigned_tasks
            for parent_id in [self._parent_task_id(task)]
            if parent_id and parent_id not in tasks_by_id
        ]
        while pending_parent_ids:
            parent_id = pending_parent_ids.pop()
            if not parent_id or parent_id in tasks_by_id:
                continue
            try:
                parent_task = await self.get_task(parent_id)
            except requests.HTTPError:
                logger.warning("Could not fetch ClickUp parent task %s while building assigned task hierarchy.", parent_id)
                continue
            if not isinstance(parent_task, dict):
                continue
            tasks_by_id[parent_id] = parent_task
            next_parent_id = self._parent_task_id(parent_task)
            if next_parent_id and next_parent_id not in tasks_by_id:
                pending_parent_ids.append(next_parent_id)

        included_task_ids: set[str] = set()
        for task_id in assigned_task_ids:
            current_task_id: str | None = task_id
            while current_task_id and current_task_id not in included_task_ids:
                included_task_ids.add(current_task_id)
                parent_id = self._parent_task_id(tasks_by_id.get(current_task_id, {}))
                current_task_id = parent_id if parent_id in tasks_by_id else None

        children_by_parent_id: dict[str, list[str]] = {}
        root_ids: list[str] = []
        for task_id in included_task_ids:
            task = tasks_by_id[task_id]
            parent_id = self._parent_task_id(task)
            if parent_id and parent_id in included_task_ids:
                children = children_by_parent_id.setdefault(parent_id, [])
                if task_id not in children:
                    children.append(task_id)
                continue
            root_ids.append(task_id)

        def _sort_task_ids(task_ids: list[str]) -> list[str]:
            return sorted(
                task_ids,
                key=lambda item: (
                    self._task_created_sort_key(tasks_by_id.get(item, {})),
                    str((tasks_by_id.get(item, {}) or {}).get("name") or "").strip().lower(),
                    item,
                ),
            )

        root_ids = _sort_task_ids(list(dict.fromkeys(root_ids)))
        children_by_parent_id = {
            parent_id: _sort_task_ids(child_ids)
            for parent_id, child_ids in children_by_parent_id.items()
        }
        return {
            "tasks_by_id": {
                task_id: tasks_by_id[task_id]
                for task_id in included_task_ids
                if task_id in tasks_by_id
            },
            "assigned_task_ids": assigned_task_ids,
            "root_ids": root_ids,
            "children_by_parent_id": children_by_parent_id,
        }

    def render_assigned_task_hierarchy(
        self,
        hierarchy: dict[str, Any],
        *,
        recommended_task_id: str | None = None,
    ) -> str:
        tasks_by_id = hierarchy.get("tasks_by_id")
        tasks_by_id = tasks_by_id if isinstance(tasks_by_id, dict) else {}
        assigned_task_ids = {
            str(task_id)
            for task_id in (hierarchy.get("assigned_task_ids") or [])
            if str(task_id).strip()
        }
        root_ids = [
            str(task_id)
            for task_id in (hierarchy.get("root_ids") or [])
            if str(task_id).strip()
        ]
        raw_children = hierarchy.get("children_by_parent_id")
        raw_children = raw_children if isinstance(raw_children, dict) else {}
        children_by_parent_id: dict[str, list[str]] = {
            str(parent_id): [str(child_id) for child_id in child_ids if str(child_id).strip()]
            for parent_id, child_ids in raw_children.items()
            if isinstance(child_ids, list)
        }
        lines: list[str] = []

        def _task_label(task_id: str) -> str:
            task = tasks_by_id.get(task_id) or {}
            task_name = str(task.get("name") or task_id)
            markers: list[str] = []
            if task_id in assigned_task_ids:
                markers.append("assigned")
            if recommended_task_id and task_id == recommended_task_id:
                markers.append("recommended")
            marker_suffix = f" [{' | '.join(markers)}]" if markers else ""
            return f"{task_name} | id={task_id}{marker_suffix}"

        def _walk(task_id: str, prefix: str, is_last: bool) -> None:
            connector = "\\- " if is_last else "|- "
            lines.append(f"{prefix}{connector}{_task_label(task_id)}")
            child_ids = children_by_parent_id.get(task_id, [])
            child_prefix = prefix + ("   " if is_last else "|  ")
            for index, child_id in enumerate(child_ids):
                _walk(child_id, child_prefix, index == len(child_ids) - 1)

        for index, task_id in enumerate(root_ids):
            _walk(task_id, "", index == len(root_ids) - 1)
        return "\n".join(lines)

    def match_task_hint(self, tasks: list[dict[str, Any]], task_hint: str) -> dict[str, Any] | None:
        return self._match_task_hint(tasks, task_hint)

    async def list_workspace_tasks(self, limit: int = 100, include_closed: bool = False) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        page = 0
        while len(tasks) < limit:
            payload = await asyncio.to_thread(
                self._request,
                "GET",
                f"/team/{self.config.clickup.workspace_id}/task",
                params={
                    "page": page,
                    "order_by": "updated",
                    "reverse": "true",
                    "include_closed": str(include_closed).lower(),
                    "subtasks": "true",
                },
            )
            page_tasks = [task for task in payload.get("tasks", []) if isinstance(task, dict)]
            new_task_count = 0
            for task in page_tasks:
                task_id = str(task.get("id") or "").strip()
                if task_id and task_id in seen_ids:
                    continue
                if task_id:
                    seen_ids.add(task_id)
                tasks.append(task)
                new_task_count += 1
                if len(tasks) >= limit:
                    break
            if len(page_tasks) < _CLICKUP_TASK_PAGE_SIZE or new_task_count == 0:
                break
            page += 1
        return tasks[:limit]

    async def get_list(self, list_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "GET", f"/list/{list_id}")

    async def list_list_tasks(
        self,
        list_id: str,
        limit: int = 100,
        include_closed: bool = False,
    ) -> list[dict[str, Any]]:
        payload = await asyncio.to_thread(
            self._request,
            "GET",
            f"/list/{list_id}/task",
            params={"archived": "false", "subtasks": "true"},
        )
        tasks = list(payload.get("tasks", []))
        if not include_closed:
            tasks = [task for task in tasks if not self._task_is_closed(task)]
        return tasks[:limit]

    async def update_task_status(self, task_id: str, status_name: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._request,
            "PUT",
            f"/task/{task_id}",
            json={"status": status_name},
        )

    async def update_task_assignees(
        self,
        task_id: str,
        *,
        add_user_ids: list[str] | None = None,
        remove_user_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "assignees": {
                "add": [int(user_id) for user_id in add_user_ids or []],
                "rem": [int(user_id) for user_id in remove_user_ids or []],
            }
        }
        return await asyncio.to_thread(
            self._request,
            "PUT",
            f"/task/{task_id}",
            json=payload,
        )

    async def set_task_state(self, task_id: str, state: str) -> str | None:
        task = await self.get_task(task_id)
        current_status = str((task.get("status") or {}).get("status") or "")
        if self._status_matches_state(current_status, state):
            return current_status or None
        list_id = str(((task.get("list") or {}).get("id")) or "")
        if not list_id:
            return None
        list_payload = await self.get_list(list_id)
        statuses = list_payload.get("statuses", [])
        target = self._resolve_status_name(statuses, state)
        if not target:
            logger.warning("No ClickUp status in list %s matched desired state %s.", list_id, state)
            return None
        updated = await self.update_task_status(task_id, target)
        updated_status = str((updated.get("status") or {}).get("status") or "")
        if self._status_matches_state(updated_status, state):
            return updated_status or target
        refreshed = await self.get_task(task_id)
        refreshed_status = str((refreshed.get("status") or {}).get("status") or "")
        if self._status_matches_state(refreshed_status, state):
            return refreshed_status or target
        logger.warning(
            "ClickUp task %s did not confirm transition to %s. Current status is %r.",
            task_id,
            state,
            refreshed_status or updated_status or current_status,
        )
        return None

    async def create_task(
        self,
        list_id: str,
        *,
        name: str,
        description: str,
        assignee_ids: list[str] | None = None,
        priority: str | None = None,
        due_date: int | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        parent_task_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "description": description,
            "notify_all": False,
        }
        if assignee_ids:
            payload["assignees"] = [int(assignee_id) for assignee_id in assignee_ids]
        priority_value = self._priority_to_api_value(priority)
        if priority_value is not None:
            payload["priority"] = priority_value
        if due_date is not None:
            payload["due_date"] = due_date
            payload["due_date_time"] = False
        if status:
            payload["status"] = status
        if tags:
            payload["tags"] = tags
        if parent_task_id:
            payload["parent"] = parent_task_id
        created = await asyncio.to_thread(
            self._request,
            "POST",
            f"/list/{list_id}/task",
            json=payload,
        )
        if assignee_ids and created.get("id"):
            created_assignee_ids = {
                str(assignee.get("id"))
                for assignee in (created.get("assignees") or [])
                if isinstance(assignee, dict) and assignee.get("id") is not None
            }
            missing_assignee_ids = [
                assignee_id for assignee_id in assignee_ids
                if str(assignee_id) not in created_assignee_ids
            ]
            if missing_assignee_ids:
                await self.update_task_assignees(str(created.get("id")), add_user_ids=missing_assignee_ids)
                refreshed = await self.get_task(str(created.get("id")))
                if refreshed:
                    return refreshed
        return created

    async def create_time_entry(
        self,
        *,
        assignee_id: str,
        task_id: str | None,
        start_ms: int,
        stop_ms: int,
        description: str,
    ) -> dict[str, Any] | None:
        if stop_ms <= start_ms:
            return None
        payload: dict[str, Any] = {
            "start": start_ms,
            "stop": stop_ms,
            "description": description[:1000],
            "assignee": int(assignee_id),
            "billable": False,
        }
        if task_id:
            payload["tid"] = task_id
        try:
            return await asyncio.to_thread(
                self._request,
                "POST",
                f"/team/{self.config.clickup.workspace_id}/time_entries",
                json=payload,
            )
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            logger.warning(
                "Failed to create ClickUp time entry for assignee %s on task %s: %s",
                assignee_id,
                task_id or "<none>",
                status_code or exc,
            )
            return None

    async def get_running_time_entry(self, assignee_id: str | None = None) -> dict[str, Any] | None:
        params = {"assignee": assignee_id} if assignee_id else None
        try:
            payload = await asyncio.to_thread(
                self._request,
                "GET",
                f"/team/{self.config.clickup.workspace_id}/time_entries/current",
                params=params,
            )
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if assignee_id and status_code == 403:
                self._assignee_timer_query_supported = False
                logger.warning(
                    "Current ClickUp token is not allowed to query running time entries for assignee %s.",
                    assignee_id,
                )
                return None
            if status_code in {404, 422}:
                return None
            raise
        if assignee_id:
            self._assignee_timer_query_supported = True
        if not payload:
            return None
        if isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload

    async def start_assignee_timer(
        self,
        *,
        assignee_id: str,
        task_id: str,
        start_ms: int,
        description: str,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "start": start_ms,
            "duration": -1,
            "description": description[:1000],
            "assignee": int(assignee_id),
            "billable": False,
            "tid": task_id,
        }
        try:
            response = await asyncio.to_thread(
                self._request,
                "POST",
                f"/team/{self.config.clickup.workspace_id}/time_entries",
                json=payload,
            )
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            self._assignee_timer_start_supported = False
            logger.warning(
                "Failed to start assignee timer for user %s on task %s: %s",
                assignee_id,
                task_id,
                status_code or exc,
            )
            return None
        self._assignee_timer_start_supported = True
        if isinstance(response.get("data"), dict):
            return response["data"]
        return response

    async def close_time_entry(
        self,
        *,
        timer_id: str,
        start_ms: int,
        stop_ms: int,
        description: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any] | None:
        if stop_ms <= start_ms:
            return None
        payload: dict[str, Any] = {
            "start": start_ms,
            "end": stop_ms,
            "tags": [],
        }
        if description:
            payload["description"] = description[:1000]
        if task_id:
            payload["tid"] = task_id
        try:
            response = await asyncio.to_thread(
                self._request,
                "PUT",
                f"/team/{self.config.clickup.workspace_id}/time_entries/{timer_id}",
                json=payload,
            )
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            logger.warning(
                "Failed to close ClickUp time entry %s for task %s: %s",
                timer_id,
                task_id or "<none>",
                status_code or exc,
            )
            return None
        if isinstance(response.get("data"), dict):
            return response["data"]
        return response

    async def resolve_task_for_user(
        self,
        user: UserProfile,
        task_hint: str,
        *,
        include_mission_board: bool = False,
        include_workspace: bool = False,
    ) -> dict[str, Any] | None:
        task_hint = task_hint.strip()
        if not task_hint:
            return None
        pools: list[dict[str, Any]] = await self.list_assigned_tasks(user, limit=50)
        if include_mission_board and self.config.clickup.mission_board_list_id:
            mission_tasks = await self.list_list_tasks(self.config.clickup.mission_board_list_id, limit=100, include_closed=False)
            seen_ids = {str(task.get("id") or "") for task in pools}
            for task in mission_tasks:
                task_id = str(task.get("id") or "")
                if task_id and task_id not in seen_ids:
                    pools.append(task)
                    seen_ids.add(task_id)
        if include_workspace:
            workspace_tasks = await self.list_workspace_tasks(limit=200, include_closed=False)
            seen_ids = {str(task.get("id") or "") for task in pools}
            for task in workspace_tasks:
                task_id = str(task.get("id") or "")
                if task_id and task_id not in seen_ids:
                    pools.append(task)
                    seen_ids.add(task_id)
        return self._match_task_hint(pools, task_hint)

    async def ensure_task_assigned_to_user(self, task: dict[str, Any], user: UserProfile) -> bool:
        clickup_user_id = await self.resolve_clickup_user_id(user)
        if not clickup_user_id:
            return False
        assignee_ids = {
            str(assignee.get("id"))
            for assignee in (task.get("assignees") or [])
            if assignee.get("id") is not None
        }
        if clickup_user_id in assignee_ids:
            return True
        await self.update_task_assignees(str(task.get("id")), add_user_ids=[clickup_user_id])
        return True

    def can_query_assignee_timers(self) -> bool:
        return self._assignee_timer_query_supported is not False

    async def suggest_next_tasks(
        self,
        user: UserProfile,
        session: SessionState,
        messages: list[MessageRecord],
        *,
        exclude_task_ids: set[str] | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        tasks = await self.list_workspace_tasks(
            limit=_WORKSPACE_TASK_SCAN_LIMIT,
            include_closed=False,
        )
        open_unassigned = [
            task
            for task in tasks
            if not self._task_is_closed(task)
            and not (task.get("assignees") or [])
            and str(task.get("id") or "") not in (exclude_task_ids or set())
        ]
        scored = self._rank_next_tasks(open_unassigned, session, messages)
        suggestions: list[dict[str, Any]] = []
        for _, reason, task in scored[:limit]:
            suggestion = dict(task)
            suggestion["_don_pollo_workspace_option"] = True
            suggestion["_don_pollo_suggestion_reason"] = reason
            suggestions.append(suggestion)
        return suggestions

    @staticmethod
    def task_location_label(task: dict[str, Any]) -> str:
        labels: list[str] = []
        for key in ("space", "folder", "list"):
            payload = task.get(key)
            if not isinstance(payload, dict):
                continue
            label = str(payload.get("name") or "").strip()
            if label and label not in labels:
                labels.append(label)
        return " / ".join(labels) or "ClickUp workspace"

    @staticmethod
    def pick_highest_priority_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not tasks:
            return None
        rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3, None: 4}
        return sorted(
            tasks,
            key=lambda task: (
                rank.get(((task.get("priority") or {}).get("priority") or "").strip().lower() or None, 5),
                ClickUpClient._task_created_sort_key(task),
                ((task.get("status") or {}).get("status") or "").strip().lower(),
                str(task.get("name") or "").strip().lower(),
            ),
        )[0]

    async def check_connection(self) -> tuple[bool, str]:
        try:
            teams = await self._list_authorized_workspaces()
        except requests.HTTPError as exc:
            return False, f"ClickUp request failed: {exc}"
        workspace_id = str(self.config.clickup.workspace_id)
        for team in teams:
            if str(team.get("id")) == workspace_id:
                return True, f"Authorized workspace found: {team.get('name')} ({workspace_id})"
        return False, f"Workspace {workspace_id} was not found among authorized workspaces."

    async def _load_candidate_tasks(self, user: UserProfile) -> list[dict[str, Any]]:
        clickup_user_id = await self.resolve_clickup_user_id(user)
        if clickup_user_id:
            params: dict[str, Any] = {
                "page": 0,
                "order_by": "updated",
                "reverse": "true",
                "include_closed": "false",
                "subtasks": "true",
                "assignees[]": [clickup_user_id],
            }
            try:
                payload = await asyncio.to_thread(
                    self._request,
                    "GET",
                    f"/team/{self.config.clickup.workspace_id}/task",
                    params=params,
                )
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    logger.warning(
                        "ClickUp workspace %s was not found or is not authorized for the current token.",
                        self.config.clickup.workspace_id,
                    )
                    return []
                raise
            tasks = [
                task
                for task in list(payload.get("tasks", []))
                if not self._task_is_closed(task)
            ][:25]
            if tasks:
                return tasks
            fallback_tasks = await self._workspace_assignee_fallback_tasks(clickup_user_id=clickup_user_id)
            if fallback_tasks:
                logger.warning(
                    "ClickUp filtered assignee query returned no tasks for user %s (%s). "
                    "Recovered %s task(s) via workspace scan fallback.",
                    user.user_key,
                    clickup_user_id,
                    len(fallback_tasks),
                )
            return fallback_tasks
        list_id = self.config.clickup.default_list_id
        if not list_id:
            return []
        try:
            payload = await asyncio.to_thread(self._request, "GET", f"/list/{list_id}/task")
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.warning(
                    "ClickUp list %s for user %s was not found. Returning no ClickUp context.",
                    list_id,
                    user.user_key,
                )
                return []
            raise
        return [
            task
            for task in list(payload.get("tasks", []))
            if not self._task_is_closed(task)
        ][:10]

    async def _workspace_assignee_fallback_tasks(
        self,
        *,
        clickup_user_id: str,
    ) -> list[dict[str, Any]]:
        tasks = await self.list_workspace_tasks(limit=250, include_closed=False)
        filtered: list[dict[str, Any]] = []
        for task in tasks:
            if self._task_is_closed(task):
                continue
            assignees = task.get("assignees") or []
            if any(str(assignee.get("id")) == clickup_user_id for assignee in assignees):
                filtered.append(task)
        return filtered[:25]

    async def _list_workspace_members(self) -> list[dict[str, Any]]:
        workspaces = await self._list_authorized_workspaces()
        workspace_id = str(self.config.clickup.workspace_id)
        for workspace in workspaces:
            if str(workspace.get("id")) != workspace_id:
                continue
            members: list[dict[str, Any]] = []
            for member in workspace.get("members", []):
                member_user = member.get("user") or {}
                if member_user.get("id") is not None:
                    members.append(member_user)
            return members
        logger.warning(
            "Configured ClickUp workspace %s was not found among the authorized workspaces for this token.",
            workspace_id,
        )
        return []

    def _rank_next_tasks(
        self,
        tasks: list[dict[str, Any]],
        session: SessionState,
        messages: list[MessageRecord],
    ) -> list[tuple[int, str, dict[str, Any]]]:
        evidence_tokens = self._build_evidence_tokens(session, messages)
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for task in tasks:
            score, reason = self._score_next_task(task, evidence_tokens)
            scored.append((score, reason, task))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored

    async def _list_authorized_workspaces(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._workspace_cache and now - self._workspace_cache[0] < 300:
            return self._workspace_cache[1]
        payload = await asyncio.to_thread(self._request, "GET", "/team")
        teams = list(payload.get("teams", []))
        self._workspace_cache = (now, teams)
        return teams

    def _rank_tasks(
        self,
        tasks: list[dict[str, Any]],
        session: SessionState,
        messages: list[MessageRecord],
    ) -> list[tuple[int, str, dict[str, Any]]]:
        evidence_tokens = self._build_evidence_tokens(session, messages)
        prior_task_id = str(session.metadata.get("active_clickup_task_id") or "")
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for task in tasks:
            score, reason = self._score_task(task, evidence_tokens, prior_task_id)
            scored.append((score, reason, task))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored

    def _score_task(
        self,
        task: dict[str, Any],
        evidence_tokens: set[str],
        prior_task_id: str,
    ) -> tuple[int, str]:
        score = 0
        reasons: list[str] = []
        task_id = str(task.get("id") or "")
        if prior_task_id and task_id == prior_task_id:
            score += 25
            reasons.append("same task as earlier today")

        status = ((task.get("status") or {}).get("status") or "").strip().lower()
        if status:
            if any(word in status for word in ("progress", "active", "working")):
                score += 50
                reasons.append(f"status={status}")
            elif any(word in status for word in ("review", "qa", "test")):
                score += 35
                reasons.append(f"status={status}")
            elif any(word in status for word in ("todo", "to do", "open", "backlog")):
                score += 15
                reasons.append(f"status={status}")
            elif any(word in status for word in ("done", "complete", "closed")):
                score -= 100

        priority = ((task.get("priority") or {}).get("priority") or "").strip().lower()
        if priority == "urgent":
            score += 30
        elif priority == "high":
            score += 20
        elif priority == "normal":
            score += 10
        elif priority == "low":
            score += 5
        if priority:
            reasons.append(f"priority={priority}")

        task_text = " ".join(
            part
            for part in (
                str(task.get("name") or ""),
                str(task.get("description") or ""),
            )
            if part
        )
        task_tokens = self._tokenize(task_text)
        overlap = sorted(evidence_tokens & task_tokens)
        if overlap:
            score += min(len(overlap) * 14, 70)
            reasons.append("keyword overlap: " + ", ".join(overlap[:5]))

        if not reasons:
            reasons.append("highest-ranked assigned task")
        return score, "; ".join(reasons)

    def _score_next_task(self, task: dict[str, Any], evidence_tokens: set[str]) -> tuple[int, str]:
        score = 0
        reasons: list[str] = []
        status = ((task.get("status") or {}).get("status") or "").strip().lower()
        if status:
            if any(word in status for word in ("progress", "active", "working")):
                score += 30
            elif any(word in status for word in ("review", "qa", "test")):
                score += 20
            elif any(word in status for word in ("todo", "to do", "open", "backlog")):
                score += 10
            reasons.append(f"status={status}")
        priority = ((task.get("priority") or {}).get("priority") or "").strip().lower()
        if priority == "urgent":
            score += 45
        elif priority == "high":
            score += 35
        elif priority == "normal":
            score += 15
        elif priority == "low":
            score += 5
        if priority:
            reasons.append(f"priority={priority}")
        task_text = " ".join(
            part
            for part in (
                str(task.get("name") or ""),
                str(task.get("description") or ""),
                self.task_location_label(task),
            )
            if part
        )
        task_tokens = self._tokenize(task_text)
        overlap = sorted(evidence_tokens & task_tokens)
        if overlap:
            score += min(len(overlap) * 18, 72)
            reasons.append("keyword overlap: " + ", ".join(overlap[:5]))
        if not reasons:
            reasons.append("open workspace task")
        return score, "; ".join(reasons)

    def _build_evidence_tokens(
        self,
        session: SessionState,
        messages: list[MessageRecord],
    ) -> set[str]:
        parts: list[str] = []
        for value in (session.latest_plan, session.latest_status, session.latest_blocker):
            if value:
                parts.append(value)
        inbound_messages = [message.content for message in messages if message.direction == "inbound" and message.content.strip()]
        parts.extend(inbound_messages[-6:])
        return self._tokenize(" ".join(parts))

    def _tokenize(self, text: str) -> set[str]:
        tokens = set()
        for token in re.findall(r"[a-z0-9]{3,}", text.lower()):
            if token not in _STOPWORDS:
                tokens.add(token)
        return tokens

    def _member_matches(self, user: UserProfile, member: dict[str, Any]) -> bool:
        member_email = str(member.get("email") or "").strip().lower()
        if user.clickup_user_email and member_email == user.clickup_user_email.lower():
            return True
        member_name = self._normalize_identifier(str(member.get("username") or ""))
        candidates = {
            self._normalize_identifier(user.display_name),
            self._normalize_identifier(user.storage_folder_name),
            self._normalize_identifier(user.discord_username),
            self._normalize_identifier(user.user_key),
        }
        candidates.discard("")
        return member_name in candidates

    def _normalize_identifier(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    def _match_task_hint(self, tasks: list[dict[str, Any]], task_hint: str) -> dict[str, Any] | None:
        normalized_hint = self._normalize_identifier(task_hint)
        lowered_hint = task_hint.strip().lower()
        for task in tasks:
            task_id = str(task.get("id") or "")
            if task_id and task_id.lower() == lowered_hint:
                return task
        scored: list[tuple[float, dict[str, Any]]] = []
        hint_tokens = self._tokenize(task_hint)
        for task in tasks:
            name = str(task.get("name") or "")
            normalized_name = self._normalize_identifier(name)
            score = 0.0
            if normalized_name == normalized_hint and normalized_hint:
                score += 100.0
            elif normalized_hint and normalized_hint in normalized_name:
                score += 85.0
            name_tokens = self._tokenize(name)
            overlap = len(hint_tokens & name_tokens)
            if overlap:
                score += overlap * 12.0
            ratio = difflib.SequenceMatcher(None, lowered_hint, name.lower()).ratio()
            score += ratio * 40.0
            if score > 0:
                scored.append((score, task))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_task = scored[0]
        if best_score < 35:
            return None
        return best_task

    def _format_ranked_context(
        self,
        scored: list[tuple[int, str, dict[str, Any]]],
        active_task_id: str | None,
        selection_reason: str | None,
    ) -> str:
        if active_task_id:
            active_task = next(
                (task for _, _, task in scored if str(task.get("id")) == active_task_id),
                None,
            )
            if active_task is not None:
                other_tasks = [task for _, _, task in scored if str(task.get("id")) != active_task_id][:4]
                return self._format_active_task_context(active_task, selection_reason, other_tasks)

        lines = ["Assigned ClickUp tasks:"]
        for _, _, task in scored[:5]:
            lines.append(self._task_summary_line(task))
        return "\n".join(lines)

    def _format_active_task_context(
        self,
        task: dict[str, Any],
        selection_reason: str | None,
        other_tasks: list[dict[str, Any]],
    ) -> str:
        lines = [
            "Active ClickUp task candidate:",
            self._task_summary_line(task),
        ]
        if selection_reason:
            lines.append(f"Why this task: {selection_reason}")
        description = str(task.get("description") or "").strip()
        if description:
            lines.extend(["Description:", description[:1200]])
        if other_tasks:
            lines.append("")
            lines.append("Other assigned tasks:")
            for other in other_tasks:
                lines.append(self._task_summary_line(other))
        return "\n".join(lines)

    def _task_summary_line(self, task: dict[str, Any]) -> str:
        status = (task.get("status") or {}).get("status")
        priority = (task.get("priority") or {}).get("priority")
        task_id = task.get("id")
        return (
            f"- {task.get('name')} | id={task_id} | status={status or 'unknown'} | "
            f"priority={priority or 'none'}"
        )

    def _priority_to_api_value(self, priority: str | None) -> int | None:
        if not priority:
            return None
        return _PRIORITY_TO_INT.get(priority.strip().lower())

    def _parent_task_id(self, task: dict[str, Any]) -> str | None:
        raw_parent = task.get("parent")
        if isinstance(raw_parent, dict):
            parent_id = str(raw_parent.get("id") or "").strip()
            return parent_id or None
        parent_id = str(raw_parent or "").strip()
        return parent_id or None

    @staticmethod
    def _task_created_sort_key(task: dict[str, Any]) -> int:
        raw_value = task.get("date_created")
        if raw_value in (None, ""):
            return 2**63 - 1
        if isinstance(raw_value, (int, float)):
            return int(raw_value)
        text = str(raw_value).strip()
        if text.isdigit():
            return int(text)
        try:
            return int(datetime.fromisoformat(text).timestamp() * 1000)
        except ValueError:
            return 2**63 - 1

    def _resolve_status_name(self, statuses: list[dict[str, Any]], state: str) -> str | None:
        keywords = _STATE_KEYWORDS.get(state, ())
        for status in statuses:
            name = str(status.get("status") or "")
            if any(keyword == name.strip().lower() for keyword in keywords):
                return name
        for status in statuses:
            name = str(status.get("status") or "")
            lowered = name.strip().lower()
            if any(keyword in lowered for keyword in keywords):
                return name
        return None

    def _status_matches_state(self, status_name: str, state: str) -> bool:
        lowered = status_name.strip().lower()
        return any(keyword in lowered for keyword in _STATE_KEYWORDS.get(state, ()))

    def _task_is_closed(self, task: dict[str, Any]) -> bool:
        status = task.get("status") or {}
        status_type = str(status.get("type") or "").strip().lower()
        status_name = str(status.get("status") or "").strip().lower()
        if status_type == "closed":
            return True
        return any(word in status_name for word in ("done", "complete", "closed"))

    def _upload_task_attachment_sync(self, task_id: str, file_path: Path) -> dict:
        with file_path.open("rb") as handle:
            response = requests.post(
                f"{self.base_url}/task/{task_id}/attachment",
                headers={"Authorization": self.api_token},
                files={"attachment": (file_path.name, handle)},
                timeout=60,
            )
        response.raise_for_status()
        return response.json()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_method = method.upper()
        max_attempts = _MAX_GET_ATTEMPTS if normalized_method == "GET" else 1
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.request(
                    normalized_method,
                    f"{self.base_url}{path}",
                    headers={
                        "Authorization": self.api_token,
                        "Content-Type": "application/json",
                    },
                    params=params,
                    json=json,
                    timeout=60,
                )
            except requests.RequestException as exc:
                if attempt >= max_attempts:
                    raise
                delay = 0.5 * (2 ** (attempt - 1))
                logger.warning(
                    "ClickUp %s %s failed with %s; retrying in %.1fs (%s/%s).",
                    normalized_method,
                    path,
                    type(exc).__name__,
                    delay,
                    attempt,
                    max_attempts,
                )
                time.sleep(delay)
                continue
            retryable = response.status_code in _RETRYABLE_STATUS_CODES
            if retryable and attempt < max_attempts:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 0.5 * (2 ** (attempt - 1))
                except ValueError:
                    delay = 0.5 * (2 ** (attempt - 1))
                logger.warning(
                    "ClickUp %s %s returned HTTP %s; retrying in %.1fs (%s/%s).",
                    normalized_method,
                    path,
                    response.status_code,
                    delay,
                    attempt,
                    max_attempts,
                )
                time.sleep(min(max(delay, 0.0), 10.0))
                continue
            response.raise_for_status()
            if response.content:
                return response.json()
            return {}
        raise RuntimeError(f"ClickUp request exhausted retries: {normalized_method} {path}")

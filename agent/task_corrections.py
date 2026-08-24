from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any


async def apply_operator_task_correction(
    runtime: Any,
    *,
    user_key: str,
    session_date: str,
    task_id: str,
    corrected_by: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    user = runtime.resolve_user_profile(user_key)
    if user is None:
        raise ValueError("The selected worker is not active in the roster.")
    task_id = task_id.strip()
    corrected_by = corrected_by.strip()
    reason = reason.strip()
    if not task_id or not corrected_by or not reason:
        raise ValueError("task_id, corrected_by, and reason are required.")
    if not runtime.clickup:
        raise RuntimeError("ClickUp is not configured.")
    assigned_tasks = await runtime.clickup.list_assigned_tasks(user, limit=100)
    selected = next(
        (
            task
            for task in assigned_tasks
            if str(task.get("id") or "") == task_id
        ),
        None,
    )
    if selected is None:
        raise ValueError("Choose a ClickUp task currently assigned to this worker.")
    task_name = str(selected.get("name") or task_id)
    reference = now or runtime.resolve_user_local_now(user)
    async with runtime._user_session_lock(user.user_key):
        session = runtime.state_store.get_session(user.user_key, session_date)
        if session.session_date != session_date:
            raise ValueError("The selected workday session no longer exists.")
        runtime._normalize_session_state(session, user=user)
        previous = deepcopy(session)
        prior_task_id = str(runtime._active_task_id(session) or "")
        prior_task_name = str(
            session.metadata.get("active_clickup_task_name") or ""
        )
        tracking = session.metadata.get("clickup_time_tracking")
        prior_timer_task_id = (
            str(tracking.get("task_id") or "")
            if isinstance(tracking, dict)
            else ""
        )
        warning = ""
        if (
            prior_timer_task_id
            and prior_timer_task_id != task_id
            and not session.clocked_out_at
        ):
            await runtime._pause_current_task_tracking(
                user,
                session,
                reference,
                set_hold=False,
                end_reason="operator_task_correction",
            )
            warning = (
                "The previous task timer was closed at the correction time. "
                "Earlier task-time allocation was preserved for audit and can be "
                "corrected separately in Time Tracking."
            )
        session.metadata["active_clickup_task_id"] = task_id
        session.metadata["active_clickup_task_name"] = task_name
        session.metadata["clickup_selection_reason"] = (
            f"Corrected by {corrected_by}: {reason}"
        )
        if session.clocked_in_at and not session.clocked_out_at and session.stage not in {
            "on_lunch_break",
            "awaiting_clock_out_artifacts",
            "clocked_out",
        }:
            await runtime._activate_clickup_task(
                user,
                session,
                reference,
                task_id,
                task_name,
            )
        history = session.metadata.get("operator_task_corrections")
        if not isinstance(history, list):
            history = []
        record = {
            "corrected_at": reference.isoformat(),
            "corrected_by": corrected_by,
            "reason": reason,
            "previous_task_id": prior_task_id,
            "previous_task_name": prior_task_name,
            "new_task_id": task_id,
            "new_task_name": task_name,
            "previous_timer_task_id": prior_timer_task_id,
            "warning": warning,
        }
        history.append(record)
        session.metadata["operator_task_corrections"] = history[-50:]
        await runtime._persist_session_state(
            user,
            session,
            now=reference,
            previous_session=previous,
            trigger="operator_task_correction",
            details=record,
        )
        runtime._resolve_matching_operational_issue(
            "slack_update_missing_task",
            user_key=user.user_key,
            session_date=session_date,
            now=reference,
        )
    return {
        "corrected": True,
        "user_key": user.user_key,
        "session_date": session_date,
        "task_id": task_id,
        "task_name": task_name,
        "warning": warning,
    }

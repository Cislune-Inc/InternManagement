from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from .operations import OperationalIssueReporter

logger = logging.getLogger(__name__)


async def collect_slack_update_feedback(runtime: Any, now: datetime) -> int:
    slack = getattr(runtime, "slack", None)
    if not runtime.config or not slack or not runtime.config.slack.enabled:
        return 0
    state = runtime._load_slack_update_state()
    last_checked = runtime._coerce_datetime_for_reference(
        str(state.get("feedback_checked_at") or ""),
        reference=now,
    )
    interval = timedelta(
        minutes=runtime.config.slack.feedback_poll_interval_minutes
    )
    if last_checked and now - last_checked < interval:
        return 0
    daily = state.get("daily_updates")
    if not isinstance(daily, dict):
        state["feedback_checked_at"] = now.isoformat()
        runtime._write_slack_update_state(state)
        return 0
    cutoff = now - timedelta(days=14)
    feedback_map = runtime.config.slack.feedback_reactions
    reviewed = 0
    for user_key, user_updates in daily.items():
        if not isinstance(user_updates, dict):
            continue
        for session_date, update in user_updates.items():
            if not isinstance(update, dict):
                continue
            posted_at = runtime._coerce_datetime_for_reference(
                str(update.get("posted_at") or ""),
                reference=now,
            )
            if not posted_at or posted_at < cutoff:
                continue
            channel_id = str(update.get("channel_id") or "")
            message_ts = str(update.get("message_ts") or "")
            if not channel_id or not message_ts:
                continue
            try:
                reactions = await slack.get_reactions(channel_id, message_ts)
            except Exception:
                logger.exception(
                    "Failed to read Slack feedback reactions for %s/%s.",
                    channel_id,
                    message_ts,
                )
                continue
            observed: dict[str, int] = {}
            for reaction in reactions:
                reaction_name = str(reaction.get("name") or "").strip(":")
                action = feedback_map.get(reaction_name)
                count = int(reaction.get("count") or 0)
                if action and count > 0:
                    observed[action] = observed.get(action, 0) + count
            update["operator_feedback"] = observed
            update["feedback_checked_at"] = now.isoformat()
            reviewed += 1
            await _sync_feedback_issues(
                runtime,
                user_key=str(user_key),
                session_date=str(session_date),
                update=update,
                observed=observed,
                feedback_map=feedback_map,
                now=now,
            )
    state["feedback_checked_at"] = now.isoformat()
    runtime._write_slack_update_state(state)
    return reviewed


async def _sync_feedback_issues(
    runtime: Any,
    *,
    user_key: str,
    session_date: str,
    update: dict[str, Any],
    observed: dict[str, int],
    feedback_map: dict[str, str],
    now: datetime,
) -> None:
    channel_id = str(update.get("channel_id") or "")
    message_ts = str(update.get("message_ts") or "")
    for action in {value for value in feedback_map.values() if value != "useful"}:
        fingerprint_parts = (channel_id, message_ts, action)
        if observed.get(action):
            user = runtime.resolve_user_profile(user_key)
            await runtime._report_operational_issue(
                category="slack_update_feedback",
                severity="warning",
                summary=(
                    f"Slack update was flagged as {action.replace('_', ' ')}"
                    + (f" for {user.display_name}." if user else ".")
                ),
                details={
                    "feedback_action": action,
                    "reaction_count": observed[action],
                    "user_key": user_key,
                    "display_name": user.display_name if user else "",
                    "session_date": session_date,
                    "channel_id": channel_id,
                    "message_ts": message_ts,
                    "active_task_id": str(update.get("active_task_id") or ""),
                    "active_task_name": str(
                        update.get("active_task_name") or ""
                    ),
                },
                fingerprint_parts=fingerprint_parts,
                now=now,
            )
        else:
            operations = getattr(runtime, "operations", None)
            if isinstance(operations, OperationalIssueReporter):
                operations.resolve("slack_update_feedback", *fingerprint_parts)

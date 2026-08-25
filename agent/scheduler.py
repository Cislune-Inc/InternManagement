from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .time_utils import resolve_timezone


logger = logging.getLogger(__name__)


async def run_scheduler_tick(runtime: Any, client: Any) -> None:
    await runtime.refresh_configuration()
    if not runtime.config:
        return
    base_now = datetime.now(tz=resolve_timezone(runtime.runtime_timezone_name()))
    for user in runtime.roster_by_key.values():
        try:
            await runtime._run_scheduler_for_user(client, user, base_now)
            resolve_matching = getattr(
                getattr(runtime, "state_store", None),
                "resolve_matching_operational_issues",
                None,
            )
            if callable(resolve_matching):
                resolve_matching(
                    category="scheduler_user_failure",
                    details_match={"user_key": user.user_key},
                    resolved_at=base_now,
                )
        except Exception as exc:
            logger.exception(
                "Scheduler tick failed for user %s (%s)",
                user.user_key,
                user.display_name,
            )
            await runtime._report_operational_issue(
                category="scheduler_user_failure",
                severity="error",
                summary=f"Scheduler processing failed for {user.display_name}.",
                details={
                    "user_key": user.user_key,
                    "display_name": user.display_name,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                },
                fingerprint_parts=(user.user_key, type(exc).__name__),
                now=base_now,
            )
    maintenance = [
        (
            "operational_issue_digest",
            lambda: runtime._maybe_send_operational_issue_digest(base_now),
        ),
        (
            "weekly_photo_recap",
            lambda: runtime._maybe_post_slack_weekly_photo_recap(base_now),
        ),
        (
            "slack_feedback",
            lambda: runtime._maybe_collect_slack_update_feedback(base_now),
        ),
        ("dashboard", runtime.write_dashboard),
    ]
    for step_name, action in maintenance:
        try:
            await action()
        except Exception as exc:
            logger.exception("Scheduler maintenance step failed: %s.", step_name)
            await runtime._report_operational_issue(
                category="scheduler_maintenance_failure",
                severity="error",
                summary=f"Scheduler maintenance failed during {step_name}.",
                details={
                    "step": step_name,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                },
                fingerprint_parts=(step_name, type(exc).__name__),
                now=base_now,
            )

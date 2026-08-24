from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import quote, urlsplit, urlunsplit

from .models import AdminProfile, AgentConfig
from .slack_client import SlackClient
from .state_store import StateStore
from .time_utils import resolve_timezone

logger = logging.getLogger(__name__)


_UNCHANGED_DIGEST_REMINDER = timedelta(days=7)
_DIGEST_PREVIEW_LIMIT = 5


class OperationalIssueReporter:
    def __init__(
        self,
        *,
        state_store: StateStore,
        config_provider: Callable[[], AgentConfig | None],
        slack_provider: Callable[[], SlackClient | None],
        admins_provider: Callable[[], list[AdminProfile]],
        timezone_provider: Callable[[], str],
    ) -> None:
        self.state_store = state_store
        self.config_provider = config_provider
        self.slack_provider = slack_provider
        self.admins_provider = admins_provider
        self.timezone_provider = timezone_provider

    async def report(
        self,
        *,
        category: str,
        severity: str,
        summary: str,
        details: dict[str, Any] | None = None,
        fingerprint_parts: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> dict[str, Any]:
        observed_at = now or datetime.now(
            tz=resolve_timezone(self.timezone_provider())
        )
        fingerprint = issue_fingerprint(category, *fingerprint_parts)
        issue = self.state_store.record_operational_issue(
            fingerprint=fingerprint,
            category=category,
            severity=severity,
            summary=summary,
            details=details,
            observed_at=observed_at,
        )
        config = self.config_provider()
        slack = self.slack_provider()
        if (
            not config
            or not config.slack.operational_alerts_enabled
            or not slack
        ):
            return issue
        if severity.lower() not in {"error", "critical"}:
            return issue
        if not self.state_store.operational_issue_needs_notification(
            fingerprint,
            cooldown=timedelta(
                minutes=config.slack.operational_alert_cooldown_minutes
            ),
            now=observed_at,
        ):
            return issue
        targets = [admin for admin in self.admins_provider() if admin.slack_user_id]
        if not targets:
            return issue
        message = _issue_message(
            severity=severity,
            category=category,
            summary=summary,
            occurrence_count=int(issue.get("occurrence_count") or 1),
            details=details or {},
            manager_queue_url=config.slack.manager_queue_url,
        )
        notified = False
        for admin in targets:
            try:
                await slack.post_message(str(admin.slack_user_id), message)
                notified = True
            except Exception:
                logger.exception(
                    "Failed to send operational alert to Slack admin %s.",
                    admin.name,
                )
        if notified:
            self.state_store.mark_operational_issue_notified(
                fingerprint,
                notified_at=observed_at,
            )
        return issue

    async def maybe_send_digest(self, now: datetime | None = None) -> bool:
        observed_at = now or datetime.now(
            tz=resolve_timezone(self.timezone_provider())
        )
        self._maintain_route_issues(observed_at)
        config = self.config_provider()
        slack = self.slack_provider()
        if (
            not config
            or not config.slack.operational_alerts_enabled
            or not slack
        ):
            return False
        digest_timezone = resolve_timezone(
            config.slack.operational_digest_timezone
        )
        digest_now = observed_at.astimezone(digest_timezone)
        if digest_now.hour < config.slack.operational_digest_hour:
            return False
        digest_state = self.state_store.get_operational_state(
            "operational_issue_digest"
        ) or {}
        last_checked_local_date = str(
            digest_state.get("last_checked_local_date") or ""
        )
        if last_checked_local_date == digest_now.date().isoformat():
            return False
        last_sent_local_date = str(
            digest_state.get("last_sent_local_date") or ""
        )
        if last_sent_local_date == digest_now.date().isoformat():
            return False
        last_sent_raw = str(digest_state.get("last_sent_at") or "")
        try:
            last_sent_at = datetime.fromisoformat(last_sent_raw) if last_sent_raw else None
        except ValueError:
            last_sent_at = None
        if last_sent_at is not None:
            if last_sent_at.tzinfo is None and observed_at.tzinfo is not None:
                last_sent_at = last_sent_at.replace(tzinfo=observed_at.tzinfo)
            if last_sent_at.astimezone(digest_timezone).date() == digest_now.date():
                return False
        issues = self.state_store.list_operational_issues(status="open", limit=1000)
        if not issues:
            return False
        targets = [admin for admin in self.admins_provider() if admin.slack_user_id]
        if not targets:
            return False
        signature = _digest_signature(issues)
        last_signature = str(digest_state.get("last_signature") or "")
        last_signature_sent_at = _parse_digest_datetime(
            str(
                digest_state.get("last_signature_sent_at")
                or digest_state.get("last_sent_at")
                or ""
            ),
            reference=observed_at,
        )
        weekly_reminder = bool(
            signature == last_signature
            and last_signature_sent_at is not None
            and observed_at - last_signature_sent_at >= _UNCHANGED_DIGEST_REMINDER
        )
        if signature == last_signature and not weekly_reminder:
            self.state_store.set_operational_state(
                "operational_issue_digest",
                {
                    **digest_state,
                    "last_checked_at": observed_at.isoformat(),
                    "last_checked_local_date": digest_now.date().isoformat(),
                    "timezone": config.slack.operational_digest_timezone,
                    "open_issue_count": len(issues),
                },
            )
            return False
        message = _digest_message(
            issues,
            manager_queue_url=config.slack.manager_queue_url,
            weekly_reminder=weekly_reminder,
        )
        notified = False
        for admin in targets:
            try:
                await slack.post_message(str(admin.slack_user_id), message)
                notified = True
            except Exception:
                logger.exception(
                    "Failed to send operational digest to Slack admin %s.",
                    admin.name,
                )
        if not notified:
            return False
        self.state_store.set_operational_state(
            "operational_issue_digest",
            {
                "last_sent_at": observed_at.isoformat(),
                "last_sent_local_date": digest_now.date().isoformat(),
                "last_checked_at": observed_at.isoformat(),
                "last_checked_local_date": digest_now.date().isoformat(),
                "last_signature": signature,
                "last_signature_sent_at": observed_at.isoformat(),
                "timezone": config.slack.operational_digest_timezone,
                "open_issue_count": len(issues),
            },
        )
        return True

    def _maintain_route_issues(self, observed_at: datetime) -> dict[str, int]:
        stale_before = observed_at - timedelta(days=14)
        open_issues = self.state_store.list_operational_issues(
            status="open",
            limit=1000,
        )
        route_issues = [
            issue
            for issue in open_issues
            if str(issue.get("category") or "") == "slack_route_uncertain"
        ]
        stale_resolved = 0
        active: list[dict[str, Any]] = []
        for issue in route_issues:
            details = issue.get("details")
            details = details if isinstance(details, dict) else {}
            if (
                details.get("user_key")
                and ("active_task_id" in details or "active_task_name" in details)
                and not str(
                    details.get("active_task_id")
                    or details.get("active_task_name")
                    or ""
                ).strip()
            ):
                if self.state_store.resolve_operational_issue(
                    str(issue.get("fingerprint") or ""),
                    resolved_at=observed_at,
                ):
                    stale_resolved += 1
                continue
            try:
                last_seen_at = datetime.fromisoformat(
                    str(issue.get("last_seen_at") or "")
                )
            except ValueError:
                active.append(issue)
                continue
            if last_seen_at.tzinfo is None and stale_before.tzinfo is not None:
                last_seen_at = last_seen_at.replace(tzinfo=stale_before.tzinfo)
            if last_seen_at < stale_before:
                if self.state_store.resolve_operational_issue(
                    str(issue.get("fingerprint") or ""),
                    resolved_at=observed_at,
                ):
                    stale_resolved += 1
                continue
            active.append(issue)

        for issue in open_issues:
            if str(issue.get("category") or "") != "slack_update_missing_task":
                continue
            details = issue.get("details")
            details = details if isinstance(details, dict) else {}
            if details.get("escalated_after_minutes"):
                continue
            if self.state_store.resolve_operational_issue(
                str(issue.get("fingerprint") or ""),
                resolved_at=observed_at,
            ):
                stale_resolved += 1

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for issue in active:
            details = issue.get("details")
            details = details if isinstance(details, dict) else {}
            user_key = str(details.get("user_key") or "").strip()
            task_key = str(
                details.get("active_task_id")
                or details.get("active_task_name")
                or "unassigned"
            ).strip()
            grouped.setdefault((user_key, task_key), []).append(issue)

        merged = 0
        for (user_key, task_key), issues in grouped.items():
            target = issue_fingerprint(
                "slack_route_uncertain",
                user_key,
                task_key,
            )
            fingerprints = [
                str(issue.get("fingerprint") or "")
                for issue in issues
            ]
            if fingerprints == [target]:
                continue
            if self.state_store.merge_operational_issues(
                target_fingerprint=target,
                source_fingerprints=fingerprints,
                resolved_at=observed_at,
            ):
                merged += max(0, len(fingerprints) - 1)
        return {"stale_resolved": stale_resolved, "merged": merged}

    def resolve(self, category: str, *fingerprint_parts: str) -> bool:
        return self.state_store.resolve_operational_issue(
            issue_fingerprint(category, *fingerprint_parts)
        )


def issue_fingerprint(category: str, *parts: str) -> str:
    source = "|".join((category, *parts))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _digest_signature(issues: list[dict[str, Any]]) -> str:
    """Identify manager decisions, ignoring noisy recurrence counters and timestamps."""
    rows = sorted(
        "|".join(
            (
                str(issue.get("fingerprint") or ""),
                str(issue.get("category") or ""),
                str(issue.get("severity") or ""),
            )
        )
        for issue in issues
    )
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _parse_digest_datetime(value: str, *, reference: datetime) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    if parsed is not None and parsed.tzinfo is None and reference.tzinfo is not None:
        parsed = parsed.replace(tzinfo=reference.tzinfo)
    return parsed


def _digest_message(
    issues: list[dict[str, Any]],
    *,
    manager_queue_url: str,
    weekly_reminder: bool,
) -> str:
    severity_order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    ordered = sorted(
        issues,
        key=lambda issue: (
            severity_order.get(str(issue.get("severity") or "").lower(), 9),
            str((issue.get("details") or {}).get("display_name") or "").lower(),
            str(issue.get("category") or ""),
        ),
    )
    title = (
        f"*Don Pollo — {len(ordered)} unresolved manager action"
        f"{'s' if len(ordered) != 1 else ''}*"
    )
    if weekly_reminder:
        title += " _(weekly reminder)_"
    lines = [title, ""]
    for issue in ordered[:_DIGEST_PREVIEW_LIMIT]:
        lines.append(_digest_issue_line(issue, manager_queue_url=manager_queue_url))
    hidden_count = len(ordered) - _DIGEST_PREVIEW_LIMIT
    if hidden_count > 0:
        lines.append(f"• *+{hidden_count} more* in the manager queue")
    lines.extend(
        (
            "",
            "*Process them*",
            "1. Open the item and assign the hours to an existing contract task or an overhead category.",
            "2. If the result is unclear, ask the worker for one concrete outcome before assigning it.",
            "3. Save the correction; dismiss only a true duplicate or obsolete system error.",
            "",
            f"<{manager_queue_url}|Open manager queue>",
            "_Only new or changed items are sent. Unchanged items return as one weekly reminder._",
        )
    )
    return "\n".join(lines)


def _digest_issue_line(issue: dict[str, Any], *, manager_queue_url: str) -> str:
    category = str(issue.get("category") or "")
    details = issue.get("details")
    details = details if isinstance(details, dict) else {}
    display_name = str(
        details.get("display_name")
        or details.get("user_key")
        or "Don Pollo"
    ).strip()
    user_key = str(details.get("user_key") or "").strip()
    session_date = str(details.get("session_date") or "").strip()
    date_label = f" ({session_date})" if session_date else ""
    action_url = _worker_action_url(manager_queue_url, user_key) if user_key else manager_queue_url
    if category == "slack_update_missing_task":
        return (
            f"• *{display_name}*{date_label} — work was recorded without a confirmed task. "
            f"<{action_url}|Assign or classify>"
        )
    if category == "slack_route_uncertain":
        task_name = str(details.get("active_task_name") or "this work").strip()
        return (
            f"• *{display_name}* — choose the project channel for {task_name}. "
            f"<{action_url}|Review routing>"
        )
    summary = str(issue.get("summary") or "Review this Don Pollo issue.").strip()
    return f"• *{display_name}* — {summary} <{action_url}|Review>"


def _worker_action_url(manager_queue_url: str, user_key: str) -> str:
    parsed = urlsplit(manager_queue_url)
    if parsed.scheme and parsed.netloc:
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                "/work",
                f"worker={quote(user_key)}",
                "",
            )
        )
    return f"/work?worker={quote(user_key)}"


def _issue_message(
    *,
    severity: str,
    category: str,
    summary: str,
    occurrence_count: int,
    details: dict[str, Any],
    manager_queue_url: str,
) -> str:
    lines = [
        f"*Don Pollo {severity.upper()}*",
        summary,
        f"Category: `{category}`",
        f"Occurrences: {occurrence_count}",
    ]
    useful_details = [
        f"{key}: {value}"
        for key, value in details.items()
        if value not in (None, "")
    ][:8]
    if useful_details:
        lines.extend(("", *useful_details))
    lines.append(
        "\nReview the manager queue at "
        f"{manager_queue_url} for details and resolution."
    )
    return "\n".join(lines)

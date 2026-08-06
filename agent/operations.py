from __future__ import annotations

import hashlib
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable

from .models import AdminProfile, AgentConfig
from .slack_client import SlackClient
from .state_store import StateStore
from .time_utils import resolve_timezone

logger = logging.getLogger(__name__)


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
        self._maintain_route_issues(observed_at)
        digest_state = self.state_store.get_operational_state(
            "operational_issue_digest"
        ) or {}
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
        category_counts = Counter(str(issue.get("category") or "unknown") for issue in issues)
        occurrence_counts: Counter[str] = Counter()
        severity_counts = Counter(str(issue.get("severity") or "unknown").lower() for issue in issues)
        for issue in issues:
            occurrence_counts[str(issue.get("category") or "unknown")] += int(
                issue.get("occurrence_count") or 1
            )
        lines = [
            "*Don Pollo operational digest*",
            f"Open issues: {len(issues)}",
            "Severity: " + ", ".join(
                f"{severity} {count}"
                for severity, count in sorted(severity_counts.items())
            ),
            "",
            "Top categories:",
        ]
        for category, count in category_counts.most_common(8):
            lines.append(
                f"• `{category}`: {count} open, {occurrence_counts[category]} occurrence(s)"
            )
        lines.extend(("", f"Review and resolve: {config.slack.manager_queue_url}"))
        message = "\n".join(lines)
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

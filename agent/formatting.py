from __future__ import annotations

from datetime import datetime

from .models import MessageRecord, SessionState, UserProfile
from .time_utils import format_admin_datetime, format_transcript_datetime


def build_transcript_markdown(
    user: UserProfile,
    session: SessionState,
    messages: list[MessageRecord],
) -> str:
    lines = [
        f"# {user.display_name} - {session.session_date}",
        "",
        f"- User key: `{user.user_key}`",
        f"- Discord user ID: `{user.discord_user_id}`",
        f"- Stage: `{session.stage}`",
        "",
        "## Transcript",
        "",
    ]
    for message in messages:
        timestamp = format_transcript_datetime(message.created_at)
        author = "Agent" if message.direction == "outbound" else user.display_name
        lines.append(f"### {timestamp} - {author}")
        lines.append("")
        lines.append(message.content or "_No text content_")
        lines.append("")
        if message.attachments:
            lines.append("Attachments:")
            for attachment in message.attachments:
                path_ref = f" (`{attachment.local_path}`)" if attachment.local_path else ""
                original_ref = f" [from {attachment.original_filename}]" if attachment.original_filename else ""
                lines.append(f"- {attachment.filename}{original_ref}{path_ref}")
                if attachment.description:
                    lines.append(f"  - Description: {attachment.description}")
                if attachment.tags:
                    lines.append(f"  - Tags: {', '.join(attachment.tags)}")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def build_clickup_update(
    user: UserProfile,
    session: SessionState,
    summary: str,
    archive_path: str | None,
) -> str:
    onboarding_summary = str(session.metadata.get("last_task_onboarding_summary") or "").strip()
    lines = [
        f"Daily update for {user.display_name} ({session.session_date})",
        "",
        f"Stage: {session.stage}",
    ]
    if active_task_name := session.metadata.get("active_clickup_task_name"):
        lines.append(f"Active ClickUp task: {active_task_name}")
    if session.clocked_in_at:
        lines.append(f"Clocked in: {format_admin_datetime(session.clocked_in_at, include_relative=False)}")
    if session.clocked_out_at:
        lines.append(f"Clocked out: {format_admin_datetime(session.clocked_out_at, include_relative=False)}")
    plan_text = onboarding_summary or session.latest_plan
    if plan_text:
        lines.extend(["", "Task onboarding plan" if onboarding_summary else "Plan", plan_text])
    if session.latest_blocker:
        lines.extend(["", "Blockers", session.latest_blocker])
    if session.latest_status:
        lines.extend(["", "Latest status", session.latest_status])
    if session.latest_feedback:
        lines.extend(["", "Agent feedback", session.latest_feedback])
    if summary:
        lines.extend(["", "Summary", summary])
    if archive_path:
        lines.extend(["", f"Local archive: {archive_path}"])
    return "\n".join(lines)


def iso_now(dt: datetime) -> str:
    return dt.isoformat()

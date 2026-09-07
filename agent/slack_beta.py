"""Hours-first Slack transport adapter. No Discord or ClickUp clock dependency."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .slack_timekeeping import HELP, FALLBACK, SlackTimekeeping, clock_command, timestamp

logger = logging.getLogger(__name__)


def enabled(runtime: Any, slack_id: str | None) -> bool:
    return bool(runtime.config and slack_id and slack_id in runtime.config.slack.work_intake_beta_slack_user_ids)


def clock_user(runtime: Any, slack_id: str) -> Any:
    user = runtime.roster_by_slack_id.get(slack_id)
    if user:
        return user if user.active else None
    admin = runtime.admin_profile_by_slack_user_id(slack_id)
    if admin:
        from .worker_portal import WorkerPortalService

        return WorkerPortalService(runtime)._live_user(admin)
    return None


async def manager_hours(runtime: Any, admin: Any, slack_id: str, text: str, event_id: str, now: datetime) -> None:
    if not admin:
        await runtime.slack.post_message(slack_id, "Only configured managers can reconcile another worker's hours. Use report hours for your own actual-time report.")
        return
    from .slack_work_intake import _safe

    service = ledger(runtime)
    try:
        if text.lower().startswith("hours reports"):
            reports = service.reports()
            response = f"{len(reports)} pending actual-hours reports; showing up to five.\n" + "\n".join(f"`{_safe(row['id'])}` · {_safe(row['user_key'])}: {_safe(row['text'][:700])}" for row in reports[:5])
        elif text.lower().startswith("hours resolve "):
            parts = text.split(maxsplit=3)
            if len(parts) != 4:
                raise ValueError("Use hours resolve REPORT_ID RECONCILIATION_NOTE after correcting the actual hours.")
            response = service.resolve_report(parts[2], actor_id=slack_id, note=parts[3], now=now)
        else:
            parts = text.split(maxsplit=5)
            if len(parts) != 6:
                raise ValueError("Use hours add USER_KEY START_ISO END_ISO REASON. Include actual dates and UTC offsets.")
            target = runtime.roster_by_key.get(parts[2])
            if target is None:
                raise ValueError("Choose a known roster user_key; use the hours editor for archived workers.")
            start, end = timestamp(parts[3]), timestamp(parts[4])
            async with runtime._user_session_lock(target.user_key):
                response = service.add_actual_hours(target, start, end, actor_id=slack_id, event_id=event_id, reason=parts[5], now=now)
                corrected = runtime.state_store.get_session(target.user_key, start.astimezone(service.zone).date().isoformat())
                await archive(runtime, target, corrected, now)
    except (ValueError, TypeError) as exc:
        response = str(exc)
    await runtime.slack.post_message(slack_id, response)


def ledger(runtime: Any) -> SlackTimekeeping:
    return SlackTimekeeping(runtime.state_store, timezone_name=runtime.config.timezone,
                           daily_limit_hours=runtime.config.labor.overtime_limit_hours)


async def archive(runtime: Any, user: Any, session: Any, now: datetime) -> None:
    if session is None:
        return
    # SQLite already committed the clock + idempotency receipt atomically. File
    # projection failure must not tell the worker their recorded clock failed.
    runtime._refresh_session_time_summary(session, now)
    runtime.state_store.save_session(session)
    try:
        await runtime._archive_session(user, session)
    except Exception as exc:
        logger.error("Slack clock archive failed for %s: %s", user.user_key, type(exc).__name__)
        await runtime._report_operational_issue(
            category="slack_clock_archive", severity="error",
            summary="Recorded Slack hours need archive/export repair before payroll.",
            details={"user_key": user.user_key, "session_date": session.session_date},
            fingerprint_parts=(user.user_key, session.session_date), now=now,
        )


async def handle_message(runtime: Any, event: dict[str, Any]) -> bool:
    slack_id = str(event.get("user") or "")
    if not enabled(runtime, slack_id):
        if runtime.config and runtime.config.slack.work_intake_beta_slack_user_ids and runtime.slack:
            await runtime.slack.post_message(slack_id, "Your Slack identity is not enabled for the DP beta clock yet. Slack Erik your actual hours and ask to connect your roster entry. Do not use Gusto Kiosk.")
            return True
        return False
    user = clock_user(runtime, slack_id)
    if user is None or runtime.slack is None:
        return True
    text = str(event.get("text") or "").strip()
    event_id = str(event.get("event_ts") or event.get("ts") or "")
    try:
        now = datetime.fromtimestamp(float(event_id), timezone.utc)
    except (ValueError, OverflowError, OSError):
        await runtime.slack.post_message(slack_id, "I could not validate this message's timestamp. " + FALLBACK)
        return True
    admin = runtime.admin_profile_by_slack_user_id(slack_id)
    if text.lower().startswith(("hours add ", "hours resolve ", "hours reports")):
        await manager_hours(runtime, admin, slack_id, text, event_id, now)
        return True
    if text.lower().startswith("hours authorize "):
        if not admin or admin.discord_user_id != runtime.config.admin_discord_user_id:
            await runtime.slack.post_message(slack_id, "Only Erik, the configured primary admin, can authorize remote work or overtime.")
            return True
        parts = text.split(maxsplit=7)
        try:
            if len(parts) < 7:
                raise ValueError("Use: hours authorize USER_KEY remote|overtime START_ISO END_ISO REASON (times include UTC offsets).")
            target, kind, start, end = parts[2:6]
            if target not in runtime.roster_by_key or not runtime.roster_by_key[target].active:
                raise ValueError("Choose an active roster user_key; no approval was recorded.")
            reason = " ".join(parts[6:])
            ledger(runtime).authorize(target, kind, timestamp(start), timestamp(end), approver=slack_id, reason=reason, now=now)
            response = "Advance authorization recorded for the specified worker, type and time window. Actual hours must still be recorded."
        except (ValueError, TypeError) as exc:
            response = str(exc)
        await runtime.slack.post_message(slack_id, response)
        return True
    command = clock_command(text)
    if command:
        async with runtime._user_session_lock(user.user_key):
            try:
                response, session = ledger(runtime).handle(user, *command, event_id=event_id, now=now)
            except ValueError as exc:
                response, session = str(exc), None
            await runtime.slack.post_message(slack_id, response)
            await archive(runtime, user, session, now)
        if command[0] == "in" and response.startswith("Clocked in") and len(command[1].split(maxsplit=1)) == 2:
            await runtime._handle_slack_work_intake(slack_id, "work " + command[1].split(maxsplit=1)[1], event)
        return True
    if text.lower() in {"login", "portal", "website", "link"}:
        await runtime.slack.post_message(slack_id, HELP + "\nThe old task-gated portal is not the beta clock. Use Slack for hours.")
        return True
    # Preserve explicit legacy admin tools, but ordinary admin prose uses the
    # same work assistant as everyone else.
    if admin and text.lower().startswith(("run ", "admin ")):
        return False
    if not text:
        await runtime.slack.post_message(slack_id, "Your attachment arrived. Add a short caption so I know what it shows. " + HELP)
        return True
    from .slack_work_intake import SlackWorkIntake

    prefix = "work update " if SlackWorkIntake(runtime.state_store).has_item(slack_id) else "work "
    await runtime._handle_slack_work_intake(slack_id, text if re.match(r"^work(?:\s|$)", text, re.I) else prefix + text, event)
    return True


async def tick(runtime: Any, user: Any, now: datetime) -> None:
    service = ledger(runtime)
    _, session = service.tick(user, now)
    await archive(runtime, user, session, now)
    for notice in service.pending_notices(user.user_key):
        await runtime.slack.post_message(user.slack_user_id, notice["text"])
        service.notice_delivered(notice["id"], datetime.now(timezone.utc))


async def flush_work_notices(runtime: Any) -> None:
    from .slack_work_intake import SlackWorkIntake
    if not runtime.slack:
        return
    intake = SlackWorkIntake(runtime.state_store)
    for notice in intake.pending_notices():
        if not enabled(runtime, notice["owner_id"]) or clock_user(runtime, notice["owner_id"]) is None:
            continue
        try:
            await runtime.slack.post_message(notice["owner_id"], notice["text"])
        except Exception:
            logger.warning("Work decision notification pending retry; no decision was lost.")
            continue
        intake.notice_delivered(notice["id"])


async def run_slack_only(runtime: Any) -> None:
    import os
    from .slack_receiver import SlackSocketReceiver

    if not os.getenv("SLACK_BOT_TOKEN") or not os.getenv("SLACK_APP_TOKEN"):
        raise RuntimeError("Slack-only startup requires SLACK_BOT_TOKEN and SLACK_APP_TOKEN.")
    if not runtime.config.slack.enabled or not runtime.config.slack.work_intake_beta_slack_user_ids:
        raise RuntimeError("Enable Slack and configure the verified beta roster before starting the Slack-only clock.")
    receiver = SlackSocketReceiver(runtime, None, bot_token=os.environ["SLACK_BOT_TOKEN"], app_token=os.environ["SLACK_APP_TOKEN"])

    async def scheduler() -> None:
        while True:
            try:
                await runtime.scheduler_tick(None)
            except Exception:
                logger.exception("Slack-only scheduler failed; clock records remain durable.")
            await asyncio.sleep(30)

    tasks = [asyncio.create_task(receiver.start()), asyncio.create_task(scheduler())]
    try:
        # A dead socket receiver is a failed service, not a healthy scheduler.
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise RuntimeError("Slack-only service exited unexpectedly.")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await receiver.close()

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.models import SessionState, UserProfile
from agent.runtime import InternManagementRuntime
from agent.signals import detect_signals, is_clock_out_cancellation


_CLOCK_OUT_RETURN_STATE_KEY = "clock_out_return_state"
_CLOCK_OUT_PAUSE_NOTE_KEY = "clock_out_pause_note"
_TIME_RECORD_ORIGIN_KEY = "time_record_origin"
_LEGACY_EDITOR = "g"


def merge_overlapping_segments(
    segments: list[dict[str, Any]],
) -> tuple[list[dict[str, str | None]], bool]:
    parsed: list[tuple[datetime, datetime]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            return segments, False
        start_text = str(segment.get("clocked_in_at") or "").strip()
        end_text = str(segment.get("clocked_out_at") or "").strip()
        if not start_text or not end_text:
            return segments, False
        try:
            start_at = datetime.fromisoformat(start_text)
            end_at = datetime.fromisoformat(end_text)
        except ValueError:
            return segments, False
        if end_at <= start_at:
            return segments, False
        parsed.append((start_at, end_at))

    merged: list[tuple[datetime, datetime]] = []
    overlap_found = False
    for start_at, end_at in sorted(parsed, key=lambda item: item[0]):
        if not merged or start_at >= merged[-1][1]:
            merged.append((start_at, end_at))
            continue
        overlap_found = True
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end_at))
    if not overlap_found:
        return segments, False
    return [
        {
            "clocked_in_at": start_at.isoformat(),
            "clocked_out_at": end_at.isoformat(),
        }
        for start_at, end_at in merged
    ], True


def is_legacy_migration_candidate(session: SessionState, legacy_date: str) -> bool:
    if session.session_date != legacy_date:
        return False
    if str(session.metadata.get(_TIME_RECORD_ORIGIN_KEY) or ""):
        return False
    latest_edit = session.metadata.get("latest_manual_time_edit")
    if not isinstance(latest_edit, dict):
        return False
    editor = str(latest_edit.get("edited_by") or "").strip().lower()
    reason = str(latest_edit.get("reason") or "").strip().lower()
    task_seconds = int(session.time_summary.get("task_tracked_total_seconds") or 0)
    return editor == _LEGACY_EDITOR and reason == _LEGACY_EDITOR and task_seconds == 0


def _archived_sessions(
    runtime: InternManagementRuntime,
) -> Iterator[tuple[UserProfile, SessionState]]:
    people_dir = runtime.bootstrap.storage_root_path / "people"
    if not people_dir.exists():
        return
    seen: set[tuple[str, str]] = set()
    for profile_path in sorted(people_dir.glob("*/profile.json")):
        try:
            user = UserProfile(**json.loads(profile_path.read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        for session_path in sorted(profile_path.parent.glob("*/session.json")):
            session_date = session_path.parent.name
            key = (user.user_key, session_date)
            if key in seen:
                continue
            seen.add(key)
            try:
                session = runtime._load_session_for_manual_time_edit(user, session_date)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            yield user, session


def _clock_out_request_at(
    runtime: InternManagementRuntime,
    session: SessionState,
) -> datetime | None:
    candidate: datetime | None = None
    for message in runtime.state_store.list_messages(
        session.user_key,
        session.session_date,
    ):
        if message.direction != "inbound":
            continue
        if is_clock_out_cancellation(message.content):
            candidate = None
            continue
        if candidate is None and detect_signals(message.content).clocking_out:
            candidate = message.created_at
    return candidate


def _has_open_time(session: SessionState) -> bool:
    if any(
        isinstance(segment, dict) and not segment.get("clocked_out_at")
        for segment in session.work_segments
    ):
        return True
    tracking = session.metadata.get("clickup_time_tracking")
    return isinstance(tracking, dict) and not tracking.get("closed_at")


def _close_attendance_at(session: SessionState, clock_out_at: datetime) -> None:
    session.clocked_out_at = clock_out_at.isoformat()
    for segment in reversed(session.work_segments):
        if not isinstance(segment, dict) or segment.get("clocked_out_at"):
            continue
        start_text = str(segment.get("clocked_in_at") or "")
        try:
            start_at = datetime.fromisoformat(start_text)
        except ValueError:
            continue
        if start_at.tzinfo is None and clock_out_at.tzinfo is not None:
            start_at = start_at.replace(tzinfo=clock_out_at.tzinfo)
        if clock_out_at > start_at:
            segment["clocked_out_at"] = clock_out_at.isoformat()
        break
    return_state = session.metadata.get(_CLOCK_OUT_RETURN_STATE_KEY)
    if not isinstance(return_state, dict):
        return_state = {}
        session.metadata[_CLOCK_OUT_RETURN_STATE_KEY] = return_state
    return_state["clock_out_requested_at"] = clock_out_at.isoformat()


def _repair_entry(
    *,
    action: str,
    now: datetime,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "action": action,
        "at": now.isoformat(),
        "performed_by": "Codex time-integrity repair",
        **details,
    }


def _append_repair_history(session: SessionState, entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    history = session.metadata.get("time_integrity_repairs")
    history = list(history) if isinstance(history, list) else []
    history.extend(entries)
    session.metadata["time_integrity_repairs"] = history[-20:]
    session.metadata["latest_time_integrity_repair"] = entries[-1]


async def build_plan(
    runtime: InternManagementRuntime,
    *,
    legacy_date: str,
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for user, session in _archived_sessions(runtime):
        now = runtime.resolve_user_local_now(user)
        runtime._normalize_session_state(session, user=user)
        runtime._refresh_session_time_summary(
            session,
            runtime._session_time_summary_reference_now(session, user, now),
        )
        actions: list[dict[str, Any]] = []
        if session.stage == "awaiting_clock_out_artifacts" and _has_open_time(session):
            requested_at = _clock_out_request_at(runtime, session)
            if requested_at is not None:
                actions.append(
                    {
                        "action": "close_pending_clock_out_time",
                        "clock_out_requested_at": requested_at.isoformat(),
                        "complete_historical_workflow": session.session_date
                        < runtime.resolve_user_workday_date(user, now),
                    }
                )
        merged_segments, changed = merge_overlapping_segments(session.work_segments)
        if changed:
            actions.append(
                {
                    "action": "merge_overlapping_segments",
                    "old_segment_count": len(session.work_segments),
                    "new_segment_count": len(merged_segments),
                    "new_work_segments": merged_segments,
                }
            )
        if is_legacy_migration_candidate(session, legacy_date):
            actions.append(
                {
                    "action": "label_legacy_migration",
                    "origin": "legacy_migration",
                    "basis": (
                        f"{legacy_date} record has zero task allocation and was manually seeded "
                        f"by editor {_LEGACY_EDITOR!r}."
                    ),
                }
            )
        if actions:
            plan.append(
                {
                    "user_key": user.user_key,
                    "display_name": user.display_name,
                    "session_date": session.session_date,
                    "actions": actions,
                }
            )
    return plan


async def apply_plan(
    runtime: InternManagementRuntime,
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for item in plan:
        user = runtime.resolve_user_profile(str(item["user_key"]))
        if user is None:
            raise RuntimeError(f"Could not resolve active user {item['user_key']!r}.")
        session_date = str(item["session_date"])
        session = runtime._load_session_for_manual_time_edit(user, session_date)
        previous_session = runtime._clone_session_state(session)
        now = runtime.resolve_user_local_now(user)
        repair_entries: list[dict[str, Any]] = []
        for action in item["actions"]:
            action_name = str(action["action"])
            if action_name == "close_pending_clock_out_time":
                requested_at = datetime.fromisoformat(str(action["clock_out_requested_at"]))
                _close_attendance_at(session, requested_at)
                sync_warning = ""
                try:
                    await runtime._pause_current_task_tracking(
                        user,
                        session,
                        requested_at,
                        set_hold=True,
                        end_reason="time_integrity_repair_clock_out_request",
                    )
                except Exception as exc:
                    clickup = runtime.clickup
                    runtime.clickup = None
                    try:
                        await runtime._pause_current_task_tracking(
                            user,
                            session,
                            requested_at,
                            set_hold=False,
                            end_reason="time_integrity_repair_clock_out_request",
                        )
                    finally:
                        runtime.clickup = clickup
                    sync_warning = (
                        "ClickUp synchronization failed during repair; local task time was "
                        f"closed and the error was {type(exc).__name__}."
                    )
                if bool(action.get("complete_historical_workflow")):
                    session.stage = "clocked_out"
                    session.awaiting_clock_out_photo = False
                    session.awaiting_clock_out_summary = False
                    session.metadata.pop(_CLOCK_OUT_RETURN_STATE_KEY, None)
                    session.metadata.pop(_CLOCK_OUT_PAUSE_NOTE_KEY, None)
                repair_entries.append(
                    _repair_entry(
                        action=action_name,
                        now=now,
                        details={
                            "clock_out_requested_at": requested_at.isoformat(),
                            "historical_workflow_completed": bool(
                                action.get("complete_historical_workflow")
                            ),
                            "reason": (
                                "Stopped attendance and task time at the worker's first explicit "
                                "clock-out request."
                            ),
                            "sync_warning": sync_warning,
                        },
                    )
                )
            elif action_name == "merge_overlapping_segments":
                old_segments = list(session.work_segments)
                merged_segments, changed = merge_overlapping_segments(old_segments)
                if not changed:
                    continue
                session.work_segments = merged_segments
                session.clocked_in_at = str(merged_segments[0]["clocked_in_at"])
                session.clocked_out_at = str(merged_segments[-1]["clocked_out_at"])
                repair_entries.append(
                    _repair_entry(
                        action=action_name,
                        now=now,
                        details={
                            "old_work_segments": old_segments,
                            "new_work_segments": merged_segments,
                            "reason": (
                                "Consolidated overlapping attendance intervals without changing "
                                "the union of time represented."
                            ),
                        },
                    )
                )
            elif action_name == "label_legacy_migration":
                session.metadata[_TIME_RECORD_ORIGIN_KEY] = "legacy_migration"
                repair_entries.append(
                    _repair_entry(
                        action=action_name,
                        now=now,
                        details={
                            "origin": "legacy_migration",
                            "basis": str(action.get("basis") or ""),
                        },
                    )
                )
        if not repair_entries:
            continue
        _append_repair_history(session, repair_entries)
        await runtime._persist_session_state(
            user,
            session,
            now=now,
            previous_session=previous_session,
            trigger="time_integrity_repair",
            details={
                "actions": [entry["action"] for entry in repair_entries],
                "session_date": session_date,
            },
        )
        applied.append(
            {
                "user_key": user.user_key,
                "session_date": session_date,
                "actions": [entry["action"] for entry in repair_entries],
            }
        )
    await runtime.write_dashboard()
    return applied


async def run(*, apply: bool, legacy_date: str) -> dict[str, Any]:
    runtime = InternManagementRuntime()
    await runtime.refresh_configuration(force=True)
    plan = await build_plan(runtime, legacy_date=legacy_date)
    result: dict[str, Any] = {
        "mode": "apply" if apply else "preview",
        "legacy_date": legacy_date,
        "planned_session_count": len(plan),
        "plan": plan,
    }
    if apply:
        result["applied"] = await apply_plan(runtime, plan)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview or apply auditable Don Pollo time-integrity repairs."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--legacy-date", default="2026-08-05")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(apply=args.apply, legacy_date=args.legacy_date))
    if args.summary:
        action_counts: dict[str, int] = {}
        for item in result["plan"]:
            for action in item["actions"]:
                action_name = str(action["action"])
                action_counts[action_name] = action_counts.get(action_name, 0) + 1
        result = {
            "mode": result["mode"],
            "legacy_date": result["legacy_date"],
            "planned_session_count": result["planned_session_count"],
            "planned_action_count": sum(action_counts.values()),
            "action_counts": action_counts,
            "applied_session_count": len(result.get("applied") or []),
        }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

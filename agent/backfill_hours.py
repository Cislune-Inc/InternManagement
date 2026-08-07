from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .models import SessionState, UserProfile
from .runtime import InternManagementRuntime, _RETRO_HOURS_BACKFILL_METADATA_KEY
from .signals import detect_signals
from .time_tracking_dashboard import write_time_tracking_dashboard
from .time_utils import ADMIN_DISPLAY_TIMEZONE, localize_datetime, resolve_timezone


_EXPLANATION_FILENAME = "hours_backfill_explanation.json"
_RETRO_AUDIT_RELATIVE_PATH = Path("dashboard") / "time_tracking" / "retro_backfill"
_SESSION_DATE_DIRECTORY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TRANSCRIPT_HEADING_PATTERN = re.compile(r"^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - (.+)$")
_CLOCK_IN_TEXT_PATTERNS = (
    re.compile(r"\b(?:clocked\s+in|clock\s+in|came\s+in)\s+(?:at\s+)?(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", re.IGNORECASE),
)
_CLOCK_OUT_TEXT_PATTERNS = (
    re.compile(r"\b(?:clocked\s+out|clock\s+out|left)\s+(?:at\s+)?(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", re.IGNORECASE),
)


@dataclass(slots=True)
class ArchivedMessage:
    created_at: datetime
    direction: str
    content: str
    source: str


@dataclass(slots=True)
class TimeCandidate:
    at: datetime
    source: str
    note: str


@dataclass(slots=True)
class ArchivedDay:
    user: UserProfile
    daily_dir: Path
    session_path: Path
    transcript_path: Path
    state_changes_path: Path
    session: SessionState


class RetroHoursBackfiller:
    def __init__(self, runtime: InternManagementRuntime) -> None:
        self.runtime = runtime

    def run(
        self,
        *,
        apply: bool,
        user_keys: set[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        reference_now = now or datetime.now(tz=resolve_timezone(self.runtime.runtime_timezone_name()))
        run_id = reference_now.strftime("%Y%m%d-%H%M%S")
        storage_root = self.runtime._storage_root_path()
        if storage_root is None:
            raise RuntimeError("Storage root is unavailable for retro hours backfill.")
        audit_root = storage_root / _RETRO_AUDIT_RELATIVE_PATH / run_id
        explanation_root = audit_root / "day_explanations"
        original_root = audit_root / "original_sessions"
        audit_rows: list[dict[str, Any]] = []
        warnings_total = 0
        changed_count = 0
        unchanged_count = 0
        unresolved_count = 0
        processed_count = 0
        skipped_count = 0
        audit_root.mkdir(parents=True, exist_ok=True)

        for day in self._iter_archived_days(
            user_keys=user_keys,
            date_from=date_from,
            date_to=date_to,
        ):
            result = self._reconstruct_day(day, run_id=run_id, reference_now=reference_now)
            processed_count += 1
            warnings_total += len(result["warnings"])
            if result["confidence"] == "unresolved":
                unresolved_count += 1
            if result["changed"]:
                changed_count += 1
            else:
                unchanged_count += 1

            audit_explanation_path = explanation_root / day.user.storage_folder_name / day.session.session_date / _EXPLANATION_FILENAME
            audit_explanation_path.parent.mkdir(parents=True, exist_ok=True)
            audit_explanation_path.write_text(
                json.dumps(result["explanation_payload"], indent=2, sort_keys=True),
                encoding="utf-8",
            )

            daily_explanation_path = day.daily_dir / _EXPLANATION_FILENAME
            if apply and result["changed"]:
                if result["session_text"] != result["original_session_text"]:
                    backup_path = original_root / day.user.storage_folder_name / day.session.session_date / "session.json"
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(day.session_path, backup_path)
                    day.session_path.write_text(result["session_text"], encoding="utf-8")
                    self.runtime.state_store.save_session(result["session"])
                if result["daily_explanation_text"] != result["original_daily_explanation_text"]:
                    daily_explanation_path.write_text(result["daily_explanation_text"], encoding="utf-8")

            audit_rows.append(
                {
                    "user_key": day.session.user_key,
                    "session_date": day.session.session_date,
                    "changed": result["changed"],
                    "confidence": result["confidence"],
                    "clocked_in_before_seconds": result["before_clocked_in_seconds"],
                    "clocked_in_after_seconds": result["after_clocked_in_seconds"],
                    "task_tracked_before_seconds": result["before_task_seconds"],
                    "task_tracked_after_seconds": result["after_task_seconds"],
                    "clock_in_source": result["clock_in_source"],
                    "clock_out_source": result["clock_out_source"],
                    "task_time_source": result["task_time_source"],
                    "explanation_path": str((daily_explanation_path if apply else audit_explanation_path).resolve()),
                }
            )

        audit_csv_path = audit_root / "audit.csv"
        with audit_csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "user_key",
                    "session_date",
                    "changed",
                    "confidence",
                    "clocked_in_before_seconds",
                    "clocked_in_after_seconds",
                    "task_tracked_before_seconds",
                    "task_tracked_after_seconds",
                    "clock_in_source",
                    "clock_out_source",
                    "task_time_source",
                    "explanation_path",
                ],
            )
            writer.writeheader()
            for row in audit_rows:
                writer.writerow(row)

        if apply:
            self.runtime._write_time_tracking_csv_sync(reference_now)

        summary = {
            "run_id": run_id,
            "apply": apply,
            "processed_days": processed_count,
            "changed_days": changed_count,
            "unchanged_days": unchanged_count,
            "unresolved_days": unresolved_count,
            "warnings_count": warnings_total,
            "skipped_days": skipped_count,
            "audit_root": str(audit_root.resolve()),
            "audit_csv_path": str(audit_csv_path.resolve()),
        }
        (audit_root / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        write_time_tracking_dashboard(storage_root)
        return summary

    def _iter_archived_days(
        self,
        *,
        user_keys: set[str] | None,
        date_from: str | None,
        date_to: str | None,
    ) -> list[ArchivedDay]:
        storage_root = self.runtime._storage_root_path()
        if storage_root is None:
            return []
        people_dir = storage_root / "people"
        if not people_dir.exists():
            return []
        days: list[ArchivedDay] = []
        for user_dir in sorted(people_dir.iterdir(), key=lambda path: path.name.lower()):
            if not user_dir.is_dir():
                continue
            profile_path = user_dir / "profile.json"
            profile: UserProfile | None = None
            if profile_path.exists():
                try:
                    profile = UserProfile(**json.loads(profile_path.read_text(encoding="utf-8")))
                except (TypeError, ValueError, json.JSONDecodeError):
                    profile = None
            for daily_dir in sorted(user_dir.iterdir(), key=lambda path: path.name):
                if not daily_dir.is_dir() or not _SESSION_DATE_DIRECTORY_PATTERN.fullmatch(daily_dir.name):
                    continue
                session_path = daily_dir / "session.json"
                if not session_path.exists():
                    continue
                try:
                    session = SessionState(**json.loads(session_path.read_text(encoding="utf-8")))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if user_keys and session.user_key not in user_keys and user_dir.name not in user_keys:
                    continue
                if date_from and session.session_date < date_from:
                    continue
                if date_to and session.session_date > date_to:
                    continue
                user = profile or UserProfile(
                    user_key=session.user_key,
                    display_name=session.user_key,
                    discord_user_id=0,
                    discord_username=session.user_key,
                    storage_folder_name=user_dir.name,
                )
                days.append(
                    ArchivedDay(
                        user=user,
                        daily_dir=daily_dir,
                        session_path=session_path,
                        transcript_path=daily_dir / "transcript.md",
                        state_changes_path=daily_dir / "state_machine_changes.jsonl",
                        session=session,
                    )
                )
        return days

    def _reconstruct_day(
        self,
        day: ArchivedDay,
        *,
        run_id: str,
        reference_now: datetime,
    ) -> dict[str, Any]:
        user = day.user
        original = self.runtime._clone_session_state(day.session)
        self.runtime._normalize_session_state(original, user=user)
        before_now = self.runtime._session_time_summary_reference_now(original, user, reference_now)
        self.runtime._refresh_session_time_summary(original, before_now)

        messages, message_source = self._load_archived_messages(
            user,
            session_date=original.session_date,
            transcript_path=day.transcript_path,
        )
        state_entries = self._load_state_entries(day.state_changes_path)
        evidence = self._collect_evidence(
            user,
            session_date=original.session_date,
            original=original,
            messages=messages,
            state_entries=state_entries,
            reference_now=reference_now,
        )

        rebuilt = self.runtime._clone_session_state(original)
        rebuilt_segments = self._choose_work_segments(
            user,
            session=original,
            evidence=evidence,
            reference_now=reference_now,
        )
        warnings = list(evidence["warnings"])
        if original.work_segments and not self._segments_are_sane(
            [
                {
                    "clocked_in_at": str(segment.get("clocked_in_at") or "") or None,
                    "clocked_out_at": str(segment.get("clocked_out_at") or "") or None,
                }
                for segment in original.work_segments
                if isinstance(segment, dict) and segment.get("clocked_in_at")
            ],
            timezone_name=self.runtime.resolve_user_timezone_name(user),
            session_date=original.session_date,
            reference_now=reference_now,
        ):
            warnings.append("Stored work segments were rebuilt because the archived boundaries were duplicated, overlapping, open, or inflated.")
        if rebuilt_segments:
            rebuilt.work_segments = [
                {
                    "clocked_in_at": segment["clocked_in_at"],
                    "clocked_out_at": segment["clocked_out_at"],
                }
                for segment in rebuilt_segments
            ]
            rebuilt.clocked_in_at = rebuilt_segments[0]["clocked_in_at"]
            rebuilt.clocked_out_at = rebuilt_segments[-1]["clocked_out_at"]
        task_windows, task_time_source = self._build_task_windows(
            user,
            session=original,
            evidence=evidence,
            final_clock_out=rebuilt.clocked_out_at,
        )
        if not task_windows:
            task_time_source = "none"
        confidence = self._confidence_for_day(
            clock_in_source=evidence["clock_in_source"],
            clock_out_source=evidence["clock_out_source"],
            warnings=warnings,
            resolved=bool(rebuilt_segments),
        )

        explanation = self._build_explanation_text(
            clock_in_source=evidence["clock_in_source"],
            clock_out_source=evidence["clock_out_source"],
            task_time_source=task_time_source,
            rebuilt_segments=rebuilt_segments,
            task_windows=task_windows,
            warnings=warnings,
        )

        provenance = {
            "backfilled_at": reference_now.isoformat(),
            "backfill_run_id": run_id,
            "confidence": confidence,
            "changed": False,
            "sources_used": sorted(
                {
                    "session_json",
                    *(["state_machine_changes"] if state_entries else []),
                    *(["sqlite_messages"] if message_source == "sqlite_messages" else []),
                    *(["transcript_markdown"] if message_source == "transcript_markdown" else []),
                }
            ),
            "clock_in_at": rebuilt.clocked_in_at,
            "clock_out_at": rebuilt.clocked_out_at,
            "clock_in_source": evidence["clock_in_source"],
            "clock_out_source": evidence["clock_out_source"],
            "task_time_source": task_time_source,
            "work_segments": rebuilt_segments,
            "lunch_windows": evidence["lunch_windows"],
            "task_windows": task_windows,
            "explanation": explanation,
            "warnings": warnings,
        }
        rebuilt.metadata[_RETRO_HOURS_BACKFILL_METADATA_KEY] = provenance

        after_now = self._summary_reference_now_for_rebuilt_session(user, rebuilt, reference_now)
        self.runtime._refresh_session_time_summary(rebuilt, after_now)

        explanation_payload = {
            "user_key": rebuilt.user_key,
            "display_name": user.display_name,
            "session_date": rebuilt.session_date,
            "backfilled_at": reference_now.isoformat(),
            "backfill_run_id": run_id,
            "confidence": confidence,
            "changed": False,
            "sources_used": provenance["sources_used"],
            "clock_in_at": rebuilt.clocked_in_at,
            "clock_out_at": rebuilt.clocked_out_at,
            "clock_in_source": evidence["clock_in_source"],
            "clock_out_source": evidence["clock_out_source"],
            "task_time_source": task_time_source,
            "clocked_in_before_seconds": int(original.time_summary.get("clocked_in_total_seconds") or 0),
            "clocked_in_after_seconds": int(rebuilt.time_summary.get("clocked_in_total_seconds") or 0),
            "task_tracked_before_seconds": int(original.time_summary.get("task_tracked_total_seconds") or 0),
            "task_tracked_after_seconds": int(rebuilt.time_summary.get("task_tracked_total_seconds") or 0),
            "clocked_in_before_human": str(original.time_summary.get("clocked_in_total_human") or "0m"),
            "clocked_in_after_human": str(rebuilt.time_summary.get("clocked_in_total_human") or "0m"),
            "task_tracked_before_human": str(original.time_summary.get("task_tracked_total_human") or "0m"),
            "task_tracked_after_human": str(rebuilt.time_summary.get("task_tracked_total_human") or "0m"),
            "work_segments": rebuilt_segments,
            "lunch_windows": evidence["lunch_windows"],
            "task_windows": task_windows,
            "warnings": warnings,
            "explanation": explanation,
        }

        original_session_text = json.dumps(asdict(day.session), indent=2, sort_keys=True)
        session_text = json.dumps(asdict(rebuilt), indent=2, sort_keys=True)
        original_daily_explanation_text = (
            (day.daily_dir / _EXPLANATION_FILENAME).read_text(encoding="utf-8")
            if (day.daily_dir / _EXPLANATION_FILENAME).exists()
            else ""
        )
        daily_explanation_text = json.dumps(explanation_payload, indent=2, sort_keys=True)
        changed = session_text != original_session_text or daily_explanation_text != original_daily_explanation_text
        provenance["changed"] = changed
        explanation_payload["changed"] = changed
        rebuilt.metadata[_RETRO_HOURS_BACKFILL_METADATA_KEY]["changed"] = changed
        session_text = json.dumps(asdict(rebuilt), indent=2, sort_keys=True)

        return {
            "session": rebuilt,
            "changed": changed,
            "confidence": confidence,
            "warnings": warnings,
            "clock_in_source": evidence["clock_in_source"],
            "clock_out_source": evidence["clock_out_source"],
            "task_time_source": task_time_source,
            "before_clocked_in_seconds": int(original.time_summary.get("clocked_in_total_seconds") or 0),
            "after_clocked_in_seconds": int(rebuilt.time_summary.get("clocked_in_total_seconds") or 0),
            "before_task_seconds": int(original.time_summary.get("task_tracked_total_seconds") or 0),
            "after_task_seconds": int(rebuilt.time_summary.get("task_tracked_total_seconds") or 0),
            "explanation_payload": explanation_payload,
            "daily_explanation_text": daily_explanation_text,
            "original_daily_explanation_text": original_daily_explanation_text,
            "session_text": session_text,
            "original_session_text": original_session_text,
        }

    def _load_archived_messages(
        self,
        user: UserProfile,
        *,
        session_date: str,
        transcript_path: Path,
    ) -> tuple[list[ArchivedMessage], str]:
        records = self.runtime.state_store.list_messages(user.user_key, session_date)
        if records:
            return [
                ArchivedMessage(
                    created_at=record.created_at,
                    direction=record.direction,
                    content=record.content or "",
                    source="sqlite_messages",
                )
                for record in records
            ], "sqlite_messages"
        if not transcript_path.exists():
            return [], "none"
        return self._parse_transcript_messages(user, transcript_path), "transcript_markdown"

    def _parse_transcript_messages(self, user: UserProfile, transcript_path: Path) -> list[ArchivedMessage]:
        lines = transcript_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        messages: list[ArchivedMessage] = []
        index = 0
        while index < len(lines):
            match = _TRANSCRIPT_HEADING_PATTERN.match(lines[index])
            if not match:
                index += 1
                continue
            timestamp_text, author = match.groups()
            created_at = localize_datetime(
                datetime.strptime(timestamp_text, "%Y-%m-%d %H:%M:%S"),
                ADMIN_DISPLAY_TIMEZONE,
            )
            index += 1
            block: list[str] = []
            while index < len(lines) and not _TRANSCRIPT_HEADING_PATTERN.match(lines[index]):
                block.append(lines[index])
                index += 1
            content = "\n".join(block).strip()
            direction = "outbound" if author == "Agent" else "inbound"
            if direction == "inbound" and author not in {user.display_name, user.discord_username, user.user_key}:
                direction = "outbound" if author == "Agent" else "inbound"
            messages.append(
                ArchivedMessage(
                    created_at=created_at,
                    direction=direction,
                    content=content,
                    source="transcript_markdown",
                )
            )
        return messages

    def _load_state_entries(self, state_changes_path: Path) -> list[dict[str, Any]]:
        if not state_changes_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for raw_line in state_changes_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return entries

    def _collect_evidence(
        self,
        user: UserProfile,
        *,
        session_date: str,
        original: SessionState,
        messages: list[ArchivedMessage],
        state_entries: list[dict[str, Any]],
        reference_now: datetime,
    ) -> dict[str, Any]:
        timezone_name = self.runtime.resolve_user_timezone_name(user)
        explicit_clock_in: list[TimeCandidate] = []
        clock_in_signals: list[TimeCandidate] = []
        explicit_clock_out: list[TimeCandidate] = []
        clock_out_signals: list[TimeCandidate] = []
        last_user_activity_same_day: datetime | None = None
        last_any_activity_same_day: datetime | None = None
        lunch_starts: list[TimeCandidate] = []
        lunch_ends: list[TimeCandidate] = []
        warnings: list[str] = []

        for message in messages:
            message_local = localize_datetime(message.created_at, timezone_name)
            if self.runtime.resolve_user_workday_date(user, message_local) != session_date:
                continue
            last_any_activity_same_day = self._later_datetime(last_any_activity_same_day, message_local)
            if message.direction == "inbound":
                last_user_activity_same_day = self._later_datetime(last_user_activity_same_day, message_local)
                signals = detect_signals(message.content)
                explicit_start = self._extract_stated_time(
                    message.content,
                    message_local=message_local,
                    session_date=session_date,
                    timezone_name=timezone_name,
                    patterns=_CLOCK_IN_TEXT_PATTERNS,
                )
                if explicit_start is not None:
                    explicit_clock_in.append(
                        TimeCandidate(
                            at=explicit_start,
                            source="explicit_clock_in_text",
                            note="Used the stated clock-in time from the inbound message.",
                        )
                    )
                if getattr(signals, "clocked_in", False):
                    clock_in_signals.append(
                        TimeCandidate(
                            at=message_local,
                            source="inbound_clock_in_signal",
                            note="Used the inbound clock-in confirmation message time.",
                        )
                    )
                explicit_end = self._extract_stated_time(
                    message.content,
                    message_local=message_local,
                    session_date=session_date,
                    timezone_name=timezone_name,
                    patterns=_CLOCK_OUT_TEXT_PATTERNS,
                )
                if explicit_end is not None:
                    explicit_clock_out.append(
                        TimeCandidate(
                            at=explicit_end,
                            source="explicit_clock_out_text",
                            note="Used the stated clock-out time from the inbound message.",
                        )
                    )
                if getattr(signals, "clocking_out", False):
                    clock_out_signals.append(
                        TimeCandidate(
                            at=message_local,
                            source="inbound_clock_out_intent",
                            note="Used the inbound clock-out intent message time.",
                        )
                    )
                if getattr(signals, "starting_lunch", False):
                    lunch_starts.append(
                        TimeCandidate(
                            at=message_local,
                            source="inbound_lunch_start_signal",
                            note="Used the inbound lunch-start message time.",
                        )
                    )
                if getattr(signals, "ending_lunch", False):
                    lunch_ends.append(
                        TimeCandidate(
                            at=message_local,
                            source="inbound_lunch_end_signal",
                            note="Used the inbound lunch-end message time.",
                        )
                    )

        state_clock_in: list[TimeCandidate] = []
        state_clock_out: list[TimeCandidate] = []
        state_stage_clock_out: list[TimeCandidate] = []
        state_segment_starts: list[TimeCandidate] = []
        state_segment_ends: list[TimeCandidate] = []
        for entry in state_entries:
            recorded_at = self.runtime._coerce_datetime(
                str(entry.get("recorded_at") or ""),
                timezone_name=timezone_name,
            )
            current = entry.get("current")
            previous = entry.get("previous")
            if not isinstance(current, dict):
                continue
            if recorded_at and self.runtime.resolve_user_workday_date(user, recorded_at) == session_date:
                last_any_activity_same_day = self._later_datetime(last_any_activity_same_day, recorded_at)
            timestamps = current.get("timestamps") if isinstance(current.get("timestamps"), dict) else {}
            current_clock_in = self.runtime._coerce_datetime(
                str(timestamps.get("clocked_in_at") or ""),
                timezone_name=timezone_name,
            )
            if current_clock_in and self.runtime.resolve_user_workday_date(user, current_clock_in) == session_date:
                state_clock_in.append(
                    TimeCandidate(
                        at=current_clock_in,
                        source="state_machine_clocked_in_at",
                        note="Used the structured clock-in timestamp from the state-machine log.",
                    )
                )
            current_clock_out = self.runtime._coerce_datetime(
                str(timestamps.get("clocked_out_at") or ""),
                timezone_name=timezone_name,
            )
            if current_clock_out and self.runtime.resolve_user_workday_date(user, current_clock_out) == session_date:
                state_clock_out.append(
                    TimeCandidate(
                        at=current_clock_out,
                        source="state_machine_clocked_out_at",
                        note="Used the structured clock-out timestamp from the state-machine log.",
                    )
                )
            current_stage = str(current.get("stage") or "")
            previous_stage = str(previous.get("stage") or "") if isinstance(previous, dict) else ""
            if recorded_at and current_stage in {"awaiting_clock_out_artifacts", "clocked_out"} and previous_stage != current_stage:
                if self.runtime.resolve_user_workday_date(user, recorded_at) == session_date:
                    state_stage_clock_out.append(
                        TimeCandidate(
                            at=recorded_at,
                            source="state_machine_stage_clock_out",
                            note="Used the state-machine transition into clock-out handling.",
                        )
                    )
            workday = current.get("workday") if isinstance(current.get("workday"), dict) else {}
            work_segments = workday.get("work_segments") if isinstance(workday.get("work_segments"), list) else []
            for segment in work_segments:
                if not isinstance(segment, dict):
                    continue
                start_dt = self.runtime._coerce_datetime(
                    str(segment.get("clocked_in_at") or ""),
                    timezone_name=timezone_name,
                )
                end_dt = self.runtime._coerce_datetime(
                    str(segment.get("clocked_out_at") or ""),
                    timezone_name=timezone_name,
                )
                if start_dt and self.runtime.resolve_user_workday_date(user, start_dt) == session_date:
                    state_segment_starts.append(
                        TimeCandidate(
                            at=start_dt,
                            source="state_machine_work_segments",
                            note="Used the structured work-segment start from the state-machine log.",
                        )
                    )
                if end_dt and self.runtime.resolve_user_workday_date(user, end_dt) == session_date:
                    state_segment_ends.append(
                        TimeCandidate(
                            at=end_dt,
                            source="state_machine_work_segments",
                            note="Used the structured work-segment end from the state-machine log.",
                        )
                    )
            automation = current.get("automation") if isinstance(current.get("automation"), dict) else {}
            lunch_started = self.runtime._coerce_datetime(
                str(automation.get("lunch_started_at") or ""),
                timezone_name=timezone_name,
            )
            lunch_ended = self.runtime._coerce_datetime(
                str(automation.get("lunch_ended_at") or ""),
                timezone_name=timezone_name,
            )
            if lunch_started and self.runtime.resolve_user_workday_date(user, lunch_started) == session_date:
                lunch_starts.append(
                    TimeCandidate(
                        at=lunch_started,
                        source="state_machine_lunch_start",
                        note="Used the structured lunch-start timestamp from the state-machine log.",
                    )
                )
            if lunch_ended and self.runtime.resolve_user_workday_date(user, lunch_ended) == session_date:
                lunch_ends.append(
                    TimeCandidate(
                        at=lunch_ended,
                        source="state_machine_lunch_end",
                        note="Used the structured lunch-end timestamp from the state-machine log.",
                    )
                )

        session_clock_in = self.runtime._coerce_datetime(original.clocked_in_at, timezone_name=timezone_name)
        session_clock_out = self.runtime._coerce_datetime(original.clocked_out_at, timezone_name=timezone_name)

        stored_segment_starts = self._unique_times(
            [
                TimeCandidate(
                    at=self.runtime._coerce_datetime(
                        str(segment.get("clocked_in_at") or ""),
                        timezone_name=timezone_name,
                    ),
                    source="stored_work_segments",
                    note="Used the stored work-segment start from session.json.",
                )
                for segment in original.work_segments
                if isinstance(segment, dict)
                and self.runtime._coerce_datetime(
                    str(segment.get("clocked_in_at") or ""),
                    timezone_name=timezone_name,
                )
            ]
        )
        stored_segment_ends = self._unique_times(
            [
                TimeCandidate(
                    at=self.runtime._coerce_datetime(
                        str(segment.get("clocked_out_at") or ""),
                        timezone_name=timezone_name,
                    ),
                    source="stored_work_segments",
                    note="Used the stored work-segment end from session.json.",
                )
                for segment in original.work_segments
                if isinstance(segment, dict)
                and self.runtime._coerce_datetime(
                    str(segment.get("clocked_out_at") or ""),
                    timezone_name=timezone_name,
                )
            ]
        )

        clock_in_candidate = self._first_candidate(
            explicit_clock_in
            or state_clock_in
            or clock_in_signals
            or (
                [TimeCandidate(session_clock_in, "session_json_clocked_in_at", "Used the stored session clock-in timestamp.")]
                if session_clock_in
                else []
            )
            or stored_segment_starts
        )
        clock_out_candidate = self._first_candidate(
            explicit_clock_out
            or clock_out_signals
            or state_stage_clock_out
            or state_clock_out
            or (
                [TimeCandidate(session_clock_out, "session_json_clocked_out_at", "Used the stored session clock-out timestamp.")]
                if session_clock_out
                else []
            )
            or stored_segment_ends
        )

        if clock_out_candidate and clock_in_candidate:
            if clock_out_candidate.at.date() != datetime.fromisoformat(session_date).date() and (
                clock_out_candidate.at - clock_in_candidate.at
            ) > timedelta(hours=12):
                warnings.append("Stored clock-out looked overnight and was treated as suspicious for the final segment.")
                clock_out_candidate = None

        clamp_candidate = self._first_candidate(
            (
                [TimeCandidate(last_user_activity_same_day, "clamped_last_user_activity", "Clamped the day to the last same-day inbound activity.")]
                if last_user_activity_same_day
                else []
            )
            or (
                [TimeCandidate(last_any_activity_same_day, "clamped_last_same_day_activity", "Clamped the day to the last same-day archived activity.")]
                if last_any_activity_same_day
                else []
            )
        )
        final_clock_out = clock_out_candidate or clamp_candidate
        if clock_out_candidate is None and clamp_candidate is not None:
            warnings.append("Final open work segment was clamped to the latest reliable same-day activity.")

        lunch_windows = self._pair_lunch_windows(
            starts=self._unique_times(lunch_starts),
            ends=self._unique_times(lunch_ends),
        )

        return {
            "clock_in_candidate": clock_in_candidate,
            "clock_out_candidate": final_clock_out,
            "clock_in_source": clock_in_candidate.source if clock_in_candidate else "unresolved",
            "clock_out_source": final_clock_out.source if final_clock_out else "unresolved",
            "state_segment_starts": self._unique_times(state_segment_starts),
            "state_segment_ends": self._unique_times(state_segment_ends),
            "stored_segment_starts": stored_segment_starts,
            "stored_segment_ends": stored_segment_ends,
            "warnings": warnings,
            "lunch_windows": lunch_windows,
        }

    def _choose_work_segments(
        self,
        user: UserProfile,
        *,
        session: SessionState,
        evidence: dict[str, Any],
        reference_now: datetime,
    ) -> list[dict[str, str | None]]:
        timezone_name = self.runtime.resolve_user_timezone_name(user)
        stored_segments = [
            {
                "clocked_in_at": str(segment.get("clocked_in_at") or "") or None,
                "clocked_out_at": str(segment.get("clocked_out_at") or "") or None,
            }
            for segment in session.work_segments
            if isinstance(segment, dict) and segment.get("clocked_in_at")
        ]
        if self._segments_are_sane(
            stored_segments,
            timezone_name=timezone_name,
            session_date=session.session_date,
            reference_now=reference_now,
        ):
            segments = [dict(segment) for segment in stored_segments]
            first_start = evidence["clock_in_candidate"]
            if first_start is not None:
                segments[0]["clocked_in_at"] = first_start.at.isoformat()
            final_end = evidence["clock_out_candidate"]
            if final_end is not None:
                segments[-1]["clocked_out_at"] = final_end.at.isoformat()
            return segments

        chosen_start = evidence["clock_in_candidate"]
        if chosen_start is None:
            return []
        archived_starts = self._unique_times(
            evidence["state_segment_starts"] + evidence["stored_segment_starts"]
        )
        archived_ends = self._unique_times(
            evidence["state_segment_ends"] + evidence["stored_segment_ends"]
        )
        starts = [candidate.at for candidate in archived_starts]
        if len(starts) <= 1:
            starts = [chosen_start.at]
        else:
            starts[0] = chosen_start.at
        starts = sorted(self._dedupe_datetimes(starts))
        final_end = evidence["clock_out_candidate"]
        segments: list[dict[str, str | None]] = []
        for index, start_dt in enumerate(starts):
            next_start = starts[index + 1] if index + 1 < len(starts) else None
            end_dt = self._find_segment_end(
                start_dt=start_dt,
                next_start=next_start,
                ends=[candidate.at for candidate in archived_ends],
                final_end=final_end.at if final_end is not None else None,
            )
            segments.append(
                {
                    "clocked_in_at": start_dt.isoformat(),
                    "clocked_out_at": end_dt.isoformat() if end_dt is not None else None,
                }
            )
        return segments

    def _build_task_windows(
        self,
        user: UserProfile,
        *,
        session: SessionState,
        evidence: dict[str, Any],
        final_clock_out: str | None,
    ) -> tuple[list[dict[str, Any]], str]:
        timezone_name = self.runtime.resolve_user_timezone_name(user)
        windows: list[dict[str, Any]] = []
        entries: list[dict[str, Any]] = []
        history = session.metadata.get("clickup_time_tracking_history")
        if isinstance(history, list):
            entries.extend(item for item in history if isinstance(item, dict))
        current = session.metadata.get("clickup_time_tracking")
        if isinstance(current, dict):
            entries.append(current)
        final_clock_out_dt = self.runtime._coerce_datetime(final_clock_out, timezone_name=timezone_name)
        lunch_windows = evidence["lunch_windows"]
        for entry in entries:
            task_id = str(entry.get("task_id") or "") or None
            task_name = str(entry.get("task_name") or "") or None
            started_at = self.runtime._coerce_datetime(
                str(entry.get("started_at") or ""),
                timezone_name=timezone_name,
            )
            if started_at is None:
                continue
            end_dt = self.runtime._coerce_datetime(
                str(entry.get("closed_at") or ""),
                timezone_name=timezone_name,
            )
            if end_dt is None:
                end_dt = final_clock_out_dt
            if end_dt is None or end_dt <= started_at:
                continue
            base_window = {
                "task_id": task_id,
                "task_name": task_name,
                "started_at": started_at.isoformat(),
                "ended_at": end_dt.isoformat(),
                "source": "stored_time_entries",
                "note": "Rebuilt from explicit stored ClickUp time-tracking entries.",
            }
            for split_window in self._subtract_lunch_overlaps(base_window, lunch_windows, timezone_name=timezone_name):
                windows.append(split_window)
        for window in windows:
            window["duration_seconds"] = self.runtime._retro_task_window_seconds(window)
        return windows, ("stored_time_entries" if windows else "none")

    def _subtract_lunch_overlaps(
        self,
        window: dict[str, Any],
        lunch_windows: list[dict[str, str | None]],
        *,
        timezone_name: str,
    ) -> list[dict[str, Any]]:
        start_dt = self.runtime._coerce_datetime(str(window.get("started_at") or ""), timezone_name=timezone_name)
        end_dt = self.runtime._coerce_datetime(str(window.get("ended_at") or ""), timezone_name=timezone_name)
        if start_dt is None or end_dt is None or end_dt <= start_dt:
            return []
        intervals = [(start_dt, end_dt)]
        for lunch_window in lunch_windows:
            lunch_start = self.runtime._coerce_datetime(
                str(lunch_window.get("started_at") or ""),
                timezone_name=timezone_name,
            )
            lunch_end = self.runtime._coerce_datetime(
                str(lunch_window.get("ended_at") or ""),
                timezone_name=timezone_name,
            )
            if lunch_start is None or lunch_end is None or lunch_end <= lunch_start:
                continue
            updated: list[tuple[datetime, datetime]] = []
            for interval_start, interval_end in intervals:
                if lunch_end <= interval_start or lunch_start >= interval_end:
                    updated.append((interval_start, interval_end))
                    continue
                if lunch_start > interval_start:
                    updated.append((interval_start, lunch_start))
                if lunch_end < interval_end:
                    updated.append((lunch_end, interval_end))
            intervals = updated
        return [
            {
                **window,
                "started_at": interval_start.isoformat(),
                "ended_at": interval_end.isoformat(),
            }
            for interval_start, interval_end in intervals
            if interval_end > interval_start
        ]

    def _pair_lunch_windows(
        self,
        *,
        starts: list[TimeCandidate],
        ends: list[TimeCandidate],
    ) -> list[dict[str, str | None]]:
        windows: list[dict[str, str | None]] = []
        remaining_ends = [candidate.at for candidate in ends]
        for start in starts:
            end_at: datetime | None = None
            for candidate_end in list(remaining_ends):
                if candidate_end > start.at:
                    end_at = candidate_end
                    remaining_ends.remove(candidate_end)
                    break
            windows.append(
                {
                    "started_at": start.at.isoformat(),
                    "ended_at": end_at.isoformat() if end_at is not None else None,
                    "source": start.source,
                }
            )
        return windows

    def _find_segment_end(
        self,
        *,
        start_dt: datetime,
        next_start: datetime | None,
        ends: list[datetime],
        final_end: datetime | None,
    ) -> datetime | None:
        for candidate_end in sorted(self._dedupe_datetimes(ends)):
            if candidate_end <= start_dt:
                continue
            if next_start is not None and candidate_end > next_start:
                continue
            return candidate_end
        if final_end is not None and final_end > start_dt:
            if next_start is None or final_end <= next_start:
                return final_end
        return None

    def _segments_are_sane(
        self,
        segments: list[dict[str, str | None]],
        *,
        timezone_name: str,
        session_date: str,
        reference_now: datetime,
    ) -> bool:
        if not segments:
            return False
        current_workday = self.runtime.resolve_user_workday_date(
            UserProfile(
                user_key="audit",
                display_name="audit",
                discord_user_id=0,
                discord_username="audit",
                storage_folder_name="audit",
                timezone=timezone_name,
            ),
            reference_now,
        )
        seen_starts: set[str] = set()
        previous_end: datetime | None = None
        total_seconds = 0
        for segment in segments:
            start_dt = self.runtime._coerce_datetime(str(segment.get("clocked_in_at") or ""), timezone_name=timezone_name)
            end_dt = self.runtime._coerce_datetime(str(segment.get("clocked_out_at") or ""), timezone_name=timezone_name)
            if start_dt is None:
                return False
            start_key = start_dt.isoformat()
            if start_key in seen_starts:
                return False
            seen_starts.add(start_key)
            if end_dt is not None:
                if end_dt <= start_dt:
                    return False
                if previous_end is not None and start_dt < previous_end:
                    return False
                if (end_dt - start_dt) > timedelta(hours=16):
                    return False
                total_seconds += int((end_dt - start_dt).total_seconds())
                previous_end = end_dt
            elif session_date < current_workday:
                return False
        return total_seconds <= 16 * 3600 * max(1, len(segments))

    def _extract_stated_time(
        self,
        content: str,
        *,
        message_local: datetime,
        session_date: str,
        timezone_name: str,
        patterns: tuple[re.Pattern[str], ...],
    ) -> datetime | None:
        for pattern in patterns:
            match = pattern.search(content)
            if not match:
                continue
            parsed = self._parse_local_clock_text(
                match.group("time"),
                message_local=message_local,
                session_date=session_date,
                timezone_name=timezone_name,
            )
            if parsed is not None:
                return parsed
        return None

    def _parse_local_clock_text(
        self,
        raw_time: str,
        *,
        message_local: datetime,
        session_date: str,
        timezone_name: str,
    ) -> datetime | None:
        text = str(raw_time or "").strip().lower().replace(".", "")
        match = re.fullmatch(r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?", text)
        if not match:
            return None
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or "0")
        ampm = match.group("ampm")
        if minute > 59 or hour < 0 or hour > 23:
            return None
        session_day = datetime.fromisoformat(session_date).date()
        tz = resolve_timezone(timezone_name)
        if ampm:
            normalized_hour = hour % 12
            if ampm == "pm":
                normalized_hour += 12
            return datetime.combine(session_day, time(normalized_hour, minute), tzinfo=tz)
        if hour > 12:
            return datetime.combine(session_day, time(hour, minute), tzinfo=tz)
        candidates = [
            datetime.combine(session_day, time(hour % 12, minute), tzinfo=tz),
            datetime.combine(session_day, time((hour % 12) + 12, minute), tzinfo=tz),
        ]
        valid = [candidate for candidate in candidates if candidate <= message_local + timedelta(minutes=15)]
        if valid:
            return max(valid)
        return min(candidates, key=lambda candidate: abs(candidate - message_local))

    def _summary_reference_now_for_rebuilt_session(
        self,
        user: UserProfile,
        session: SessionState,
        reference_now: datetime,
    ) -> datetime:
        if session.clocked_out_at:
            clocked_out = self.runtime._coerce_datetime(
                session.clocked_out_at,
                timezone_name=self.runtime.resolve_user_timezone_name(user),
            )
            if clocked_out is not None:
                return clocked_out
        return self.runtime._session_time_summary_reference_now(session, user, reference_now)

    def _build_explanation_text(
        self,
        *,
        clock_in_source: str,
        clock_out_source: str,
        task_time_source: str,
        rebuilt_segments: list[dict[str, str | None]],
        task_windows: list[dict[str, Any]],
        warnings: list[str],
    ) -> str:
        parts: list[str] = []
        if rebuilt_segments:
            parts.append(
                f"Clocked-in time starts from `{clock_in_source}` and ends from `{clock_out_source}`."
            )
        else:
            parts.append("Clocked-in time could not be reconstructed confidently from the archived evidence.")
        if task_windows:
            parts.append(
                f"Task-tracked time came from `{task_time_source}` across {len(task_windows)} explicit task window(s)."
            )
        else:
            parts.append("Task-tracked time stayed at zero because there was no explicit timer or task-boundary evidence to replay.")
        if warnings:
            parts.append("Warnings: " + " ".join(warnings))
        return " ".join(parts)

    def _confidence_for_day(
        self,
        *,
        clock_in_source: str,
        clock_out_source: str,
        warnings: list[str],
        resolved: bool,
    ) -> str:
        if not resolved or clock_in_source == "unresolved":
            return "unresolved"
        if warnings:
            if clock_out_source.startswith("clamped_") or clock_in_source.startswith("explicit_"):
                return "low"
            return "medium"
        if clock_in_source.startswith("state_machine") or clock_in_source.startswith("explicit_"):
            if clock_out_source.startswith("state_machine") or clock_out_source.startswith("explicit_") or clock_out_source == "inbound_clock_out_intent":
                return "high"
        return "medium"

    def _first_candidate(self, candidates: list[TimeCandidate]) -> TimeCandidate | None:
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: item.at)[0]

    def _unique_times(self, candidates: list[TimeCandidate]) -> list[TimeCandidate]:
        by_key: dict[str, TimeCandidate] = {}
        for candidate in candidates:
            if not isinstance(candidate, TimeCandidate):
                continue
            key = candidate.at.isoformat()
            by_key.setdefault(key, candidate)
        return sorted(by_key.values(), key=lambda item: item.at)

    def _dedupe_datetimes(self, values: list[datetime]) -> list[datetime]:
        unique: dict[str, datetime] = {}
        for value in values:
            unique.setdefault(value.isoformat(), value)
        return sorted(unique.values())

    def _later_datetime(self, current: datetime | None, candidate: datetime | None) -> datetime | None:
        if candidate is None:
            return current
        if current is None or candidate > current:
            return candidate
        return current


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Retroactively backfill archived hours with durable provenance.")
    parser.add_argument("--apply", action="store_true", help="Rewrite archived sessions and rebuild the main time-tracking CSV.")
    parser.add_argument("--dry-run", action="store_true", help="Generate only the audit bundle. This is the default when no mode is provided.")
    parser.add_argument("--user", action="append", dest="users", default=[], help="Limit the backfill to one or more user keys or storage-folder names.")
    parser.add_argument("--from", dest="date_from", help="Inclusive YYYY-MM-DD lower bound.")
    parser.add_argument("--to", dest="date_to", help="Inclusive YYYY-MM-DD upper bound.")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("Use either --apply or --dry-run, not both.")
    if not args.apply:
        args.dry_run = True
    return args


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    load_dotenv()
    runtime = InternManagementRuntime()
    try:
        asyncio.run(runtime.refresh_configuration(force=True))
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    summary = RetroHoursBackfiller(runtime).run(
        apply=bool(args.apply),
        user_keys=set(args.users) if args.users else None,
        date_from=args.date_from,
        date_to=args.date_to,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

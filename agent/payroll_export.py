from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .models import SessionState, UserProfile
from .payroll_review import apply_review_state
from .runtime import InternManagementRuntime
from .time_utils import resolve_timezone


_PAYROLL_ROOT = Path("dashboard") / "payroll"


def _is_hourly_payroll_worker(user: UserProfile) -> bool:
    return user.compensation_plan == "cislune_hourly"


class PayrollExporter:
    def __init__(self, runtime: InternManagementRuntime) -> None:
        self.runtime = runtime
        self._task_classification_cache: dict[str, tuple[str, str, str]] = {}

    async def export(self, week_ending: date) -> dict[str, Any]:
        week_start = week_ending - timedelta(days=6)
        storage_root = self.runtime._storage_root_path()
        if storage_root is None:
            raise RuntimeError("Storage root is unavailable.")
        output_dir = storage_root / _PAYROLL_ROOT / week_ending.isoformat()
        output_dir.mkdir(parents=True, exist_ok=True)

        review_rows: list[dict[str, Any]] = []
        labor_rows: list[dict[str, Any]] = []
        compliance_rows: list[dict[str, Any]] = []
        gusto_rows: list[dict[str, Any]] = []
        pending_reports: list[dict[str, Any]] = []
        state_store = getattr(self.runtime, "state_store", None)
        if state_store:
            with state_store._connect() as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='slack_clock_reports'").fetchone():
                    # Reports contain actual-time claims in the worker's own
                    # words. Their work dates cannot be inferred from receipt
                    # dates; include all unresolved claims, even without shifts.
                    pending_reports = [dict(row) for row in conn.execute(
                        "SELECT id,user_key,reported_at,text,status FROM slack_clock_reports WHERE status='pending' ORDER BY reported_at"
                    )]
        for user in sorted(
            self.runtime.roster_by_key.values(),
            key=lambda item: item.display_name.lower(),
        ):
            for session_date in _date_range(week_start, week_ending):
                session_path = (
                    storage_root
                    / "people"
                    / (user.storage_folder_name or user.user_key)
                    / session_date.isoformat()
                    / "session.json"
                )
                # Beta clocks commit to SQLite first. An archive/file failure
                # must not silently exclude recorded hours from payroll review.
                state_store = getattr(self.runtime, "state_store", None)
                session = state_store.get_session(user.user_key, session_date.isoformat()) if state_store else SessionState(user_key=user.user_key, session_date=session_date.isoformat())
                if not session.metadata.get("slack_clock_beta") and not session.metadata.get("slack_clock_legacy_unresolved"):
                    if not session_path.exists():
                        continue
                    session = _load_session(session_path)
                if session is None:
                    continue
                reference_now = self.runtime._session_time_summary_reference_now(
                    session,
                    user,
                    datetime.combine(
                        session_date + timedelta(days=1),
                        datetime.min.time(),
                        tzinfo=resolve_timezone(self.runtime.resolve_user_timezone_name(user)),
                    ),
                )
                self.runtime._normalize_session_state(session, user=user)
                self.runtime._refresh_session_time_summary(session, reference_now)
                compliance_events = session.metadata.get("compliance_events")
                if (
                    int(session.time_summary.get("clocked_in_total_seconds") or 0) <= 0
                    and int(session.time_summary.get("task_tracked_total_seconds") or 0) <= 0
                    and not (isinstance(compliance_events, list) and compliance_events)
                ):
                    continue
                review = self._build_review_row(user, session, session_path)
                review_rows.append(review)
                labor_rows.extend(
                    await self._build_labor_rows(user, session, session_path)
                )
                compliance_rows.extend(
                    self._build_compliance_rows(user, session, session_path)
                )

        weekly_totals: dict[str, int] = defaultdict(int)
        for row in review_rows:
            weekly_totals[str(row["user_key"])] += int(row["paid_seconds"])
        for row in review_rows:
            weekly_hours = _hours(weekly_totals[str(row["user_key"])])
            row["weekly_tracked_hours"] = weekly_hours
            row["weekly_paid_hours"] = weekly_hours
            if weekly_totals[str(row["user_key"])] > 40 * 60 * 60:
                review_codes = _decode_review_codes(row.get("review_codes"))
                review_codes.append("weekly_overtime")
                row["review_codes"] = json.dumps(sorted(set(review_codes)))
                row["warnings"] = _join_warning(
                    str(row.get("warnings") or ""),
                    "Weekly tracked work exceeds 40 hours; overtime classification requires review.",
                )
                row["requires_review"] = True
        apply_review_state(
            review_rows,
            storage_root=storage_root,
            week_ending=week_ending.isoformat(),
        )
        review_by_day = {
            (str(row["user_key"]), str(row["session_date"])): row for row in review_rows
        }
        for user in self.runtime.roster_by_key.values():
            if not _is_hourly_payroll_worker(user) or not user.gusto_entity_uuid:
                continue
            for session_date in _date_range(week_start, week_ending):
                review = review_by_day.get((user.user_key, session_date.isoformat()))
                if review is not None:
                    gusto_rows.append(self._build_gusto_row(user, review))

        summary_rows = self._build_project_summary(labor_rows)
        _write_csv(output_dir / "payroll_review.csv", review_rows)
        _write_csv(output_dir / "project_labor.csv", labor_rows)
        _write_csv(output_dir / "nasa_project_labor.csv", labor_rows)
        _write_csv(output_dir / "project_summary.csv", summary_rows)
        _write_csv(output_dir / "compliance_events.csv", compliance_rows)
        _write_csv(output_dir / "unreconciled_time_reports.csv", pending_reports)
        (output_dir / "gusto_time_sheets.json").write_text(
            json.dumps(
                {
                    "approval_required": True,
                    "ready_for_submission": False,
                    "week_start": week_start.isoformat(),
                    "week_ending": week_ending.isoformat(),
                    "time_sheets": gusto_rows,
                    "unreconciled_time_reports": pending_reports,
                    "unmapped_workers": sorted(
                        {
                            str(row["display_name"])
                            for row in review_rows
                            if str(row.get("compensation_plan") or "") == "cislune_hourly"
                            if not str(row.get("gusto_entity_uuid") or "").strip()
                        }
                    ),
                    "excluded_non_hourly_workers": sorted(
                        {
                            str(row["display_name"])
                            for row in review_rows
                            if str(row.get("compensation_plan") or "") != "cislune_hourly"
                        }
                    ),
                    "note": (
                        "Only workers explicitly classified as cislune_hourly and mapped to Gusto "
                        "are included. Review and approve this bundle before entering or syncing "
                        "data to Gusto. Production API submission is intentionally disabled."
                    ),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        hourly_payroll_seconds = sum(
            int(row["hourly_payroll_seconds"]) for row in review_rows
        )
        stipend_effort_seconds = sum(
            int(row["paid_seconds"])
            for row in review_rows
            if str(row.get("compensation_plan") or "") == "nasa_stipend"
        )
        unclassified_seconds = sum(
            int(row["paid_seconds"])
            for row in review_rows
            if str(row.get("compensation_plan") or "") == "needs_review"
        )
        summary = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "week_start": week_start.isoformat(),
            "week_ending": week_ending.isoformat(),
            "worker_days": len(review_rows),
            "workers": len({str(row["user_key"]) for row in review_rows}),
            "tracked_hours": _hours(
                sum(int(row["paid_seconds"]) for row in review_rows)
            ),
            "hourly_payroll_hours": _hours(hourly_payroll_seconds),
            "stipend_effort_hours": _hours(stipend_effort_seconds),
            "unclassified_hours": _hours(unclassified_seconds),
            "paid_hours": _hours(hourly_payroll_seconds),
            "task_hours": _hours(sum(int(row["seconds"]) for row in labor_rows)),
            "requires_review_days": sum(bool(row.get("requires_review")) for row in review_rows),
            "missing_gusto_mappings": len(
                {
                    str(row["user_key"])
                    for row in review_rows
                    if str(row.get("compensation_plan") or "") == "cislune_hourly"
                    if not str(row.get("gusto_entity_uuid") or "").strip()
                }
            ),
            "compliance_events": len(compliance_rows),
            "unreconciled_time_reports": len(pending_reports),
            "output_dir": str(output_dir.resolve()),
        }
        (output_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        latest_dir = storage_root / _PAYROLL_ROOT / "latest"
        if latest_dir.exists():
            shutil.rmtree(latest_dir)
        shutil.copytree(output_dir, latest_dir)
        return summary

    def _build_review_row(
        self,
        user: UserProfile,
        session: SessionState,
        session_path: Path,
    ) -> dict[str, Any]:
        paid_seconds = int(session.time_summary.get("clocked_in_total_seconds") or 0)
        gross_seconds = int(session.time_summary.get("gross_clocked_in_total_seconds") or 0)
        meal_seconds = int(session.time_summary.get("unpaid_lunch_deducted_seconds") or 0)
        task_seconds = int(session.time_summary.get("task_tracked_total_seconds") or 0)
        hourly_payroll_seconds = paid_seconds if _is_hourly_payroll_worker(user) else 0
        regular_seconds = min(paid_seconds, 8 * 60 * 60)
        overtime_seconds = min(max(0, paid_seconds - regular_seconds), 4 * 60 * 60)
        double_overtime_seconds = max(0, paid_seconds - regular_seconds - overtime_seconds)
        starts, ends = _segment_boundaries(session)
        warnings: list[str] = []
        review_codes: list[str] = []
        integration_notes: list[str] = []
        if session.metadata.get("slack_clock_legacy_unresolved"):
            warnings.append("Historical shift has no confirmed end. Totals are not settled; reconcile the preserved original and actual-hours report.")
            review_codes.append("unresolved_historical_shift")
        if user.compensation_plan == "needs_review":
            warnings.append(
                "Compensation plan is unclassified; choose Cislune hourly, NASA stipend, salary, or external."
            )
            review_codes.append("compensation_plan_unclassified")
        elif _is_hourly_payroll_worker(user) and not user.gusto_entity_uuid:
            integration_notes.append("Not mapped to Gusto; included in parallel local reporting only.")
        elif user.compensation_plan == "nasa_stipend":
            integration_notes.append(
                "NASA stipend effort: excluded from Cislune hourly payroll and retained in project labor reporting."
            )
        if paid_seconds and not starts:
            warnings.append("Paid time exists without a complete work segment.")
            review_codes.append("incomplete_work_segment")
        lunch_started_at = str(session.metadata.get("lunch_started_at") or "").strip()
        lunch_ended_at = str(session.metadata.get("lunch_ended_at") or "").strip()
        if lunch_started_at and not lunch_ended_at:
            warnings.append("Lunch was started but no return time was recorded.")
            review_codes.append("missing_lunch_return")
        elif meal_seconds > 90 * 60:
            warnings.append("Recorded lunch exceeds 90 minutes; verify the return time.")
            review_codes.append("long_lunch")
        if abs(paid_seconds - task_seconds) > 15 * 60 and not session.metadata.get("slack_clock_beta"):
            warnings.append("Paid time and task-tracked time differ by more than 15 minutes.")
            review_codes.append("task_time_variance")
        if session.metadata.get("slack_clock_meal_started_at"):
            warnings.append("Reported meal has no confirmed return; reconcile actual time.")
            review_codes.append("missing_lunch_return")
        if session.metadata.get("slack_clock_beta") and starts and ends and any(
            start.astimezone(resolve_timezone(self.runtime.config.timezone)).date() != end.astimezone(resolve_timezone(self.runtime.config.timezone)).date()
            for start, end in zip(starts, ends)
        ):
            warnings.append("Shift spans midnight; split by the configured workday before final overtime classification.")
            review_codes.append("cross_midnight_classification")
        compliance_events = session.metadata.get("compliance_events")
        if isinstance(compliance_events, list) and compliance_events:
            warnings.append(f"{len(compliance_events)} compliance event(s) require review.")
            review_codes.append("compliance_event")
        return {
            "user_key": user.user_key,
            "display_name": user.display_name,
            "worker_type": user.worker_type,
            "compensation_plan": user.compensation_plan,
            "session_date": session.session_date,
            "timezone": self.runtime.resolve_user_timezone_name(user),
            "shift_started_at": min(starts).astimezone(timezone.utc).isoformat() if starts else "",
            "shift_ended_at": max(ends).astimezone(timezone.utc).isoformat() if ends else "",
            "gross_seconds": gross_seconds,
            "gross_hours": _hours(gross_seconds),
            "unpaid_meal_seconds": meal_seconds,
            "unpaid_meal_hours": _hours(meal_seconds),
            "paid_seconds": paid_seconds,
            "paid_hours": _hours(paid_seconds),
            "tracked_seconds": paid_seconds,
            "tracked_hours": _hours(paid_seconds),
            "hourly_payroll_seconds": hourly_payroll_seconds,
            "hourly_payroll_hours": _hours(hourly_payroll_seconds),
            "task_tracked_seconds": task_seconds,
            "task_tracked_hours": _hours(task_seconds),
            "regular_hours": _hours(regular_seconds),
            "overtime_hours": _hours(overtime_seconds),
            "double_overtime_hours": _hours(double_overtime_seconds),
            "weekly_paid_hours": 0.0,
            "gusto_entity_uuid": user.gusto_entity_uuid or "",
            "requires_review": bool(review_codes),
            "review_codes": json.dumps(sorted(set(review_codes))),
            "warnings": " | ".join(warnings),
            "integration_notes": " | ".join(integration_notes),
            "session_path": str(session_path.resolve()),
        }

    def _build_gusto_row(
        self,
        user: UserProfile,
        review: dict[str, Any],
    ) -> dict[str, Any]:
        entries = []
        for label, key in (
            ("Regular", "regular_hours"),
            ("Overtime", "overtime_hours"),
            ("Double overtime", "double_overtime_hours"),
        ):
            hours = float(review[key])
            if hours > 0:
                entries.append({"hours_worked": hours, "pay_classification": label})
        return {
            "entity_uuid": user.gusto_entity_uuid,
            "entity_type": "Employee",
            "time_zone": self.runtime.resolve_user_timezone_name(user),
            "shift_started_at": review["shift_started_at"],
            "shift_ended_at": review["shift_ended_at"],
            "entries": entries,
            "metadata": {
                "user_key": user.user_key,
                "session_date": str(review["session_date"]),
                "source_session_path": review["session_path"],
                "requires_review": review["requires_review"],
            },
        }

    async def _build_labor_rows(
        self,
        user: UserProfile,
        session: SessionState,
        session_path: Path,
    ) -> list[dict[str, Any]]:
        raw_time_by_task = session.time_summary.get("time_by_task")
        task_rows = raw_time_by_task if isinstance(raw_time_by_task, list) else []
        rows: list[dict[str, Any]] = []
        task_total = 0
        for task_row in task_rows:
            if not isinstance(task_row, dict):
                continue
            seconds = max(0, int(task_row.get("seconds") or 0))
            if seconds <= 0:
                continue
            task_total += seconds
            task_id = str(task_row.get("task_id") or "")
            task_name = str(task_row.get("task_name") or "")
            project, labor_code, classification = await self._classify_task(
                user,
                task_id,
                task_name,
            )
            rows.append(
                self._labor_row(
                    user,
                    session,
                    session_path,
                    task_id=task_id,
                    task_name=task_name,
                    project=project,
                    labor_code=labor_code,
                    classification=classification,
                    seconds=seconds,
                )
            )
        paid_seconds = int(session.time_summary.get("clocked_in_total_seconds") or 0)
        unallocated_seconds = max(0, paid_seconds - task_total)
        if unallocated_seconds > 0:
            rows.append(
                self._labor_row(
                    user,
                    session,
                    session_path,
                    task_id="",
                    task_name="Unallocated paid time",
                    project="Overhead / Unmapped",
                    labor_code="OVERHEAD_REVIEW",
                    classification="unallocated",
                    seconds=unallocated_seconds,
                )
            )
        return rows

    def _labor_row(
        self,
        user: UserProfile,
        session: SessionState,
        session_path: Path,
        *,
        task_id: str,
        task_name: str,
        project: str,
        labor_code: str,
        classification: str,
        seconds: int,
    ) -> dict[str, Any]:
        rate = user.labor_cost_rate
        return {
            "user_key": user.user_key,
            "display_name": user.display_name,
            "worker_type": user.worker_type,
            "compensation_plan": user.compensation_plan,
            "session_date": session.session_date,
            "task_id": task_id,
            "task_name": task_name,
            "project": project,
            "labor_code": labor_code,
            "classification": classification,
            "seconds": seconds,
            "hours": _hours(seconds),
            "labor_cost_rate": "" if rate is None else round(rate, 2),
            "estimated_labor_cost": "" if rate is None else round(_hours(seconds) * rate, 2),
            "source": "session.time_summary.time_by_task",
            "session_path": str(session_path.resolve()),
        }

    async def _classify_task(
        self,
        user: UserProfile,
        task_id: str,
        task_name: str,
    ) -> tuple[str, str, str]:
        cache_key = task_id or task_name.lower()
        cached = self._task_classification_cache.get(cache_key)
        if cached is not None:
            return cached
        if task_id in self.runtime.config.labor.overhead_task_ids or any(
            re.search(pattern, task_name, flags=re.IGNORECASE)
            for pattern in self.runtime.config.labor.overhead_name_patterns
        ):
            result = ("Overhead", "OVERHEAD", "overhead")
            self._task_classification_cache[cache_key] = result
            return result
        session = SessionState(
            user_key=user.user_key,
            session_date=date.today().isoformat(),
            metadata={
                "active_clickup_task_id": task_id,
                "active_clickup_task_name": task_name,
            },
        )
        _channel_id, label, uncertain = await self.runtime._resolve_slack_daily_channel(
            user,
            session,
        )
        if uncertain or label in {"default", "mapping needed"}:
            result = ("Unmapped", "UNMAPPED", "unmapped")
        else:
            route = next(
                (
                    item
                    for item in self.runtime.config.slack.project_routes
                    if item.label == label
                ),
                None,
            )
            result = (
                label,
                route.labor_code if route and route.labor_code else _labor_code(label),
                "project",
            )
        self._task_classification_cache[cache_key] = result
        return result

    def _build_compliance_rows(
        self,
        user: UserProfile,
        session: SessionState,
        session_path: Path,
    ) -> list[dict[str, Any]]:
        events = session.metadata.get("compliance_events")
        events = events if isinstance(events, list) else []
        return [
            {
                "user_key": user.user_key,
                "display_name": user.display_name,
                "worker_type": user.worker_type,
                "compensation_plan": user.compensation_plan,
                "session_date": session.session_date,
                "event_type": str(event.get("event_type") or event.get("type") or ""),
                "recorded_at": str(event.get("recorded_at") or event.get("at") or ""),
                "confirmation": str(event.get("confirmation") or ""),
                "worked_seconds": int(event.get("worked_seconds") or 0),
                "worked_hours": _hours(int(event.get("worked_seconds") or 0)),
                "session_path": str(session_path.resolve()),
            }
            for event in events
            if isinstance(event, dict)
        ]

    def _build_project_summary(
        self,
        labor_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in labor_rows:
            key = (
                str(row["project"]),
                str(row["labor_code"]),
                str(row["classification"]),
            )
            bucket = grouped.setdefault(
                key,
                {
                    "project": key[0],
                    "labor_code": key[1],
                    "classification": key[2],
                    "seconds": 0,
                    "estimated_labor_cost": 0.0,
                    "cost_rate_coverage": True,
                },
            )
            bucket["seconds"] += int(row["seconds"])
            if row["estimated_labor_cost"] == "":
                bucket["cost_rate_coverage"] = False
            else:
                bucket["estimated_labor_cost"] += float(row["estimated_labor_cost"])
        results = []
        for bucket in grouped.values():
            route = next(
                (
                    item
                    for item in self.runtime.config.slack.project_routes
                    if item.label == bucket["project"]
                ),
                None,
            )
            hours = _hours(int(bucket["seconds"]))
            budget_hours = route.budget_hours if route else None
            results.append(
                {
                    "project": bucket["project"],
                    "labor_code": bucket["labor_code"],
                    "classification": bucket["classification"],
                    "hours": hours,
                    "budget_hours": "" if budget_hours is None else budget_hours,
                    "remaining_budget_hours": (
                        "" if budget_hours is None else round(budget_hours - hours, 2)
                    ),
                    "estimated_labor_cost": (
                        round(float(bucket["estimated_labor_cost"]), 2)
                        if bucket["cost_rate_coverage"]
                        else ""
                    ),
                    "cost_rate_coverage": bool(bucket["cost_rate_coverage"]),
                }
            )
        return sorted(results, key=lambda row: str(row["project"]).lower())


def _load_session(path: Path) -> SessionState | None:
    try:
        return SessionState(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _segment_boundaries(session: SessionState) -> tuple[list[datetime], list[datetime]]:
    starts: list[datetime] = []
    ends: list[datetime] = []
    for segment in session.work_segments:
        if not isinstance(segment, dict):
            continue
        try:
            started_at = datetime.fromisoformat(
                str(segment.get("clocked_in_at") or segment.get("started_at") or "")
            )
            ended_at = datetime.fromisoformat(
                str(segment.get("clocked_out_at") or segment.get("ended_at") or "")
            )
        except ValueError:
            continue
        if started_at.tzinfo is None or ended_at.tzinfo is None:
            continue
        if ended_at > started_at:
            starts.append(started_at)
            ends.append(ended_at)
    return starts, ends


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    if not fieldnames:
        fieldnames = ["empty"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _hours(seconds: int) -> float:
    return round(seconds / 3600, 4)


def _decode_review_codes(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(decoded, list):
            return [str(item) for item in decoded if str(item)]
    return []


def _labor_code(label: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", label.upper()).strip("_") or "UNMAPPED"


def _join_warning(current: str, warning: str) -> str:
    return f"{current} | {warning}" if current else warning


def _default_week_ending(today: date) -> date:
    days_since_sunday = (today.weekday() + 1) % 7
    most_recent_sunday = today - timedelta(days=days_since_sunday)
    return today - timedelta(days=7) if today.weekday() == 6 else most_recent_sunday


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an approval-first Monday payroll and project-labor bundle."
    )
    parser.add_argument(
        "--week-ending",
        help="Week-ending date in YYYY-MM-DD. Defaults to the prior completed Sunday.",
    )
    return parser.parse_args(argv)


async def _run(argv: list[str] | None = None) -> dict[str, Any]:
    args = _parse_args(argv)
    load_dotenv()
    runtime = InternManagementRuntime()
    await runtime.refresh_configuration(force=True)
    today = datetime.now(tz=resolve_timezone(runtime.runtime_timezone_name())).date()
    week_ending = (
        datetime.fromisoformat(args.week_ending).date()
        if args.week_ending
        else _default_week_ending(today)
    )
    return await PayrollExporter(runtime).export(week_ending)


def main(argv: list[str] | None = None) -> None:
    try:
        summary = asyncio.run(_run(argv))
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_REVIEW_LOG = Path("dashboard") / "payroll" / "review_resolutions.jsonl"


def review_fingerprint(row: dict[str, Any]) -> str:
    evidence = {
        "review_codes": sorted(_review_codes(row)),
        "paid_seconds": int(row.get("paid_seconds") or 0),
        "task_tracked_seconds": int(row.get("task_tracked_seconds") or 0),
        "shift_started_at": str(row.get("shift_started_at") or ""),
        "shift_ended_at": str(row.get("shift_ended_at") or ""),
    }
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_review_state(storage_root: Path) -> tuple[dict[tuple[str, str, str], dict[str, Any]], dict[str, int]]:
    resolutions: dict[tuple[str, str, str], dict[str, Any]] = {}
    learned_variances: dict[str, int] = {}
    path = storage_root / _REVIEW_LOG
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return resolutions, learned_variances
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        action = str(record.get("action") or "")
        if action in {"resolve_day", "reopen_day"}:
            key = (
                str(record.get("week_ending") or ""),
                str(record.get("user_key") or ""),
                str(record.get("session_date") or ""),
            )
            resolutions[key] = record
        elif action == "learn_task_variance":
            user_key = str(record.get("user_key") or "")
            maximum = max(0, int(record.get("max_abs_variance_seconds") or 0))
            if user_key:
                learned_variances[user_key] = max(learned_variances.get(user_key, 0), maximum)
    return resolutions, learned_variances


def apply_review_state(
    rows: list[dict[str, Any]],
    *,
    storage_root: Path,
    week_ending: str,
) -> None:
    resolutions, learned_variances = load_review_state(storage_root)
    for row in rows:
        codes = _review_codes(row)
        row["review_fingerprint"] = review_fingerprint(row)
        row["review_status"] = "clear" if not codes else "needs_review"
        row["resolved_by"] = ""
        row["resolution_note"] = ""
        row["resolved_at"] = ""
        if not codes:
            row["requires_review"] = False
            continue

        key = (week_ending, str(row.get("user_key") or ""), str(row.get("session_date") or ""))
        resolution = resolutions.get(key)
        if (
            resolution
            and resolution.get("action") == "resolve_day"
            and str(resolution.get("review_fingerprint") or "") == row["review_fingerprint"]
        ):
            row["requires_review"] = False
            row["review_status"] = "resolved"
            row["resolved_by"] = str(resolution.get("resolved_by") or "")
            row["resolution_note"] = str(resolution.get("note") or "")
            row["resolved_at"] = str(resolution.get("recorded_at") or "")
            continue

        if codes == ["task_time_variance"]:
            maximum = learned_variances.get(str(row.get("user_key") or ""), 0)
            variance = abs(
                int(row.get("paid_seconds") or 0)
                - int(row.get("task_tracked_seconds") or 0)
            )
            if maximum and variance <= maximum:
                row["requires_review"] = False
                row["review_status"] = "auto_resolved"
                row["resolution_note"] = (
                    "Matched a manager-approved task-time variance rule for this worker."
                )
                continue
        row["requires_review"] = True


def record_review_resolution(
    storage_root: Path,
    *,
    week_ending: str,
    row: dict[str, Any],
    resolved_by: str,
    note: str,
    remember_similar: bool,
) -> dict[str, Any]:
    resolved_by = resolved_by.strip()
    note = note.strip()
    if not resolved_by:
        raise ValueError("Resolved by is required.")
    if not note:
        raise ValueError("A review note is required.")
    codes = _review_codes(row)
    if not codes:
        raise ValueError("This day no longer has a review reason.")
    if remember_similar and codes != ["task_time_variance"]:
        raise ValueError(
            "Learning is only available when task-time variance is the sole review reason."
        )
    fingerprint = str(row.get("review_fingerprint") or review_fingerprint(row))
    recorded_at = datetime.now(tz=timezone.utc).isoformat()
    record = {
        "action": "resolve_day",
        "recorded_at": recorded_at,
        "week_ending": week_ending,
        "user_key": str(row.get("user_key") or ""),
        "session_date": str(row.get("session_date") or ""),
        "review_codes": codes,
        "review_fingerprint": fingerprint,
        "resolved_by": resolved_by,
        "note": note,
        "remember_similar": bool(remember_similar),
    }
    _append_record(storage_root, record)
    if remember_similar:
        variance = abs(
            int(row.get("paid_seconds") or 0)
            - int(row.get("task_tracked_seconds") or 0)
        )
        _append_record(
            storage_root,
            {
                "action": "learn_task_variance",
                "recorded_at": recorded_at,
                "user_key": str(row.get("user_key") or ""),
                "max_abs_variance_seconds": variance,
                "approved_by": resolved_by,
                "source_week_ending": week_ending,
                "source_session_date": str(row.get("session_date") or ""),
                "note": note,
            },
        )
    return record


def _append_record(storage_root: Path, record: dict[str, Any]) -> None:
    path = storage_root / _REVIEW_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _review_codes(row: dict[str, Any]) -> list[str]:
    raw = row.get("review_codes")
    if isinstance(raw, list):
        return sorted({str(item) for item in raw if str(item)})
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = [item.strip() for item in raw.split(",")]
        if isinstance(decoded, list):
            return sorted({str(item) for item in decoded if str(item)})
    return []

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VALID_PLANS = {
    "cislune_hourly",
    "nasa_stipend",
    "salary",
    "external",
    "needs_review",
}


def _canonical_roster_keys(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    canonical: dict[str, str] = {}
    for row in rows:
        if str(row.get("active", "true")).strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            continue
        user_key = str(row.get("user_key") or "").strip()
        if not user_key:
            continue
        lookup = user_key.casefold()
        if lookup in canonical and canonical[lookup] != user_key:
            raise ValueError(
                f"Roster has ambiguous user keys: {canonical[lookup]} and {user_key}."
            )
        canonical[lookup] = user_key
    return canonical


def resolve_reviewed_batch(
    *,
    roster_path: Path,
    hourly_users: list[str],
    stipend_users: list[str],
) -> list[tuple[str, str, str]]:
    canonical = _canonical_roster_keys(roster_path)
    requested: list[tuple[str, str, str]] = [
        (user, "cislune_hourly", "owner-confirmed-hourly")
        for user in hourly_users
    ] + [
        (user, "nasa_stipend", "owner-confirmed-stipend-intern")
        for user in stipend_users
    ]
    resolved: list[tuple[str, str, str]] = []
    plans_by_key: dict[str, str] = {}
    for requested_key, plan, evidence in requested:
        normalized = requested_key.strip().casefold()
        user_key = canonical.get(normalized)
        if not user_key and len(normalized) >= 3:
            matches = [
                candidate
                for lookup, candidate in canonical.items()
                if lookup.startswith(normalized)
            ]
            if len(matches) == 1:
                user_key = matches[0]
            elif len(matches) > 1:
                raise ValueError(
                    f"Ambiguous active roster prefix {requested_key}: "
                    + ", ".join(sorted(matches))
                )
        if not user_key:
            raise ValueError(f"Unknown active roster user: {requested_key}")
        previous_plan = plans_by_key.get(user_key)
        if previous_plan and previous_plan != plan:
            raise ValueError(f"Conflicting compensation plans requested for {user_key}.")
        if previous_plan:
            continue
        plans_by_key[user_key] = plan
        resolved.append((user_key, plan, evidence))
    return resolved


def _load_overrides(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return {
        str(key): value
        for key, value in payload.items()
        if isinstance(value, dict)
    }


def record_classification(
    overrides: dict[str, dict[str, Any]],
    *,
    user_key: str,
    slack_user_id: str,
    plan: str,
    evidence: str,
    reviewed_by: str,
    reviewed_at: str,
) -> tuple[str, dict[str, dict[str, Any]]]:
    normalized_plan = plan.strip().lower().replace("-", "_")
    if normalized_plan not in VALID_PLANS:
        choices = ", ".join(sorted(plan.replace("_", "-") for plan in VALID_PLANS))
        raise ValueError(f"Unknown compensation plan. Choose one of: {choices}")
    normalized_slack_id = slack_user_id.strip().upper()
    normalized_user_key = user_key.strip()
    if not normalized_slack_id and not normalized_user_key:
        raise ValueError("Provide --slack-id or --user.")
    storage_key = normalized_slack_id or normalized_user_key
    existing = dict(overrides.get(storage_key, {}))
    existing.update(
        {
            "compensation_plan": normalized_plan,
            "compensation_evidence": evidence.strip(),
            "compensation_reviewed_at": reviewed_at,
            "compensation_reviewed_by": reviewed_by.strip(),
        }
    )
    if normalized_user_key:
        existing["roster_user_key"] = normalized_user_key
    overrides[storage_key] = existing
    return storage_key, overrides


def _write_overrides(path: Path, payload: dict[str, dict[str, Any]]) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    original_mode = 0o600
    if path.exists():
        timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
        backup_path = path.with_name(
            f"{path.stem}.before-compensation-review.{timestamp}{path.suffix}"
        )
        shutil.copy2(path, backup_path)
        original_mode = stat.S_IMODE(path.stat().st_mode)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, original_mode)
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record a reviewed compensation classification without editing the roster by hand."
    )
    parser.add_argument("--user", default="", help="Roster user key.")
    parser.add_argument("--slack-id", default="", help="Slack user ID.")
    parser.add_argument("--plan", default="")
    parser.add_argument("--evidence", default="", help="Short evidence label, not message text.")
    parser.add_argument("--reviewed-by", required=True)
    parser.add_argument(
        "--hourly",
        action="append",
        default=[],
        metavar="USER",
        help="Case-insensitive roster user key confirmed as Cislune hourly; repeat as needed.",
    )
    parser.add_argument(
        "--stipend",
        action="append",
        default=[],
        metavar="USER",
        help="Case-insensitive roster user key confirmed as a NASA stipend intern; repeat as needed.",
    )
    parser.add_argument("--roster", type=Path, default=Path("config/roster.csv"))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply all high-confidence classifications to the roster and refresh the review queue.",
    )
    parser.add_argument(
        "--overrides",
        type=Path,
        default=Path("storage/dashboard/payroll/workforce_identity_overrides.json"),
    )
    args = parser.parse_args()

    overrides = _load_overrides(args.overrides)
    reviewed_at = datetime.now(timezone.utc).isoformat()
    batch_requested = bool(args.hourly or args.stipend)
    if batch_requested and any((args.user, args.slack_id, args.plan, args.evidence)):
        parser.error("Do not combine --hourly/--stipend with single-record options.")
    if not batch_requested and (not args.plan or not args.evidence):
        parser.error("Single-record mode requires --plan and --evidence.")

    records = (
        resolve_reviewed_batch(
            roster_path=args.roster,
            hourly_users=args.hourly,
            stipend_users=args.stipend,
        )
        if batch_requested
        else [(args.user, args.plan, args.evidence)]
    )
    storage_keys: list[str] = []
    for user_key, plan, evidence in records:
        storage_key, overrides = record_classification(
            overrides,
            user_key=user_key,
            slack_user_id=args.slack_id if not batch_requested else "",
            plan=plan,
            evidence=evidence,
            reviewed_by=args.reviewed_by,
            reviewed_at=reviewed_at,
        )
        storage_keys.append(storage_key)
    backup_path = _write_overrides(args.overrides, overrides)
    print(
        "Recorded reviewed compensation classification for "
        + ", ".join(storage_keys)
        + "."
    )
    if backup_path:
        print(f"Previous overrides: {backup_path}")
    if args.apply:
        import runpy

        inference = runpy.run_path(
            str(Path(__file__).with_name("infer_compensation_plans.py")),
            run_name="compensation_inference",
        )

        candidates_path = Path(
            "storage/dashboard/payroll/workforce_identity_candidates.csv"
        )
        review_path = Path(
            "storage/dashboard/payroll/compensation_classification_review.csv"
        )
        fieldnames, roster_rows = inference["_load_csv"](args.roster)
        _candidate_fields, candidate_rows = inference["_load_csv"](candidates_path)
        proposals = inference["build_proposals"](
            roster_rows, candidate_rows, overrides
        )
        review_rows = inference["uncertain_proposals"](proposals)
        inference["_write_csv"](review_path, review_rows)
        inference["_apply_confident"](
            args.roster, fieldnames, roster_rows, proposals
        )
        print(f"Compensation review queue now contains {len(review_rows)} user(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


VALID_PLANS = {
    "cislune_hourly",
    "nasa_stipend",
    "salary",
    "external",
    "needs_review",
}


def _load_csv(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    if not path.exists():
        return [], []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def _load_overrides(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): value
        for key, value in payload.items()
        if isinstance(value, dict)
    }


def _candidate_by_roster_key(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("roster_user_key") or "").strip(): row
        for row in rows
        if str(row.get("roster_user_key") or "").strip()
    }


def _override_for(
    row: dict[str, Any],
    candidate: dict[str, Any],
    overrides: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    for key in (
        str(row.get("user_key") or "").strip(),
        str(row.get("slack_user_id") or "").strip(),
        str(candidate.get("slack_user_id") or "").strip(),
    ):
        if key and key in overrides:
            return overrides[key]
    return {}


def infer_plan(
    row: dict[str, Any],
    candidate: dict[str, Any],
    override: dict[str, Any],
) -> tuple[str, str, str]:
    current = str(row.get("compensation_plan") or "needs_review").strip().lower()
    if current in VALID_PLANS and current != "needs_review":
        return current, "high", "explicit roster classification"

    override_plan = str(override.get("compensation_plan") or "").strip().lower()
    if override_plan in VALID_PLANS and override_plan != "needs_review":
        return override_plan, "high", "operator workforce override"

    worker_type = str(
        override.get("worker_type")
        or row.get("worker_type")
        or candidate.get("worker_type")
        or "intern"
    ).strip().lower()
    if worker_type in {"salaried", "exempt"}:
        return "salary", "high", f"worker_type={worker_type}"
    if worker_type in {"external", "contractor"}:
        return "external", "high", f"worker_type={worker_type}"
    if str(row.get("gusto_entity_uuid") or "").strip():
        return "cislune_hourly", "high", "existing Gusto employee mapping"

    funding = " ".join(
        str(override.get(key) or "")
        for key in ("funding_source", "payment_type", "program", "notes")
    ).lower()
    title = str(candidate.get("slack_title") or "").lower()
    combined = f"{funding} {title}"
    if "nasa" in combined and "stipend" in combined:
        return "nasa_stipend", "medium", "NASA stipend wording in workforce evidence"
    if any(term in combined for term in ("space grant stipend", "stipend intern")):
        return "nasa_stipend", "medium", "stipend wording in workforce evidence"
    if "hourly" in combined and "cislune" in combined:
        return "cislune_hourly", "medium", "Cislune hourly wording in workforce evidence"
    return "needs_review", "low", "no payroll-grade funding or employment evidence"


def build_proposals(
    roster_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates = _candidate_by_roster_key(candidate_rows)
    proposals: list[dict[str, Any]] = []
    for row in roster_rows:
        if str(row.get("active", "true")).strip().lower() in {"0", "false", "no", "off"}:
            continue
        user_key = str(row.get("user_key") or "").strip()
        candidate = candidates.get(user_key, {})
        override = _override_for(row, candidate, overrides)
        proposed, confidence, evidence = infer_plan(row, candidate, override)
        current = str(row.get("compensation_plan") or "needs_review").strip().lower()
        proposals.append(
            {
                "user_key": user_key,
                "current_plan": current,
                "proposed_plan": proposed,
                "confidence": confidence,
                "evidence": evidence,
                "requires_confirmation": str(
                    proposed == "needs_review" or confidence != "high"
                ).lower(),
            }
        )
    return proposals


def uncertain_proposals(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        proposal
        for proposal in proposals
        if str(proposal.get("requires_confirmation") or "").lower() == "true"
    ]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else ["user_key"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _apply_confident(
    roster_path: Path,
    fieldnames: list[str],
    roster_rows: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
) -> int:
    proposed_by_key = {str(row["user_key"]): row for row in proposals}
    changed = 0
    if "compensation_plan" not in fieldnames:
        fieldnames.append("compensation_plan")
    for row in roster_rows:
        proposal = proposed_by_key.get(str(row.get("user_key") or ""))
        if not proposal or proposal["confidence"] != "high":
            continue
        proposed = str(proposal["proposed_plan"])
        if proposed == "needs_review" or str(row.get("compensation_plan") or "") == proposed:
            continue
        row["compensation_plan"] = proposed
        changed += 1
    if not changed:
        return 0

    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    backup_path = roster_path.with_name(
        f"{roster_path.stem}.before-compensation-inference.{timestamp}{roster_path.suffix}"
    )
    shutil.copy2(roster_path, backup_path)
    original_mode = stat.S_IMODE(roster_path.stat().st_mode)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{roster_path.name}.",
        suffix=".tmp",
        dir=roster_path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(roster_rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, original_mode)
        os.replace(temporary_name, roster_path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()
    print(f"Applied {changed} high-confidence classification(s). Previous roster: {backup_path}")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Infer payroll-safe compensation classifications and isolate uncertain cases."
    )
    parser.add_argument("--roster", type=Path, default=Path("config/roster.csv"))
    parser.add_argument(
        "--candidates",
        type=Path,
        default=Path("storage/dashboard/payroll/workforce_identity_candidates.csv"),
    )
    parser.add_argument(
        "--overrides",
        type=Path,
        default=Path("storage/dashboard/payroll/workforce_identity_overrides.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("storage/dashboard/payroll/compensation_classification_review.csv"),
    )
    parser.add_argument("--apply-confident", action="store_true")
    args = parser.parse_args()

    fieldnames, roster_rows = _load_csv(args.roster)
    _candidate_fields, candidate_rows = _load_csv(args.candidates)
    overrides = _load_overrides(args.overrides)
    proposals = build_proposals(roster_rows, candidate_rows, overrides)
    review_rows = uncertain_proposals(proposals)
    _write_csv(args.output, review_rows)
    print(
        f"Wrote {len(review_rows)} uncertain classification(s) "
        f"to {args.output.resolve()}."
    )
    print(f"Reviewed {len(proposals) - len(review_rows)} classification(s) are omitted.")
    if args.apply_confident:
        _apply_confident(args.roster, fieldnames, roster_rows, proposals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

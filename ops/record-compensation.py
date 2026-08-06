from __future__ import annotations

import argparse
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
    parser.add_argument("--plan", required=True)
    parser.add_argument("--evidence", required=True, help="Short evidence label, not message text.")
    parser.add_argument("--reviewed-by", required=True)
    parser.add_argument(
        "--overrides",
        type=Path,
        default=Path("storage/dashboard/payroll/workforce_identity_overrides.json"),
    )
    args = parser.parse_args()

    overrides = _load_overrides(args.overrides)
    reviewed_at = datetime.now(timezone.utc).isoformat()
    storage_key, overrides = record_classification(
        overrides,
        user_key=args.user,
        slack_user_id=args.slack_id,
        plan=args.plan,
        evidence=args.evidence,
        reviewed_by=args.reviewed_by,
        reviewed_at=reviewed_at,
    )
    backup_path = _write_overrides(args.overrides, overrides)
    print(f"Recorded reviewed compensation classification for {storage_key}.")
    if backup_path:
        print(f"Previous overrides: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

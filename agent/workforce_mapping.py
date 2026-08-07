from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .runtime import InternManagementRuntime


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


async def build_workforce_mapping(runtime: InternManagementRuntime) -> list[dict[str, Any]]:
    if runtime.slack is None or runtime.clickup is None:
        raise RuntimeError("Both Slack and ClickUp credentials are required for workforce mapping.")
    slack_users = await runtime.slack.list_users()
    clickup_users = await runtime.clickup.list_workspace_members()
    roster_slack_ids = {
        str(user.slack_user_id): user
        for user in runtime.roster_by_key.values()
        if user.slack_user_id
    }
    roster_clickup_ids = {
        str(user.clickup_user_id): user
        for user in runtime.roster_by_key.values()
        if user.clickup_user_id
    }
    admin_clickup_ids = {
        str(admin.clickup_user_id)
        for admin in runtime.config.admins
        if admin.clickup_user_id
    }
    overrides = _load_identity_overrides(runtime)
    clickup_by_email = {
        str(member.get("email") or "").strip().lower(): member
        for member in clickup_users
        if str(member.get("email") or "").strip()
    }
    clickup_by_id = {
        str(member.get("id")): member
        for member in clickup_users
        if member.get("id") is not None
    }
    clickup_by_name: dict[str, list[dict[str, Any]]] = {}
    for member in clickup_users:
        name = _normalized(
            str(member.get("username") or member.get("name") or "")
        )
        if name:
            clickup_by_name.setdefault(name, []).append(member)

    rows: list[dict[str, Any]] = []
    for slack_member in slack_users:
        if slack_member.get("deleted") or slack_member.get("is_bot"):
            continue
        slack_id = str(slack_member.get("id") or "")
        if not slack_id or slack_id == "USLACKBOT":
            continue
        profile = slack_member.get("profile")
        profile = profile if isinstance(profile, dict) else {}
        slack_name = str(
            profile.get("real_name_normalized")
            or profile.get("real_name")
            or slack_member.get("real_name")
            or slack_member.get("name")
            or ""
        )
        slack_email = str(profile.get("email") or "").strip()
        title = str(profile.get("title") or "").strip()
        roster_user = roster_slack_ids.get(slack_id)
        clickup_member = clickup_by_email.get(slack_email.lower()) if slack_email else None
        match_basis = "email" if clickup_member else ""
        if clickup_member is None and roster_user is not None and roster_user.clickup_user_id:
            clickup_member = clickup_by_id.get(str(roster_user.clickup_user_id))
            if clickup_member is not None:
                match_basis = "confirmed_roster"
        if clickup_member is None:
            name_matches = clickup_by_name.get(_normalized(slack_name), [])
            if len(name_matches) == 1:
                clickup_member = name_matches[0]
                match_basis = "exact_name"

        clickup_id = str(clickup_member.get("id") or "") if clickup_member else ""
        if roster_user is None and clickup_id:
            roster_user = roster_clickup_ids.get(clickup_id)
        worker_type, worker_type_confidence = _estimate_worker_type(
            roster_user=roster_user,
            clickup_id=clickup_id,
            admin_clickup_ids=admin_clickup_ids,
            slack_name=slack_name,
            title=title,
        )
        override = overrides.get(slack_id, {})
        if override:
            worker_type = str(override.get("worker_type") or worker_type)
            worker_type_confidence = "confirmed_by_operator"
        rows.append(
            {
                "slack_user_id": slack_id,
                "slack_name": slack_name,
                "slack_email": slack_email,
                "slack_title": title,
                "clickup_user_id": clickup_id,
                "clickup_name": (
                    str(clickup_member.get("username") or clickup_member.get("name") or "")
                    if clickup_member
                    else ""
                ),
                "clickup_email": str(clickup_member.get("email") or "") if clickup_member else "",
                "identity_match": match_basis or "unmatched",
                "identity_confidence": (
                    "confirmed"
                    if match_basis == "confirmed_roster"
                    else "high"
                    if match_basis in {"email", "exact_name"}
                    else "unresolved"
                ),
                "worker_type": worker_type,
                "worker_type_confidence": worker_type_confidence,
                "time_tracking_scope": str(
                    override.get("time_tracking_scope")
                    or ("active_roster" if roster_user else "needs_confirmation")
                ),
                "overtime_policy": str(
                    override.get("overtime_policy") or "needs_confirmation"
                ),
                "already_in_roster": bool(roster_user),
                "roster_user_key": roster_user.user_key if roster_user else "",
                "needs_confirmation": (
                    not clickup_member
                    or (worker_type == "needs_confirmation" and not override)
                    or (not roster_user and not override)
                ),
            }
        )
    return sorted(rows, key=lambda row: str(row["slack_name"]).lower())


def _load_identity_overrides(runtime: InternManagementRuntime) -> dict[str, dict[str, Any]]:
    storage_root = runtime._storage_root_path()
    if storage_root is None:
        return {}
    path = (
        storage_root
        / "dashboard"
        / "payroll"
        / "workforce_identity_overrides.json"
    )
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        str(slack_id): value
        for slack_id, value in loaded.items()
        if isinstance(value, dict)
    }


def _estimate_worker_type(
    *,
    roster_user: Any,
    clickup_id: str,
    admin_clickup_ids: set[str],
    slack_name: str,
    title: str,
) -> tuple[str, str]:
    if roster_user is not None:
        return str(roster_user.worker_type), "confirmed_roster"
    if clickup_id and clickup_id in admin_clickup_ids:
        return "employee", "medium_admin_inference"
    combined = f"{slack_name} {title}".lower()
    if "engineering" in combined or "external partner" in combined:
        return "contractor", "medium_name_inference"
    if any(term in title.lower() for term in ("ceo", "employee", "director", "manager")):
        return "employee", "medium_title_inference"
    return "needs_confirmation", "unresolved"


def write_workforce_mapping(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else ["empty"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a read-only Slack/ClickUp workforce identity mapping proposal."
    )
    parser.add_argument(
        "--output",
        default="storage/dashboard/payroll/workforce_identity_candidates.csv",
    )
    return parser.parse_args(argv)


async def _run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_dotenv()
    runtime = InternManagementRuntime()
    await runtime.refresh_configuration(force=True)
    rows = await build_workforce_mapping(runtime)
    output = Path(args.output).resolve()
    write_workforce_mapping(output, rows)
    unresolved = sum(bool(row["needs_confirmation"]) for row in rows)
    print(f"Wrote {len(rows)} Slack identities to {output}")
    print(f"{unresolved} identities or worker classifications need confirmation.")
    return 0


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(asyncio.run(_run(argv)))


if __name__ == "__main__":
    main()

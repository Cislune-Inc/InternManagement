from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


MANAGEMENT_TASK_TITLE = "Program management, reporting, and billing support (ongoing)"
PROJECT_MANAGEMENT_TARGETS = (
    ("CisluneCUspace", "LM_Nightjar"),
    ("CisluneCUspace", "LunaRecycle Challenge"),
    ("GRASP", "CARVE PM"),
    ("GRASP", "GRASP PM"),
    ("PERDEX Reduce Sublimation", "Sublimation Plan"),
    ("Summer 2026 Interns", "Reporting + Space Grant Deadlines"),
    ("TREAD", "TREAD PM"),
    ("XR", "PM for SimMoon"),
)


async def seed_project_management_tasks(
    runtime: Any,
    *,
    apply: bool,
    notify_before_apply: Callable[[list[dict[str, str]]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    if not runtime.clickup or not runtime.config:
        raise RuntimeError("ClickUp and the Don Pollo configuration must be available.")
    tasks = await runtime.clickup.list_workspace_tasks(limit=2000, include_closed=False)
    by_project: dict[tuple[str, str], dict[str, Any]] = {}
    existing: set[tuple[str, str, str]] = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        folder_name = str((task.get("folder") or {}).get("name") or "").strip()
        list_payload = task.get("list") or {}
        list_name = str(list_payload.get("name") or "").strip()
        list_id = str(list_payload.get("id") or "").strip()
        if folder_name and list_name and list_id:
            by_project.setdefault((folder_name, list_name), {"list_id": list_id})
            existing.add((folder_name, list_name, str(task.get("name") or "").strip().casefold()))

    assignee_ids: list[str] = []
    assignee_names: list[str] = []
    configured_approvers = {
        str(name).strip().casefold()
        for name in runtime.config.clickup.new_task_approver_names
        if str(name).strip()
    }
    for admin in runtime.admin_profiles():
        if configured_approvers and admin.name.casefold() not in configured_approvers:
            continue
        member_id = admin.clickup_user_id
        if not member_id:
            member_id = await runtime.clickup.resolve_workspace_member_id(
                name=admin.name,
                email=admin.clickup_user_email,
            )
        if member_id and str(member_id) not in assignee_ids:
            assignee_ids.append(str(member_id))
            assignee_names.append(admin.name)
    if not assignee_ids:
        raise RuntimeError("Neither configured management approver could be mapped to ClickUp.")

    planned: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    for contract_name, list_name in PROJECT_MANAGEMENT_TARGETS:
        project = by_project.get((contract_name, list_name))
        item = {"contract": contract_name, "list": list_name}
        if not project:
            missing.append(item)
            continue
        if (contract_name, list_name, MANAGEMENT_TASK_TITLE.casefold()) in existing:
            skipped.append({**item, "reason": "existing open task"})
            continue
        planned.append({**item, "list_id": str(project["list_id"])})

    if apply and planned:
        if notify_before_apply is None:
            raise RuntimeError("Management approvers must be notified before ClickUp tasks are created.")
        await notify_before_apply(planned)

    created: list[dict[str, str]] = []
    if apply:
        for item in planned:
            description = (
                f"Ongoing project-linked management bucket for {item['contract']} / {item['list']}.\n\n"
                "Use this task for time spent on planning and coordination, schedule/budget/risk review, "
                "customer or NASA communication, deliverable tracking, billing and labor-allocation support, "
                "and reviewing or unblocking project work. Add a meaningful time-entry description naming "
                "the result, decision, document, or coordination outcome. Create a separate task for any "
                "substantial standalone deliverable.\n\n"
                "Approved by Erik Franks in Codex on 2026-08-07."
            )
            task = await runtime.clickup.create_task(
                item["list_id"],
                name=MANAGEMENT_TASK_TITLE,
                description=description,
                assignee_ids=assignee_ids,
                priority="normal",
            )
            created.append(
                {
                    "contract": item["contract"],
                    "list": item["list"],
                    "task_id": str(task.get("id") or ""),
                    "url": str(task.get("url") or ""),
                }
            )

    return {
        "applied": apply,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "title": MANAGEMENT_TASK_TITLE,
        "assignees": assignee_names,
        "planned": planned if not apply else [],
        "created": created,
        "skipped": skipped,
        "missing": missing,
    }

from __future__ import annotations

import argparse
import asyncio
import json

from dotenv import load_dotenv

from agent.project_management_tasks import seed_project_management_tasks
from agent.runtime import InternManagementRuntime


async def _run(*, apply: bool) -> dict:
    runtime = InternManagementRuntime()
    await runtime.refresh_configuration(force=True)

    async def notify(planned: list[dict[str, str]]) -> None:
        if not runtime.slack or not runtime.config:
            raise RuntimeError("Slack must be available to notify management before task creation.")
        approver_names = {
            name.casefold() for name in runtime.config.clickup.new_task_approver_names
        }
        approvers = [
            admin
            for admin in runtime.admin_profiles()
            if admin.name.casefold() in approver_names
        ]
        if not approvers or any(not admin.slack_user_id for admin in approvers):
            raise RuntimeError("Every configured management approver needs a Slack member ID.")
        projects = ", ".join(f"{item['contract']} / {item['list']}" for item in planned)
        message = (
            "Approved Don Pollo project-management task setup is starting. "
            f"It will create {len(planned)} deduplicated, project-linked ongoing management/billing-support "
            f"task(s), assigned to the configured approvers, in: {projects}."
        )
        for admin in approvers:
            await runtime.slack.post_message(str(admin.slack_user_id), message)

    result = await seed_project_management_tasks(
        runtime,
        apply=apply,
        notify_before_apply=notify if apply else None,
    )
    if apply:
        runtime.state_store.set_operational_state("project_management_task_seed_v1", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed deduplicated project-linked management tasks.")
    parser.add_argument("--apply", action="store_true", help="Create the planned ClickUp tasks.")
    args = parser.parse_args()
    load_dotenv()
    print(json.dumps(asyncio.run(_run(apply=args.apply)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

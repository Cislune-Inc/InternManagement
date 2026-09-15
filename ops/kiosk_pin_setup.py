"""Trusted local operator opens a ten-minute PIN setup window; never reads PINs."""
import argparse
import asyncio

from dotenv import load_dotenv

from agent.kiosk_pins import KioskPins
from agent.runtime import InternManagementRuntime
from agent.slack_beta import clock_user, enabled


async def run(args):
    load_dotenv()
    runtime = InternManagementRuntime()
    await runtime.refresh_configuration(force=True)
    actor = args.slack_id
    if args.owner:
        profile = next((p for p in runtime.config.admins if p.discord_user_id == runtime.config.admin_discord_user_id), None)
        actor = profile.slack_user_id if profile else None
    user = clock_user(runtime, actor) if actor else None
    if not user or not enabled(runtime, actor):
        raise SystemExit("Target must be an active, enrolled identity. No enrollment or PIN changed.")
    if args.apply:
        KioskPins(runtime.state_store).allow_setup(actor, authorized_by="verified_local_operator")
        print("PIN setup open for " + user.display_name + " for ten minutes. No clock change or message sent.")
    else:
        print("Dry run: ready to open PIN setup for " + user.display_name + ".")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--owner", action="store_true")
    selection.add_argument("--slack-id")
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args()))

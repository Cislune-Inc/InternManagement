from __future__ import annotations

import asyncio
import os

import discord
from dotenv import load_dotenv

from .discord_bot import InternManagementDiscordBot
from .process_lock import SingleInstanceLock
from .runtime import InternManagementRuntime
from .ssl_compat import ensure_ssl_cert_file


def main() -> None:
    load_dotenv()
    ensure_ssl_cert_file()
    runtime = InternManagementRuntime()
    try:
        asyncio.run(runtime.refresh_configuration(force=True))
        slack_only = bool(runtime.config.slack.work_intake_beta_slack_user_ids) or os.getenv("DP_TRANSPORT", "").lower() == "slack"
        if not slack_only:
            asyncio.run(runtime.backfill_transcripts_to_pacific_once())
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    if slack_only:
        from .slack_beta import run_slack_only

        with SingleInstanceLock(runtime.bootstrap.state_db_path.parent / "agent.lock"):
            asyncio.run(run_slack_only(runtime))
        return
    token = os.environ["DISCORD_BOT_TOKEN"]
    client = InternManagementDiscordBot(runtime)
    try:
        with SingleInstanceLock(runtime.bootstrap.state_db_path.parent / "agent.lock"):
            try:
                client.run(token)
            except discord.errors.PrivilegedIntentsRequired as exc:
                raise SystemExit(
                    "Discord blocked startup because the bot requests the privileged MESSAGE CONTENT intent.\n"
                    "Enable Message Content Intent in the Discord Developer Portal for this bot, then run it again.\n"
                    "Path: Discord Developer Portal -> Applications -> Your Bot -> Bot -> Privileged Gateway Intents."
                ) from exc
            except (RuntimeError, ValueError) as exc:
                raise SystemExit(str(exc)) from exc
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()

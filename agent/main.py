from __future__ import annotations

import asyncio
import os

import discord
from dotenv import load_dotenv

from .discord_bot import InternManagementDiscordBot
from .process_lock import SingleInstanceLock
from .runtime import InternManagementRuntime


def main() -> None:
    load_dotenv()
    token = os.environ["DISCORD_BOT_TOKEN"]
    runtime = InternManagementRuntime()
    try:
        asyncio.run(runtime.refresh_configuration(force=True))
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

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

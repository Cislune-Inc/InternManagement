from __future__ import annotations

import logging

import discord
from discord.ext import tasks

from .runtime import InternManagementRuntime


logger = logging.getLogger(__name__)


class InternManagementDiscordBot(discord.Client):
    def __init__(self, runtime: InternManagementRuntime) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        super().__init__(intents=intents)
        self.runtime = runtime

    async def setup_hook(self) -> None:
        self.scheduler.start()

    async def on_ready(self) -> None:
        await self.runtime.refresh_configuration(force=True)
        print(f"Logged in as {self.user} ({self.user.id if self.user else 'unknown'})")

    async def on_message(self, message: discord.Message) -> None:
        if not self.user or message.author.id == self.user.id:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        try:
            await self.runtime.handle_direct_message(self, message)
        except Exception:
            logger.exception("Unhandled error while processing Discord DM %s", message.id)

    @tasks.loop(minutes=1)
    async def scheduler(self) -> None:
        if self.is_ready():
            try:
                await self.runtime.scheduler_tick(self)
            except Exception:
                logger.exception("Unhandled error in scheduler tick")

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.wait_until_ready()

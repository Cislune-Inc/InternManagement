from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord.ext import tasks

from .runtime import InternManagementRuntime
from .ssl_compat import build_aiohttp_connector
from .slack_receiver import SlackSocketReceiver


logger = logging.getLogger(__name__)


class InternManagementDiscordBot(discord.Client):
    def __init__(self, runtime: InternManagementRuntime) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        super().__init__(intents=intents)
        self.runtime = runtime
        self.slack_receiver: SlackSocketReceiver | None = None
        self.slack_receiver_task = None

    async def login(self, token: str) -> None:
        self.http.connector = build_aiohttp_connector()
        await super().login(token)

    async def setup_hook(self) -> None:
        self.scheduler.start()
        slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
        slack_app_token = os.environ.get("SLACK_APP_TOKEN")
        if slack_bot_token and slack_app_token:
            self.slack_receiver = SlackSocketReceiver(
                self.runtime,
                self,
                bot_token=slack_bot_token,
                app_token=slack_app_token,
            )
            self.slack_receiver_task = asyncio.create_task(self.slack_receiver.start())

    async def close(self) -> None:
        if self.slack_receiver is not None:
            await self.slack_receiver.close()
        if self.slack_receiver_task is not None:
            self.slack_receiver_task.cancel()
        await super().close()

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

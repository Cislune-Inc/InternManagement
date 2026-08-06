from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


class SlackSocketReceiver:
    def __init__(
        self,
        runtime: Any,
        discord_client: Any,
        *,
        bot_token: str,
        app_token: str,
    ) -> None:
        self.runtime = runtime
        self.discord_client = discord_client
        self.bot_token = bot_token
        self.app_token = app_token
        self._handler: Any | None = None

    async def start(self) -> None:
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        app = AsyncApp(token=self.bot_token)

        @app.event("message")
        async def handle_message(event: dict[str, Any]) -> None:
            if str(event.get("channel_type") or "") != "im":
                return
            if event.get("bot_id") or event.get("subtype"):
                return
            try:
                await self.runtime.handle_slack_direct_message(
                    self.discord_client,
                    event,
                )
            except Exception:
                logger.exception(
                    "Unhandled error while processing Slack DM %s",
                    event.get("event_ts") or event.get("ts") or "unknown",
                )

        self._handler = AsyncSocketModeHandler(app, self.app_token)
        logger.info("Starting Don Pollo Slack Socket Mode receiver.")
        await self._handler.start_async()

    async def close(self) -> None:
        if self._handler is not None:
            await self._handler.close_async()

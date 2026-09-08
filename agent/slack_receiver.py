from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from .ssl_compat import build_ssl_context


logger = logging.getLogger(__name__)

CLOCK_ACTIONS = {
    "dp_clock_in": ("Kiosk instructions", "clock in onsite"),
    "dp_clock_out": ("Clock out", "clock out"),
    "dp_clock_hours": ("My hours", "hours"),
    "dp_clock_lunch": ("Lunch", "lunch"),
    "dp_clock_back": ("Back", "back"),
    "dp_clock_rest": ("Paid rest", "break"),
    "dp_clock_work_status": ("Current work", "work status"),
    "dp_clock_work_draft": ("Preview update", "work draft"),
    "dp_clock_handoffs": ("Confirmed handoffs", "work handoffs"),
    "dp_clock_pin_setup": ("Set up / reset PIN", "kiosk setup"),
}


async def handle_clock_action(runtime: Any, discord_client: Any, body: dict[str, Any]) -> None:
    actions = body.get("actions") or []
    action = actions[0] if actions else {}
    selection = CLOCK_ACTIONS.get(str(action.get("action_id") or ""))
    if not selection:
        return
    await runtime.handle_slack_direct_message(discord_client, {
        "user": str((body.get("user") or {}).get("id") or ""),
        "text": selection[1],
        "event_ts": str(action.get("action_ts") or ""),
    })


def build_slack_web_client(bot_token: str) -> Any:
    """Build Slack's async client with the same verified CA bundle as Discord."""
    from slack_sdk.web.async_client import AsyncWebClient

    return AsyncWebClient(token=bot_token, ssl=build_ssl_context())


async def publish_app_home(runtime: Any, web_client: Any, slack_user_id: str) -> None:
    """Publish the durable portal/login entry point when a user opens the app."""
    normalized_user_id = str(slack_user_id or "").strip()
    if not normalized_user_id:
        return
    await runtime.refresh_configuration()
    view = runtime.build_slack_app_home_view(normalized_user_id)
    await web_client.views_publish(user_id=normalized_user_id, view=view)


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

        web_client = build_slack_web_client(self.bot_token)
        app = AsyncApp(client=web_client)

        @app.action(re.compile(r"^dp_clock_"))
        async def handle_clock_button(ack: Any, body: dict[str, Any]) -> None:
            await ack()
            await handle_clock_action(self.runtime, self.discord_client, body)
            try:
                await publish_app_home(self.runtime, web_client, str((body.get("user") or {}).get("id") or ""))
            except Exception:
                logger.warning("Clock action handled; App Home refresh failed.")

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

        @app.event("app_home_opened")
        async def handle_app_home_opened(event: dict[str, Any]) -> None:
            try:
                await publish_app_home(
                    self.runtime,
                    web_client,
                    str(event.get("user") or ""),
                )
            except Exception:
                logger.exception(
                    "Unhandled error while publishing Slack App Home for %s",
                    event.get("user") or "unknown",
                )

        self._handler = AsyncSocketModeHandler(
            app,
            self.app_token,
            web_client=web_client,
        )
        logger.info("Starting Don Pollo Slack Socket Mode receiver.")
        await self._handler.connect_async()
        ready_message = (
            f"Don Pollo Slack Socket Mode receiver connected pid={os.getpid()}."
        )
        logger.info(ready_message)
        print(ready_message, flush=True)
        await asyncio.Event().wait()

    async def close(self) -> None:
        if self._handler is not None:
            await self._handler.close_async()

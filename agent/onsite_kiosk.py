"""Mini-only PIN clock. Never proxy/tunnel this listener or expose manager tools."""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from .kiosk_page import PAGE
from .kiosk_pins import KioskPins

ORIGIN = "http://127.0.0.1:8766"


def create_app(runtime: Any) -> web.Application:
    csrf, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(20)
    attempts: deque[float] = deque()

    @web.middleware
    async def boundary(request: web.Request, handler: Any) -> web.Response:
        if request.remote != "127.0.0.1" or request.host != "127.0.0.1:8766":
            raise web.HTTPForbidden(text="Use the physical shop Mini check-in screen.")
        if request.method != "GET" and (request.headers.get("Origin") != ORIGIN or
                not secrets.compare_digest(request.headers.get("X-Kiosk-CSRF", ""), csrf)):
            raise web.HTTPForbidden(text="Reload the kiosk screen and try again.")
        response = await handler(request)
        response.headers.update({"Cache-Control": "no-store", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    async def page(request: web.Request) -> web.Response:
        from .slack_beta import clock_user
        await runtime.refresh_configuration()
        people = []
        for actor in dict.fromkeys(runtime.config.slack.work_intake_beta_slack_user_ids):
            user = clock_user(runtime, actor)
            if user:
                pending = actor in runtime.config.slack.clock_handover_pending_slack_user_ids
                people.append({"id": actor, "name": user.display_name + (" — setup ready; handover pending" if pending else "")})
        encoded = json.dumps(sorted(people, key=lambda p: p["name"].casefold())).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        html = PAGE.replace("__NONCE__", nonce).replace("__CSRF__", json.dumps(csrf)).replace("__PEOPLE__", encoded)
        return web.Response(text=html, content_type="text/html")

    async def confirm(request: web.Request) -> web.Response:
        current = time.monotonic()
        while attempts and attempts[0] < current - 60:
            attempts.popleft()
        if len(attempts) >= 20:
            return web.json_response({"message": "Too many attempts. Wait one minute and try again."}, status=429)
        attempts.append(current)
        from .slack_beta import archive, clock_user, enabled, ledger
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("actor"), str) or not isinstance(payload.get("pin"), str):
                raise ValueError("Choose your name and enter your PIN.")
            actor, pin = payload["actor"], payload["pin"]
            await runtime.refresh_configuration()
            user = clock_user(runtime, actor)
            if not enabled(runtime, actor) or user is None:
                raise ValueError("This identity is not enabled. Ask Erik; no clock change was made.")
            async with runtime._user_session_lock(user.user_key):
                pins = KioskPins(runtime.state_store)
                if request.path == "/pin/setup":
                    confirmation = payload.get("confirmation")
                    if not isinstance(confirmation, str):
                        raise ValueError("Enter your new PIN twice.")
                    await asyncio.to_thread(pins.set_pin, actor, pin, confirmation)
                    if actor in runtime.config.slack.clock_handover_pending_slack_user_ids:
                        return web.json_response({"message": "PIN saved. Your clock has not changed. Erik still needs to confirm your Gusto-to-DP handover before your first DP start."})
                    return web.json_response({"message": "PIN saved. Choose your name and enter it to start work. Your clock has not changed."})
                await asyncio.to_thread(pins.verify, actor, pin)
                action, ident = payload.get("action"), payload.get("request_id")
                if action not in {"start", "out", "lunch", "rest"} or not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9-]{36}", ident):
                    raise ValueError("Choose a clock action on the screen.")
                now = datetime.now(timezone.utc)
                clock = ledger(runtime)
                command = action
                if action == "start":
                    with runtime.state_store._connect() as conn:
                        session = clock._current(clock._sessions(conn, user.user_key), user, now)
                    command = "back" if session.metadata.get("slack_clock_meal_started_at") or session.metadata.get("slack_clock_rest_started_at") else "in"
                response, session = clock.handle(user, command, "onsite" if command == "in" else "",
                    event_id=f"kiosk-pin:{action}:{ident}", now=now, kiosk_verified=True)
                if action == "start" and response.startswith("Your rest return is recorded, but the work clock stopped"):
                    response, session = clock.handle(user, "in", "onsite",
                        event_id=f"kiosk-pin:resume:{ident}", now=now, kiosk_verified=True)
                await archive(runtime, user, session, now)
            if response.startswith("Clocked out at"):
                response = response.split(". This shift-day", 1)[0] + ". Saved. See My hours in Slack for totals."
            return web.json_response({"message": user.display_name + "\n" + response})
        except ValueError as exc:
            message = str(exc) if not isinstance(exc, json.JSONDecodeError) else "Invalid request. Reload the screen."
            return web.json_response({"message": message}, status=400)
        except Exception:
            return web.json_response({"message": "Could not finish. Check My hours in Slack before retrying; report actual times to Erik if needed."}, status=503)

    app = web.Application(middlewares=[boundary], client_max_size=1024)
    app.router.add_get("/", page)
    app.router.add_post("/confirm", confirm)
    app.router.add_post("/pin/setup", confirm)
    return app


async def serve(runtime: Any) -> None:
    runner = web.AppRunner(create_app(runtime), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 8766).start()
        await asyncio.Future()
    finally:
        await runner.cleanup()

"""Physical Mini check-in. Never expose this listener through a proxy or tunnel.

Slack establishes identity; the loopback browser confirms presence. This is not
tamper-proof against code sharing or trusted administrators with remote access.
The existing ledger remains the only time authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import web

ORIGIN = "http://127.0.0.1:8766"


class KioskCodes:
    def __init__(self, store: Any) -> None:
        self.store = store
        with store._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS kiosk_codes (
                digest TEXT PRIMARY KEY, actor TEXT NOT NULL, command TEXT NOT NULL,
                detail TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kiosk_codes_actor ON kiosk_codes(actor)")

    def issue(self, actor: str, command: str, detail: str, *, now: datetime | None = None) -> str:
        if command not in {"in", "back"} or (command == "in" and not detail.lower().startswith("onsite")):
            raise ValueError("Only onsite starts and returns use kiosk codes.")
        now = now or datetime.now(timezone.utc)
        code = "".join(secrets.choice("23456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(8))
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE kiosk_codes SET consumed_at=? WHERE actor=? AND consumed_at IS NULL", (now.isoformat(), actor))
            conn.execute("INSERT INTO kiosk_codes VALUES (?,?,?,?,?,NULL)",
                         (digest, actor, command, detail[:6000], (now + timedelta(minutes=2)).isoformat()))
        return code

    def consume(self, code: str, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        normalized = code.strip().upper()
        if len(normalized) != 8 or not normalized.isalnum():
            raise ValueError("Enter the eight-character code from your own DP Slack conversation.")
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM kiosk_codes WHERE digest=?", (digest,)).fetchone()
            if not row or row["consumed_at"] or datetime.fromisoformat(row["expires_at"]) <= now:
                raise ValueError("Code expired or already used. Request a new shop check-in code in DP on Slack. No new hours were added.")
            conn.execute("UPDATE kiosk_codes SET consumed_at=? WHERE digest=?", (now.isoformat(), digest))
            return dict(row)


PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Don Pollo · Shop check-in</title><style>
:root{color-scheme:light;font-family:system-ui,sans-serif;color:#102b41;background:#eef3f8}
body{margin:0;padding:clamp(20px,5vw,64px)}main{max-width:680px;margin:auto}
header{font-size:1rem;font-weight:750;color:#265a78}h1{font-size:clamp(2rem,5vw,3rem);margin:.6em 0}
section{background:white;padding:clamp(22px,5vw,44px);border:1px solid #bacddc;border-radius:18px}
p,label{font-size:1.125rem;line-height:1.6}label{display:block;font-weight:650}
input,button{box-sizing:border-box;width:100%;font:inherit;font-size:1.25rem;border-radius:8px;padding:16px;margin-top:12px}
input{border:2px solid #527187;letter-spacing:.3em;text-transform:uppercase}button{border:0;background:#075e90;color:white;font-weight:700;cursor:pointer}
button:disabled{opacity:.65}input:focus-visible,button:focus-visible,a:focus-visible{outline:3px solid #bd6200;outline-offset:3px}
#result{white-space:pre-wrap;font-size:1.125rem;line-height:1.6}small{display:block;font-size:1rem;line-height:1.5;color:#3c5364;margin-top:24px}
</style><main><header>CISLUNE / DON POLLO</header><h1>Start here. Then get to work.</h1>
<section><p>In your own Don Pollo Slack conversation, choose <strong>Shop check-in code</strong> (or <strong>Back</strong> after lunch). Enter the code here within two minutes.</p>
<form id="checkin"><label for="code">Your eight-character code</label><input id="code" name="code" maxlength="8" minlength="8" autocomplete="off" autocapitalize="characters" spellcheck="false" required>
<button id="submit">Confirm my start / return</button></form><p id="result" role="status" aria-live="polite"></p>
<small>Your clock changes only after confirmation. Use Slack for your hours, updates, breaks and clock-out—from anywhere. Do not share codes or check in for someone else.</small></section>
<p>Offsite? Get Erik’s advance approval, then use <strong>clock in remote</strong> in Slack. If DP fails, send Erik your actual hours; never work unrecorded.</p></main>
<script nonce="__NONCE__">
const form=document.querySelector('#checkin'),result=document.querySelector('#result'),button=document.querySelector('#submit'),code=document.querySelector('#code');
let reset;
form.addEventListener('submit',async e=>{e.preventDefault();clearTimeout(reset);button.disabled=true;result.textContent='Checking…';
try{const response=await fetch('/confirm',{method:'POST',headers:{'Content-Type':'application/json','X-Kiosk-CSRF':__CSRF__},body:JSON.stringify({code:code.value})});
const data=await response.json();result.textContent=data.message;code.value='';}
catch{result.textContent='Could not confirm. Check My hours in Slack before retrying. If needed, send Erik the actual times.';}
finally{button.disabled=false;code.focus();reset=setTimeout(()=>{result.textContent='';code.value='';},20000);}});
</script></html>"""


def create_app(runtime: Any) -> web.Application:
    csrf = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(20)
    attempts: deque[float] = deque()

    @web.middleware
    async def boundary(request: web.Request, handler: Any) -> web.Response:
        # Ignore forwarded headers. VPN/LAN requests cannot become kiosk requests.
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
        return web.Response(text=PAGE.replace("__NONCE__", nonce).replace("__CSRF__", json.dumps(csrf)), content_type="text/html")

    async def confirm(request: web.Request) -> web.Response:
        monotonic = time.monotonic()
        while attempts and attempts[0] < monotonic - 60:
            attempts.popleft()
        if len(attempts) >= 12:
            return web.json_response({"message": "Too many attempts. Wait one minute, then request a new code."}, status=429)
        attempts.append(monotonic)
        from .slack_beta import archive, clock_user, enabled, ledger
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("code"), str):
                raise ValueError("Enter your own code from Slack.")
            now = datetime.now(timezone.utc)
            ticket = KioskCodes(runtime.state_store).consume(payload["code"], now=now)
            # Refresh before authorization; a code never reenrolls an offboarded user.
            await runtime.refresh_configuration()
            user = clock_user(runtime, ticket["actor"])
            if not enabled(runtime, ticket["actor"]) or user is None:
                raise ValueError("This identity is not currently enabled. Contact Erik; no clock change was made.")
            async with runtime._user_session_lock(user.user_key):
                response, session = ledger(runtime).handle(user, ticket["command"], ticket["detail"],
                    event_id="kiosk:" + ticket["digest"], now=now, kiosk_verified=True)
                await archive(runtime, user, session, now)
                status = ledger(runtime).snapshot(user, now)
            return web.json_response({"message": user.display_name + "\n" + response + "\n\n" + status.replace("*", "")})
        except (ValueError, TypeError, json.JSONDecodeError):
            return web.json_response({"message": "Code invalid, expired, used, or the clock needs review. Request a fresh code in Slack; use My hours to check the record. No guessed hours were added."}, status=400)
        except Exception:
            # The ledger may already have committed: don't claim failure erased it.
            return web.json_response({"message": "Could not finish confirmation. Check My hours in Slack before retrying; report actual times to Erik if needed."}, status=503)

    app = web.Application(middlewares=[boundary], client_max_size=1024)
    app.router.add_get("/", page)
    app.router.add_post("/confirm", confirm)
    return app


async def serve(runtime: Any) -> None:
    runner = web.AppRunner(create_app(runtime), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 8766).start()
        await asyncio.Future()
    finally:
        await runner.cleanup()

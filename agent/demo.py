from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import discord
from dotenv import load_dotenv

from .advisor import HeuristicAdvisor
from .runtime import InternManagementRuntime
from .ssl_compat import build_aiohttp_connector, ensure_ssl_cert_file
from .time_utils import resolve_timezone


ROOT = Path(__file__).resolve().parent.parent


@dataclass
class DemoAttachment:
    filename: str
    content: bytes
    content_type: str = "image/jpeg"
    url: str = "demo://attachment"

    @property
    def size(self) -> int:
        return len(self.content)

    async def read(self) -> bytes:
        return self.content


@dataclass
class DemoAuthor:
    id: int


@dataclass
class DemoInboundMessage:
    id: int
    author: DemoAuthor
    content: str
    attachments: list[DemoAttachment]
    created_at: datetime


@dataclass
class DemoSentMessage:
    id: int
    content: str
    created_at: datetime


class DemoDM:
    def __init__(self, client: "DemoClient", user_id: int) -> None:
        self.client = client
        self.user_id = user_id

    async def send(self, content: str, *, view=None) -> DemoSentMessage:
        message = DemoSentMessage(
            id=next(self.client.message_ids),
            content=content,
            created_at=self.client.now,
        )
        self.client.outbox.append((self.user_id, self.client.now, content))
        print(f"[{self.client.now.isoformat()}] BOT -> {self.user_id}: {content}")
        return message


class DemoDiscordUser:
    def __init__(self, client: "DemoClient", user_id: int) -> None:
        self.client = client
        self.id = user_id

    async def create_dm(self) -> DemoDM:
        return DemoDM(self.client, self.id)


class DemoClient:
    def __init__(self, bot_user_id: int, start_time: datetime) -> None:
        self.user = SimpleNamespace(id=bot_user_id)
        self.now = start_time
        self.message_ids = count(10_000)
        self.outbox: list[tuple[int, datetime, str]] = []
        self._users: dict[int, DemoDiscordUser] = {}

    def set_now(self, now: datetime) -> None:
        self.now = now

    async def fetch_user(self, user_id: int) -> DemoDiscordUser:
        if user_id not in self._users:
            self._users[user_id] = DemoDiscordUser(self, user_id)
        return self._users[user_id]


class LiveDemoDiscordClient(discord.Client):
    def __init__(
        self,
        runtime: InternManagementRuntime,
        user_key: str,
        pace_seconds: float,
    ) -> None:
        super().__init__(intents=discord.Intents.default())
        self.runtime = runtime
        self.user_key = user_key
        self.pace_seconds = pace_seconds

    async def login(self, token: str) -> None:
        self.http.connector = build_aiohttp_connector()
        await super().login(token)

    async def on_ready(self) -> None:
        try:
            await self.runtime.refresh_configuration(force=True)
            user = self.runtime.roster_by_key.get(self.user_key)
            if not user or not user.active:
                raise RuntimeError(f"Active user {self.user_key!r} was not found in the roster.")
            now = self.runtime.resolve_user_local_now(user)
            session = self.runtime.state_store.get_session(
                user.user_key,
                self.runtime.resolve_user_workday_date(user, now),
            )
            session.stage = "demo_live"
            session.metadata["live_demo"] = True
            self.runtime.state_store.save_session(session)

            script = build_live_demo_messages(user.display_name)
            print(f"Running live demo for {user.display_name} ({user.user_key})")
            for index, content in enumerate(script, start=1):
                step_now = self.runtime.resolve_user_local_now(user)
                print(f"[{index}/{len(script)}] Sending DM to {user.display_name}")
                try:
                    await self.runtime._send_dm(self, user, session, content, step_now)
                except discord.Forbidden as exc:
                    raise RuntimeError(
                        f"Discord refused the DM to {user.display_name} ({user.discord_user_id}). "
                        "The bot likely has no mutual guild with that user or the user cannot be messaged."
                    ) from exc
                if index < len(script):
                    await asyncio.sleep(self.pace_seconds)
            await self.runtime.write_dashboard()
            print("Live demo complete.")
        finally:
            await self.close()


async def run_demo(user_key: str | None) -> Path:
    demo_root = _prepare_demo_environment()
    os.environ["BOOTSTRAP_PATH"] = str(demo_root / "bootstrap.demo.json")

    runtime = InternManagementRuntime()
    await runtime.refresh_configuration(force=True)
    runtime.clickup = None
    runtime.advisor = HeuristicAdvisor()
    runtime.image_intelligence.enabled = False

    active_users = [user for user in runtime.roster_by_key.values() if user.active]
    if not active_users:
        raise RuntimeError("No active users found in the demo roster.")
    if user_key:
        user = runtime.roster_by_key.get(user_key)
        if not user or not user.active:
            raise RuntimeError(f"Active user {user_key!r} was not found in the demo roster.")
    else:
        user = active_users[0]

    tz = resolve_timezone(runtime.resolve_user_timezone_name(user))
    start = datetime.now(tz=tz).replace(hour=9, minute=0, second=0, microsecond=0)
    client = DemoClient(bot_user_id=999999999999, start_time=start)
    session = runtime.state_store.get_session(
        user.user_key,
        runtime.resolve_user_workday_date(user, start),
    )

    print(f"Demo user: {user.display_name} ({user.user_key})")
    print(f"Demo output: {demo_root}")

    client.set_now(start)
    await runtime._maybe_send_clock_in(client, user, session, start)
    runtime.state_store.save_session(session)

    await _user_message(runtime, client, user, session, start + timedelta(minutes=3), "Yes, I clocked in.")
    await _user_message(
        runtime,
        client,
        user,
        session,
        start + timedelta(minutes=6),
        "Today I want to finish the intake form UI and clean up the project board.",
    )
    await _user_message(
        runtime,
        client,
        user,
        session,
        start + timedelta(minutes=8),
        "",
        attachments=[
            DemoAttachment(
                filename="before-start.jpg",
                content=b"demo-image-before-start",
            )
        ],
    )
    await _user_message(
        runtime,
        client,
        user,
        session,
        start + timedelta(minutes=10),
        "I might get stuck on the auth flow.",
    )

    follow_up_time = start + timedelta(hours=2, minutes=15)
    client.set_now(follow_up_time)
    await runtime._maybe_send_follow_up(client, user, session, follow_up_time)
    runtime.state_store.save_session(session)

    stuck_time = follow_up_time + timedelta(minutes=5)
    await _user_message(
        runtime,
        client,
        user,
        session,
        stuck_time,
        "I am stuck on the auth flow and need help with the redirect handling.",
    )

    admin_alert_time = stuck_time + timedelta(hours=4, minutes=1)
    client.set_now(admin_alert_time)
    await runtime._maybe_alert_admin(client, user, session, admin_alert_time)
    runtime.state_store.save_session(session)

    clock_out_time = start + timedelta(hours=8)
    await _user_message(
        runtime,
        client,
        user,
        session,
        clock_out_time,
        "I am clocking out now.",
    )
    await _user_message(
        runtime,
        client,
        user,
        session,
        clock_out_time + timedelta(minutes=2),
        "I finished the UI pass, got blocked for a while on the auth redirect, and I want to resume debugging that first tomorrow. I still think the path is workable.",
        attachments=[
            DemoAttachment(
                filename="end-of-day.jpg",
                content=b"demo-image-end-of-day",
            )
        ],
    )

    flush_time = clock_out_time + timedelta(minutes=15)
    await runtime._maybe_flush_clickup(user, session, flush_time)
    await runtime.write_dashboard()

    transcript_path = demo_root / "storage" / "people" / user.storage_folder_name / session.session_date / "transcript.md"
    dashboard_path = demo_root / "storage" / "dashboard" / "dashboard.md"
    print(f"\nTranscript: {transcript_path}")
    print(f"Dashboard: {dashboard_path}")
    return demo_root


async def run_live_demo(user_key: str, pace_seconds: float) -> None:
    load_dotenv(ROOT / ".env")
    ensure_ssl_cert_file()
    token = os.environ["DISCORD_BOT_TOKEN"]
    runtime = InternManagementRuntime()
    await runtime.refresh_configuration(force=True)
    runtime.clickup = None
    runtime.advisor = HeuristicAdvisor()
    runtime.image_intelligence.enabled = False

    client = LiveDemoDiscordClient(runtime, user_key=user_key, pace_seconds=pace_seconds)
    try:
        await client.start(token)
    except discord.errors.PrivilegedIntentsRequired:
        raise RuntimeError(
            "Discord blocked startup for the live demo because the bot requests privileged intents. "
            "Enable Message Content Intent in the Discord Developer Portal."
        ) from None


async def _user_message(
    runtime: InternManagementRuntime,
    client: DemoClient,
    user,
    session,
    when: datetime,
    content: str,
    attachments: list[DemoAttachment] | None = None,
) -> None:
    client.set_now(when)
    inbound = DemoInboundMessage(
        id=next(client.message_ids),
        author=DemoAuthor(id=user.discord_user_id),
        content=content,
        attachments=attachments or [],
        created_at=when,
    )
    print(f"[{when.isoformat()}] USER -> {user.display_name}: {content or '[attachment only]'}")
    message_record = await runtime._build_inbound_record(inbound, session, user)
    await runtime.process_inbound_event(client, user, session, message_record, when)


def _prepare_demo_environment() -> Path:
    source_config_dir = ROOT / "config"
    if not source_config_dir.exists():
        raise RuntimeError("Expected local config/ directory for the demo source data.")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    demo_root = ROOT / "demo_output" / run_id
    config_dir = demo_root / "config"
    storage_dir = demo_root / "storage"
    data_dir = demo_root / "data"
    config_dir.mkdir(parents=True, exist_ok=True)
    storage_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(source_config_dir / "agent.config.json", config_dir / "agent.config.json")
    shutil.copy2(source_config_dir / "roster.csv", config_dir / "roster.csv")

    bootstrap_payload = {
        "agent_config_path": str((config_dir / "agent.config.json").resolve()),
        "storage_root_path": str(storage_dir.resolve()),
        "state_db_path": str((data_dir / "agent_state.sqlite3").resolve()),
        "default_timezone": "America/Los_Angeles",
    }
    (demo_root / "bootstrap.demo.json").write_text(
        json.dumps(bootstrap_payload, indent=2),
        encoding="utf-8",
    )
    return demo_root


def build_live_demo_messages(display_name: str) -> list[str]:
    header = "[Live demo]"
    return [
        (
            f"{header} Hi {display_name}. This is a short scripted demo from the intern management bot. "
            "No reply is required for this test."
        ),
        f"{header} Good morning. Have you clocked in yet?",
        f"{header} Tell me what you are planning on accomplishing today.",
        f"{header} Send me a picture of your project before you start.",
        f"{header} How is the project going? Are you stuck?",
        (
            f"{header} Before you clock out, send me a picture of what you finished and a paragraph covering "
            "what you did, where you got stuck or need help, what you plan to do next, and whether your "
            "current path still makes sense."
        ),
        (
            f"{header} End of demo. In normal operation, I would archive your replies locally and "
            "post a structured summary into ClickUp after the interaction settles."
        ),
    ]


def load_active_user_keys() -> list[str]:
    runtime = InternManagementRuntime()
    asyncio.run(runtime.refresh_configuration(force=True))
    return sorted(runtime.roster_by_key.keys())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local demo of the intern management workflow.")
    parser.add_argument("--user", help="Active user_key to target.")
    parser.add_argument(
        "--mode",
        choices=["local", "live"],
        default="local",
        help="Use 'local' for an isolated simulation or 'live' to send scripted Discord DMs.",
    )
    parser.add_argument(
        "--pace-seconds",
        type=float,
        default=2.0,
        help="Seconds to wait between demo DMs in live mode.",
    )
    parser.add_argument(
        "--list-users",
        action="store_true",
        help="Print active roster user_keys and exit.",
    )
    args = parser.parse_args()
    if args.list_users:
        load_dotenv(ROOT / ".env")
        for key in load_active_user_keys():
            print(key)
        return
    if args.mode == "live":
        if not args.user:
            raise SystemExit("Live demo mode requires --user <active_user_key>.")
        asyncio.run(run_live_demo(args.user, args.pace_seconds))
        return
    asyncio.run(run_demo(args.user))


if __name__ == "__main__":
    main()

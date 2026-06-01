from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict
from pathlib import Path

from .config import parse_agent_config, parse_roster_bytes
from .models import AgentConfig, BootstrapConfig, LocalWorkspace, SessionState, UserProfile


class LocalStore:
    def __init__(self, bootstrap: BootstrapConfig) -> None:
        self.bootstrap = bootstrap
        self.config_path = bootstrap.agent_config_path
        self.storage_root = bootstrap.storage_root_path
        self.storage_root.mkdir(parents=True, exist_ok=True)

    async def load_agent_config(self) -> AgentConfig:
        raw = await asyncio.to_thread(self.config_path.read_text, encoding="utf-8")
        payload = json.loads(raw)
        return parse_agent_config(payload, self.bootstrap.default_timezone)

    async def load_roster(self, config: AgentConfig) -> list[UserProfile]:
        roster_path = self.config_path.parent / config.roster_file_name
        raw = await asyncio.to_thread(roster_path.read_bytes)
        return parse_roster_bytes(roster_path.name, raw)

    async def ensure_user_workspace(self, user: UserProfile, session_date: str) -> LocalWorkspace:
        return await asyncio.to_thread(self._ensure_user_workspace_sync, user, session_date)

    def resolve_user_workspace(self, user: UserProfile, session_date: str) -> LocalWorkspace:
        people_dir = self.storage_root / "people"
        user_dir = people_dir / _safe_name(user.storage_folder_name or user.user_key)
        daily_dir = user_dir / session_date
        images_dir = daily_dir / "images"
        return LocalWorkspace(
            root_dir=self.storage_root.resolve(),
            user_dir=user_dir.resolve(),
            daily_dir=daily_dir.resolve(),
            images_dir=images_dir.resolve(),
        )

    async def save_attachment(
        self,
        images_dir: Path,
        filename: str,
        content: bytes,
    ) -> Path:
        safe_filename = _safe_name(filename)
        return await asyncio.to_thread(self._write_bytes, images_dir / safe_filename, content)

    async def write_text_file(self, path: Path, content: str) -> Path:
        return await asyncio.to_thread(self._write_text, path, content)

    async def append_json_line(self, path: Path, payload: dict) -> Path:
        return await asyncio.to_thread(self._append_json_line, path, payload)

    async def touch_file(self, path: Path) -> Path:
        return await asyncio.to_thread(self._touch_file, path)

    async def write_dashboard(self, filename: str, content: str) -> Path:
        dashboard_path = self.storage_root / filename
        return await self.write_text_file(dashboard_path, content)

    async def write_session_snapshot(
        self,
        workspace: LocalWorkspace,
        session: SessionState,
    ) -> Path:
        payload = json.dumps(asdict(session), indent=2, sort_keys=True)
        return await self.write_text_file(workspace.daily_dir / "session.json", payload)

    def _ensure_user_workspace_sync(self, user: UserProfile, session_date: str) -> LocalWorkspace:
        workspace = self.resolve_user_workspace(user, session_date)
        user_dir = workspace.user_dir
        daily_dir = workspace.daily_dir
        images_dir = workspace.images_dir
        images_dir.mkdir(parents=True, exist_ok=True)
        self._write_text(
            user_dir / "profile.json",
            json.dumps(asdict(user), indent=2, sort_keys=True),
        )
        return workspace

    def _write_text(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path.resolve()

    def _write_bytes(self, path: Path, content: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path.resolve()

    def _append_json_line(self, path: Path, payload: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        return path.resolve()

    def _touch_file(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        return path.resolve()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().rstrip(".")
    return cleaned or "user"

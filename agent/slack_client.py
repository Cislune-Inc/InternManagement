from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

import requests


class SlackClient:
    def __init__(self, bot_token: str) -> None:
        self.bot_token = bot_token
        self.base_url = "https://slack.com/api"

    async def post_message(
        self,
        channel_id: str,
        message: str,
        *,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        import asyncio

        return await asyncio.to_thread(
            self._post_message_sync,
            channel_id,
            message,
            thread_ts=thread_ts,
        )

    async def upload_file(
        self,
        channel_id: str,
        file_path: Path,
        *,
        title: str,
        initial_comment: str = "",
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        import asyncio

        return await asyncio.to_thread(
            self._upload_file_sync,
            channel_id,
            file_path,
            title=title,
            initial_comment=initial_comment,
            thread_ts=thread_ts,
        )

    async def get_reactions(self, channel_id: str, message_ts: str) -> list[dict[str, Any]]:
        import asyncio

        payload = await asyncio.to_thread(
            self._request,
            "GET",
            "/reactions.get",
            params={
                "channel": channel_id,
                "timestamp": message_ts,
                "full": "true",
            },
        )
        message = payload.get("message")
        if not isinstance(message, dict):
            return []
        reactions = message.get("reactions")
        return reactions if isinstance(reactions, list) else []

    async def download_file(self, url: str) -> bytes:
        import asyncio

        return await asyncio.to_thread(self._download_file_sync, url)

    async def list_users(self) -> list[dict[str, Any]]:
        import asyncio

        payload = await asyncio.to_thread(
            self._request,
            "GET",
            "/users.list",
            params={"limit": 200},
        )
        members = payload.get("members")
        return members if isinstance(members, list) else []

    def _post_message_sync(
        self,
        channel_id: str,
        message: str,
        *,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel": channel_id,
            "text": message,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return self._request(
            "POST",
            "/chat.postMessage",
            json=payload,
        )

    def _upload_file_sync(
        self,
        channel_id: str,
        file_path: Path,
        *,
        title: str,
        initial_comment: str = "",
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        size = file_path.stat().st_size
        upload_payload = self._request(
            "POST",
            "/files.getUploadURLExternal",
            data={
                "filename": file_path.name,
                "length": str(size),
            },
        )
        upload_url = str(upload_payload.get("upload_url") or "")
        file_id = str(upload_payload.get("file_id") or "")
        if not upload_url or not file_id:
            raise RuntimeError("Slack did not return an upload URL and file ID.")

        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        with file_path.open("rb") as handle:
            response = requests.post(
                upload_url,
                files={"file": (file_path.name, handle, content_type)},
                timeout=60,
            )
        response.raise_for_status()

        complete_payload: dict[str, Any] = {
            "files": [{"id": file_id, "title": title}],
            "channel_id": channel_id,
        }
        if initial_comment:
            complete_payload["initial_comment"] = initial_comment
        if thread_ts:
            complete_payload["thread_ts"] = thread_ts
        return self._request(
            "POST",
            "/files.completeUploadExternal",
            json=complete_payload,
        )

    def _download_file_sync(self, url: str) -> bytes:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {self.bot_token}"},
            timeout=60,
        )
        response.raise_for_status()
        return response.content

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.bot_token}"
        if "json" in kwargs:
            headers["Content-Type"] = "application/json; charset=utf-8"
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            timeout=30,
            **kwargs,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            error = str(payload.get("error") or "unknown_error")
            raise RuntimeError(f"Slack API error for {path}: {error}")
        return payload

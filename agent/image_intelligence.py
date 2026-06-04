from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI

from .models import AttachmentRecord, MessageRecord, SessionState, UserProfile
from .openai_models import ModelFallbackChain


@dataclass(slots=True)
class ImageInsight:
    slug: str
    description: str
    tags: list[str]
    analysis_model: str | None = None


class ImageIntelligence:
    def __init__(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        self.client = AsyncOpenAI(api_key=api_key) if api_key else None
        self.models = ModelFallbackChain(
            "image intelligence",
            os.environ.get("OPENAI_VISION_MODEL"),
            os.environ.get("OPENAI_MODEL"),
            os.environ.get("BACKUP_OPENAI_MODEL"),
            "gpt-4.1-mini",
        )
        self.preferred_model = self.models.active_model or "gpt-4.1-mini"
        self.enabled = self.client is not None

    async def analyze_attachment(
        self,
        *,
        user: UserProfile,
        session: SessionState,
        original_filename: str,
        content: bytes,
        content_type: str | None,
        inbound_text: str,
        recent_messages: list[MessageRecord],
    ) -> ImageInsight:
        fallback = self._fallback_insight(original_filename)
        if not self.enabled or not self.client:
            return fallback
        mime_type = self._resolve_mime_type(original_filename, content_type)
        if mime_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
            return fallback
        prompt = self._build_prompt(user, session, inbound_text, recent_messages)
        data_url = f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"
        for model in self.models.candidate_models():
            try:
                response = await self.client.responses.create(
                    model=model,
                    input=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": prompt},
                                {"type": "input_image", "image_url": data_url},
                            ],
                        }
                    ],
                )
            except Exception:
                continue
            parsed = self._parse_response(response.output_text, original_filename)
            if parsed:
                self.models.record_success(model)
                self.preferred_model = model
                parsed.analysis_model = model
                return parsed
        return fallback

    def build_storage_filename(
        self,
        *,
        timestamp_prefix: str,
        index: int,
        original_filename: str,
        insight: ImageInsight,
    ) -> str:
        suffix = Path(original_filename).suffix.lower() or ".bin"
        safe_slug = _safe_slug(insight.slug) or _safe_slug(Path(original_filename).stem) or "image"
        safe_slug = safe_slug[:48].strip("-") or "image"
        return f"{timestamp_prefix}_{index}_{safe_slug}{suffix}"

    def _build_prompt(
        self,
        user: UserProfile,
        session: SessionState,
        inbound_text: str,
        recent_messages: list[MessageRecord],
    ) -> str:
        recent_text = "\n".join(
            f"- {message.content.strip()}"
            for message in recent_messages[-4:]
            if message.direction == "inbound" and message.content.strip()
        )
        return (
            "You are labeling an intern progress image for a local archive.\n"
            "Use the image plus the surrounding work context.\n"
            "Return strict JSON with keys slug, description, tags.\n"
            "Rules:\n"
            "- slug: 2 to 6 lowercase words, concise, noun-heavy, suitable for a filename.\n"
            "- description: one short sentence describing what is visible.\n"
            "- tags: 3 to 5 short lowercase tags.\n"
            "- avoid generic tags like photo, image, project unless absolutely necessary.\n"
            "- be concrete and visual.\n\n"
            f"Intern: {user.display_name}\n"
            f"Stage: {session.stage}\n"
            f"Latest plan: {session.latest_plan or 'n/a'}\n"
            f"Latest status: {session.latest_status or 'n/a'}\n"
            f"Latest blocker: {session.latest_blocker or 'n/a'}\n"
            f"Current message text: {inbound_text or 'n/a'}\n"
            f"Recent inbound context:\n{recent_text or '- none'}"
        )

    def _parse_response(self, text: str, original_filename: str) -> ImageInsight | None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                return None
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        slug = str(payload.get("slug") or "").strip()
        description = str(payload.get("description") or "").strip()
        tags = [str(tag).strip().lower() for tag in payload.get("tags", []) if str(tag).strip()]
        if not slug or not description:
            return None
        normalized_tags = []
        for tag in tags:
            cleaned = re.sub(r"[^a-z0-9- ]+", "", tag).strip().replace(" ", "-")
            if cleaned and cleaned not in normalized_tags:
                normalized_tags.append(cleaned[:24])
        return ImageInsight(
            slug=_safe_slug(slug) or _safe_slug(Path(original_filename).stem) or "image",
            description=description[:240],
            tags=normalized_tags[:5],
        )

    def _fallback_insight(self, original_filename: str) -> ImageInsight:
        stem = Path(original_filename).stem
        slug = _safe_slug(stem) or "image"
        words = [word for word in slug.split("-") if word]
        description_words = " ".join(words[:5]) if words else "uploaded image"
        description = f"Archive image related to {description_words}." if description_words else "Archive image."
        tags = words[:4] or ["uploaded", "image"]
        return ImageInsight(slug=slug[:48], description=description[:240], tags=tags[:5], analysis_model=None)

    def _resolve_mime_type(self, original_filename: str, content_type: str | None) -> str | None:
        if content_type and content_type.startswith("image/"):
            return content_type
        guessed, _ = mimetypes.guess_type(original_filename)
        return guessed


def build_image_manifest(messages: list[MessageRecord]) -> list[dict[str, object]]:
    manifest: list[dict[str, object]] = []
    for message in messages:
        for attachment in message.attachments:
            if not attachment.local_path:
                continue
            manifest.append(
                {
                    "message_id": message.message_id,
                    "direction": message.direction,
                    "created_at": message.created_at.isoformat(),
                    "content_excerpt": (message.content or "")[:160],
                    "filename": attachment.filename,
                    "original_filename": attachment.original_filename,
                    "local_path": attachment.local_path,
                    "content_type": attachment.content_type,
                    "size": attachment.size,
                    "description": attachment.description,
                    "tags": attachment.tags,
                    "analysis_model": attachment.analysis_model,
                }
            )
    return manifest


def _safe_slug(value: str) -> str:
    lowered = value.lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered).strip("-")
    return lowered

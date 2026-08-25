from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .models import SessionState


class MetadataKey(StrEnum):
    ACTIVE_TASK_ID = "active_clickup_task_id"
    ACTIVE_TASK_NAME = "active_clickup_task_name"
    CLICKUP_SELECTION_REASON = "clickup_selection_reason"
    PENDING_ADMIN_REVIEWS = "pending_admin_reviews"
    LEGACY_PENDING_ADMIN_REVIEW = "pending_admin_review"
    CLICKUP_PROMPT = "clickup_prompt"
    SELF_LOOKUP_PROMPT = "self_lookup_prompt"
    DAY_SUPPRESSION_PROMPT = "day_suppression_prompt"
    DAY_SUPPRESSION = "day_suppression"
    BLOCKER_STATE = "blocker_state"
    LUNCH_WINDOWS = "lunch_windows"


@dataclass(slots=True)
class SessionMetadata:
    session: SessionState

    @property
    def values(self) -> dict[str, Any]:
        return self.session.metadata

    @property
    def active_task_id(self) -> str | None:
        value = self.values.get(MetadataKey.ACTIVE_TASK_ID)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @property
    def active_task_name(self) -> str | None:
        value = self.values.get(MetadataKey.ACTIVE_TASK_NAME)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def clear_active_task(self) -> None:
        self.values.pop(MetadataKey.ACTIVE_TASK_ID, None)
        self.values.pop(MetadataKey.ACTIVE_TASK_NAME, None)
        self.values.pop(MetadataKey.CLICKUP_SELECTION_REASON, None)

    def pending_admin_reviews(self) -> list[dict[str, Any]]:
        raw = self.values.get(MetadataKey.PENDING_ADMIN_REVIEWS)
        reviews = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        legacy = self.values.get(MetadataKey.LEGACY_PENDING_ADMIN_REVIEW)
        if isinstance(legacy, dict) and legacy not in reviews:
            reviews.append(legacy)
        return reviews

    def set_pending_admin_reviews(self, reviews: list[dict[str, Any]]) -> None:
        normalized = [review for review in reviews if isinstance(review, dict)]
        if normalized:
            self.values[MetadataKey.PENDING_ADMIN_REVIEWS] = normalized
            self.values.pop(MetadataKey.LEGACY_PENDING_ADMIN_REVIEW, None)
            return
        self.values.pop(MetadataKey.PENDING_ADMIN_REVIEWS, None)
        self.values.pop(MetadataKey.LEGACY_PENDING_ADMIN_REVIEW, None)

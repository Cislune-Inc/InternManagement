from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from .signals import detect_signals


class SlackUpdatePolicy:
    def __init__(self, self_lookup_classifier: Callable[[str], str | None]) -> None:
        self._self_lookup_classifier = self_lookup_classifier

    def is_low_signal(self, text: str) -> bool:
        normalized = " ".join(text.strip().lower().split())
        return normalized in {
            "ok",
            "okay",
            "yes",
            "no",
            "done",
            "im done",
            "i'm done",
            "still working",
            "working on it",
            "same as before",
        }

    def is_workflow_chatter(self, text: str) -> bool:
        normalized = self.normalize(text)
        if not normalized:
            return True
        if self._self_lookup_classifier(text):
            return True
        if normalized in {
            "hours",
            "status",
            "today",
            "this week",
            "last week",
            "whole summer",
            "clock in",
            "clock me in",
            "clock out",
            "clock me out",
            "create task",
            "create new task",
            "switch task",
            "yes",
            "no",
            "cancel",
            "back",
        }:
            return True
        signals = detect_signals(text)
        if (
            signals.clocking_out
            or signals.starting_lunch
            or signals.ending_lunch
            or signals.starting_short_rest
            or signals.ending_short_rest
        ):
            return True
        return bool(
            re.search(
                r"\b(?:take|taking|start|starting|end|ending|done with|back from|put me on)\s+(?:my\s+)?(?:lunch|break)\b",
                normalized,
            )
            or re.search(r"\b(?:clock|break)\b.*\b(?:pollo|bot)\b", normalized)
        )

    def is_interesting(self, text: str) -> bool:
        normalized = " ".join(text.strip().split())
        if len(normalized) < 18:
            return False
        if self._is_vague_progress(normalized):
            return False
        return not (
            self.is_low_signal(normalized)
            or self.is_workflow_chatter(normalized)
        )

    def fingerprint(self, text: str) -> str:
        normalized = self.normalize(text)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def normalize(self, text: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", text.lower()))

    def _is_vague_progress(self, text: str) -> bool:
        normalized = self.normalize(text)
        return bool(
            re.fullmatch(
                r"(?:the\s+)?project\s+(?:is\s+)?(?:going|moving)\s+(?:good|great|well|forward)\.?",
                normalized,
            )
            or re.fullmatch(
                r"(?:(?:i\s+am|im|we\s+are|were)\s+)?(?:still\s+)?(?:working|making progress)(?:\s+on\s+(?:it|the project))?(?:\s+for\s+the\s+project)?\.?",
                normalized,
            )
            or re.fullmatch(
                r"(?:everything|things|it)\s+(?:is|are)\s+(?:going\s+)?(?:good|great|well|fine)\.?",
                normalized,
            )
        )

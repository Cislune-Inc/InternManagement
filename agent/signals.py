from __future__ import annotations

import re
from dataclasses import dataclass


YES_PATTERNS = [
    r"\bclocked in\b",
    r"\bi clocked in\b",
    r"\byes\b",
    r"\byeah\b",
    r"\byep\b",
    r"\bi'?m in\b",
]

CLOCK_OUT_PATTERNS = [
    r"\bclocking out\b",
    r"\bclocked out\b",
    r"\blogging off\b",
    r"\bdone for the day\b",
    r"\bend of day\b",
]

STUCK_PATTERNS = [
    r"\bstuck\b",
    r"\bblocked\b",
    r"\bcan'?t\b",
    r"\bcannot\b",
    r"\bneed help\b",
    r"\bnot sure how\b",
]

LUNCH_START_PATTERNS = [
    r"\bgoing to lunch\b",
    r"\bgoing to eat lunch\b",
    r"\btaking lunch\b",
    r"\btake lunch\b",
    r"\beating lunch\b",
    r"\beat lunch\b",
    r"\bwant to eat lunch\b",
    r"\bwant lunch\b",
    r"\blunch break\b",
    r"\bgrabbing lunch\b",
    r"\bgrab lunch\b",
    r"\bheading to lunch\b",
    r"\bon lunch\b",
]

LUNCH_END_PATTERNS = [
    r"\bdone with lunch\b",
    r"\bback from lunch\b",
    r"\blunch is over\b",
    r"\bfinished lunch\b",
    r"\bending lunch\b",
    r"\bi'?m back\b",
]

RECOVERED_PATTERNS = [
    r"\bunblocked\b",
    r"\bfixed\b",
    r"\bresolved\b",
    r"\bback on track\b",
    r"\bfigured it out\b",
    r"\bresume(?:d|ing)?\b",
    r"\bback to work\b",
    r"\bback at it\b",
    r"\bcan continue\b",
    r"\bready to continue\b",
]


@dataclass(slots=True)
class MessageSignals:
    clocked_in: bool = False
    clocking_out: bool = False
    stuck: bool = False
    recovered: bool = False
    starting_lunch: bool = False
    ending_lunch: bool = False


def detect_signals(text: str) -> MessageSignals:
    normalized = text.lower()
    return MessageSignals(
        clocked_in=_matches_any(normalized, YES_PATTERNS),
        clocking_out=_matches_any(normalized, CLOCK_OUT_PATTERNS),
        stuck=_matches_any(normalized, STUCK_PATTERNS),
        recovered=_matches_any(normalized, RECOVERED_PATTERNS),
        starting_lunch=_matches_any(normalized, LUNCH_START_PATTERNS),
        ending_lunch=_matches_any(normalized, LUNCH_END_PATTERNS),
    )


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)

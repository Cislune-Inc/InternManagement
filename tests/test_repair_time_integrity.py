from __future__ import annotations

from agent.models import SessionState
from ops.repair_time_integrity import (
    is_legacy_migration_candidate,
    merge_overlapping_segments,
)


def test_merge_overlapping_segments_preserves_union_and_gaps() -> None:
    merged, changed = merge_overlapping_segments(
        [
            {
                "clocked_in_at": "2026-08-10T09:00:00-07:00",
                "clocked_out_at": "2026-08-10T12:00:00-07:00",
            },
            {
                "clocked_in_at": "2026-08-10T13:00:00-07:00",
                "clocked_out_at": "2026-08-10T17:00:00-07:00",
            },
            {
                "clocked_in_at": "2026-08-10T09:00:00-07:00",
                "clocked_out_at": "2026-08-10T17:15:00-07:00",
            },
        ]
    )

    assert changed is True
    assert merged == [
        {
            "clocked_in_at": "2026-08-10T09:00:00-07:00",
            "clocked_out_at": "2026-08-10T17:15:00-07:00",
        }
    ]


def test_merge_overlapping_segments_leaves_nonoverlapping_intervals_alone() -> None:
    original = [
        {
            "clocked_in_at": "2026-08-10T09:00:00-07:00",
            "clocked_out_at": "2026-08-10T12:00:00-07:00",
        },
        {
            "clocked_in_at": "2026-08-10T13:00:00-07:00",
            "clocked_out_at": "2026-08-10T17:00:00-07:00",
        },
    ]

    merged, changed = merge_overlapping_segments(original)

    assert changed is False
    assert merged == original


def test_legacy_migration_candidate_requires_known_seed_signature() -> None:
    session = SessionState(
        user_key="alex",
        session_date="2026-08-05",
        metadata={
            "latest_manual_time_edit": {
                "edited_by": "g",
                "reason": "g",
            }
        },
        time_summary={"task_tracked_total_seconds": 0},
    )

    assert is_legacy_migration_candidate(session, "2026-08-05") is True
    session.metadata["time_record_origin"] = "live"
    assert is_legacy_migration_candidate(session, "2026-08-05") is False

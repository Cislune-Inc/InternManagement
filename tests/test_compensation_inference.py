from __future__ import annotations

from ops.infer_compensation_plans import (
    build_proposals,
    infer_plan,
    uncertain_proposals,
)


def test_compensation_inference_applies_only_payroll_grade_evidence() -> None:
    hourly = infer_plan(
        {
            "worker_type": "employee",
            "compensation_plan": "needs_review",
            "gusto_entity_uuid": "gusto-1",
        },
        {},
        {},
    )
    stipend_guess = infer_plan(
        {"worker_type": "intern", "compensation_plan": "needs_review"},
        {"slack_title": "NASA stipend intern"},
        {},
    )
    unknown = infer_plan(
        {"worker_type": "intern", "compensation_plan": "needs_review"},
        {"slack_title": "Mechanical intern"},
        {},
    )

    assert hourly == (
        "cislune_hourly",
        "high",
        "existing Gusto employee mapping",
    )
    assert stipend_guess[0:2] == ("nasa_stipend", "medium")
    assert unknown[0:2] == ("needs_review", "low")


def test_compensation_proposals_can_surface_only_uncertain_classifications() -> None:
    proposals = build_proposals(
        [
            {
                "user_key": "confirmed",
                "active": "true",
                "compensation_plan": "cislune_hourly",
            },
            {
                "user_key": "candidate",
                "active": "true",
                "compensation_plan": "needs_review",
            },
        ],
        [
            {
                "roster_user_key": "candidate",
                "slack_title": "NASA stipend intern",
            }
        ],
        {},
    )

    review_rows = uncertain_proposals(proposals)

    assert proposals[0]["requires_confirmation"] == "false"
    assert review_rows == [proposals[1]]
    assert review_rows[0]["proposed_plan"] == "nasa_stipend"

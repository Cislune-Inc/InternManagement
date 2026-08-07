from __future__ import annotations

from ops.apply_production_roster_controls import apply_roster_controls


def test_roster_controls_track_every_active_worker_and_preserve_pay_separation() -> None:
    rows = [
        {
            "user_key": "hourly",
            "active": "true",
            "worker_type": "employee",
            "gusto_entity_uuid": "gusto-1",
            "time_tracking_required": "false",
            "meal_tracking_required": "false",
            "overtime_approval_required": "false",
        },
        {
            "user_key": "intern",
            "active": "true",
            "worker_type": "intern",
            "gusto_entity_uuid": "",
            "compensation_plan": "nasa_stipend",
        },
        {
            "user_key": "inactive",
            "active": "false",
            "worker_type": "intern",
            "time_tracking_required": "false",
        },
    ]

    fieldnames, changed = apply_roster_controls(
        rows,
        ["user_key", "active", "worker_type", "gusto_entity_uuid"],
    )

    assert "compensation_plan" in fieldnames
    assert rows[0]["compensation_plan"] == "cislune_hourly"
    assert rows[1]["compensation_plan"] == "nasa_stipend"
    for row in rows[:2]:
        assert row["work_location"] == "Rosemead, CA"
        assert row["labor_jurisdiction"] == "California"
        assert row["time_tracking_required"] == "true"
        assert row["meal_tracking_required"] == "true"
        assert row["overtime_approval_required"] == "true"
    assert rows[2]["time_tracking_required"] == "false"
    assert changed


def test_roster_controls_do_not_guess_nasa_stipend_without_operator_classification() -> None:
    rows = [
        {
            "user_key": "intern",
            "active": "true",
            "worker_type": "intern",
            "gusto_entity_uuid": "",
        }
    ]

    apply_roster_controls(rows, list(rows[0]))

    assert rows[0]["compensation_plan"] == "needs_review"


def test_roster_controls_keep_exempt_worker_out_of_overtime_enforcement() -> None:
    rows = [
        {
            "user_key": "salary",
            "active": "true",
            "worker_type": "salaried",
            "gusto_entity_uuid": "gusto-salary",
        }
    ]

    apply_roster_controls(rows, list(rows[0]))

    assert rows[0]["compensation_plan"] == "salary"
    assert rows[0]["overtime_approval_required"] == "false"


def test_roster_controls_connect_aj_to_slack_and_preserve_existing_focus() -> None:
    rows = [
        {
            "user_key": "AJ",
            "display_name": "AJ Torres",
            "active": "true",
            "worker_type": "intern",
            "compensation_plan": "nasa_stipend",
            "interests": "materials testing",
        }
    ]

    fieldnames, changed = apply_roster_controls(rows, list(rows[0]))

    assert rows[0]["slack_user_id"] == "U095NMY2U4R"
    assert rows[0]["preferred_transport"] == "slack"
    assert rows[0]["interests"] == (
        "materials testing;Lockheed Bagworm;LM_Nightjar;shop organization"
    )
    assert {"slack_user_id", "preferred_transport", "interests"}.issubset(fieldnames)
    assert changed

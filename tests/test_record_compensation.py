from __future__ import annotations

import runpy
from pathlib import Path


MODULE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "ops" / "record-compensation.py"),
    run_name="record_compensation",
)
record_classification = MODULE["record_classification"]


def test_record_classification_normalizes_cli_friendly_values() -> None:
    key, overrides = record_classification(
        {"U123": {"worker_type": "intern"}},
        user_key="yoli",
        slack_user_id="u123",
        plan="cislune-hourly",
        evidence="slack-explicit-hourly-pay",
        reviewed_by="Erik",
        reviewed_at="2026-08-06T12:00:00+00:00",
    )

    assert key == "U123"
    assert overrides["U123"] == {
        "worker_type": "intern",
        "roster_user_key": "yoli",
        "compensation_plan": "cislune_hourly",
        "compensation_evidence": "slack-explicit-hourly-pay",
        "compensation_reviewed_at": "2026-08-06T12:00:00+00:00",
        "compensation_reviewed_by": "Erik",
    }


def test_record_classification_can_use_roster_key_without_slack() -> None:
    key, overrides = record_classification(
        {},
        user_key="example",
        slack_user_id="",
        plan="nasa-stipend",
        evidence="signed-stipend-agreement",
        reviewed_by="Erik",
        reviewed_at="2026-08-06T12:00:00+00:00",
    )

    assert key == "example"
    assert overrides[key]["compensation_plan"] == "nasa_stipend"

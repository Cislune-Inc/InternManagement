from __future__ import annotations

from ops.apply_production_controls import apply_controls


def test_production_controls_disable_practice_channel() -> None:
    payload = {
        "slack": {
            "practice_channel_id": "C0B6JU2PQ8N",
        }
    }

    changed = apply_controls(payload)

    assert payload["slack"]["practice_channel_id"] is None
    assert "slack.practice_channel_id" in changed


def test_production_controls_enforce_ten_minute_short_rest_limit() -> None:
    payload: dict = {}

    changed = apply_controls(payload)

    assert payload["labor"]["short_rest_break_minutes"] == 10
    assert "labor.short_rest_break_minutes" in changed


def test_production_controls_use_four_hour_inactivity_backstop() -> None:
    payload: dict = {}

    changed = apply_controls(payload)

    assert payload["schedule"]["auto_clock_out_after_hours"] == 4
    assert payload["schedule"]["auto_clock_out_warning_minutes"] == 15
    assert "schedule.auto_clock_out_after_hours" in changed


def test_production_controls_schedule_daily_pacific_digest_and_meal_minimum() -> None:
    payload: dict = {}

    changed = apply_controls(payload)

    assert payload["slack"]["operational_digest_interval_minutes"] == 1440
    assert payload["slack"]["operational_digest_hour"] == 8
    assert payload["slack"]["operational_digest_timezone"] == "America/Los_Angeles"
    assert payload["labor"]["meal_minimum_minutes"] == 30
    assert "slack.operational_digest_hour" in changed


def test_production_controls_do_not_reenroll_old_workers_or_reset_url() -> None:
    payload: dict = {"slack": {"worker_portal_beta_slack_user_ids": ["CURRENT"],
                              "manager_queue_url": "http://192.168.40.177:8765/exceptions"}}

    changed = apply_controls(payload)

    assert payload["slack"]["worker_portal_beta_slack_user_ids"] == ["CURRENT"]
    assert "slack.worker_portal_beta_slack_user_ids" not in changed
    assert "slack.manager_queue_url" not in changed


def test_verified_url_is_explicit_and_credentials_rejected():
    import pytest
    payload = {}
    apply_controls(payload, base_url="http://192.168.40.177:8765/")
    assert payload["slack"]["manager_queue_url"] == "http://192.168.40.177:8765/exceptions"
    for bad in ["http://user:secret@host", "http://host/?token=secret", "http://host/path", "javascript:alert(1)"]:
        untouched = {}
        with pytest.raises(ValueError):
            apply_controls(untouched, base_url=bad)
        assert untouched == {}


def test_production_controls_configure_management_slack_admins() -> None:
    payload = {
        "admins": [
            {"name": "George", "discord_user_id": 100},
            {"name": "Erik", "discord_user_id": 200},
        ]
    }

    changed = apply_controls(payload)

    assert payload["admins"][0]["slack_user_id"] == "U0AEC5J2SJD"
    assert payload["admins"][1]["slack_user_id"] == "U01SWQKDTBM"
    assert "admins[0].slack_user_id" in changed
    assert "admins[1].slack_user_id" in changed


def test_production_controls_preserve_existing_erik_slack_admin_mapping() -> None:
    payload = {
        "admins": [
            {
                "name": "Erik",
                "discord_user_id": 200,
                "slack_user_id": "U01SWQKDTBM",
            }
        ]
    }

    changed = apply_controls(payload)

    assert payload["admins"][0]["slack_user_id"] == "U01SWQKDTBM"
    assert "admins[0].slack_user_id" not in changed


def test_production_controls_configure_legacy_erik_admin_mapping() -> None:
    payload = {
        "admins": [],
        "admin_display_name": "Erik",
    }

    changed = apply_controls(payload)

    assert payload["admin_slack_user_id"] == "U01SWQKDTBM"
    assert "admin_slack_user_id" in changed

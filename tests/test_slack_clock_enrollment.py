import json

import pytest

from ops.enable_slack_clock_beta import prepare


def test_primary_admin_only_never_enrolls_secondary_admin_or_legacy_workers():
    config = payload()
    config["admins"] = [{"name": "Erik", "discord_user_id": 1, "slack_user_id": "ERIK"},
                        {"name": "George", "discord_user_id": 2, "slack_user_id": "GEORGE"}]
    updated, excluded = prepare(config, roster({"user_key": "old", "slack_user_id": "OLD"}), None,
                                primary_admin_only=True)
    assert updated["slack"]["work_intake_beta_slack_user_ids"] == ["ERIK"]
    assert excluded == ["old"]


def test_primary_authorization_owner_changed_only_to_existing_verified_admin():
    config = payload()
    config["admin_discord_user_id"] = 2
    config["admins"] = [{"name": "Erik", "discord_user_id": 1, "slack_user_id": "ERIK"},
                        {"name": "George", "discord_user_id": 2, "slack_user_id": "GEORGE"}]
    updated, _ = prepare(config, roster(), None, primary_admin_only=True, primary_admin_slack_id="ERIK")
    assert config["admin_discord_user_id"] == 2
    assert updated["admin_discord_user_id"] == 1
    assert updated["slack"]["work_intake_beta_slack_user_ids"] == ["ERIK"]
    with pytest.raises(ValueError, match="verified admin"):
        prepare(config, roster(), None, primary_admin_slack_id="STRANGER")


def payload():
    return {"admin_discord_user_id": "1", "admin_slack_user_id": "ERIK", "roster_file_name": "roster.json", "clickup": {"workspace_id": "unused"}}


def roster(*rows):
    return json.dumps(rows).encode()


def test_prepare_is_nonmutating_and_sets_one_slack_clock():
    config = payload()
    updated, excluded = prepare(config, roster({"user_key": "w", "slack_user_id": "WORKER"}), ["w"])
    assert "slack" not in config
    assert updated["slack"]["work_intake_beta_slack_user_ids"] == ["ERIK", "WORKER"]
    assert not updated["slack"]["daily_updates_enabled"]
    assert excluded == []


def test_prepare_blocks_missing_and_duplicate_slack_mapping():
    with pytest.raises(ValueError, match="Map these active workers"):
        prepare(payload(), roster({"user_key": "w", "discord_user_id": 2}), ["w"])
    with pytest.raises(ValueError, match="Duplicate Slack identity"):
        prepare(payload(), roster({"user_key": "w", "slack_user_id": "WORKER"}, {"user_key": "other", "slack_user_id": "WORKER"}), None)


def test_limited_beta_explicitly_lists_excluded_workers():
    updated, excluded = prepare(payload(), roster({"user_key": "w", "slack_user_id": "WORKER"}, {"user_key": "later", "discord_user_id": 3}), ["w"])
    assert excluded == ["later"]
    assert updated["slack"]["work_intake_beta_slack_user_ids"] == ["ERIK", "WORKER"]


def test_no_implicit_all_active_or_secondary_admin_enrollment():
    config = payload()
    config["admins"] = [{"name": "Owner", "discord_user_id": 1, "slack_user_id": "ERIK"},
                        {"name": "Manager", "discord_user_id": 2, "slack_user_id": "MANAGER"}]
    data = roster({"user_key": "w", "slack_user_id": "WORKER"})
    with pytest.raises(ValueError, match="explicit"):
        prepare(config, data, None)
    updated, _ = prepare(config, data, ["w"])
    assert updated["slack"]["work_intake_beta_slack_user_ids"] == ["ERIK", "WORKER"]

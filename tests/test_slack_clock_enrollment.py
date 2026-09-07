import json

import pytest

from ops.enable_slack_clock_beta import prepare


def payload():
    return {"admin_discord_user_id": "1", "admin_slack_user_id": "ERIK", "roster_file_name": "roster.json", "clickup": {"workspace_id": "unused"}}


def roster(*rows):
    return json.dumps(rows).encode()


def test_prepare_is_nonmutating_and_sets_one_slack_clock():
    config = payload()
    updated, excluded = prepare(config, roster({"user_key": "w", "slack_user_id": "WORKER"}), None)
    assert "slack" not in config
    assert updated["slack"]["work_intake_beta_slack_user_ids"] == ["ERIK", "WORKER"]
    assert not updated["slack"]["daily_updates_enabled"]
    assert excluded == []


def test_prepare_blocks_missing_and_duplicate_slack_mapping():
    with pytest.raises(ValueError, match="Map these active workers"):
        prepare(payload(), roster({"user_key": "w", "discord_user_id": 2}), None)
    with pytest.raises(ValueError, match="Duplicate Slack identity"):
        prepare(payload(), roster({"user_key": "w", "slack_user_id": "WORKER"}, {"user_key": "other", "slack_user_id": "WORKER"}), None)


def test_limited_beta_explicitly_lists_excluded_workers():
    updated, excluded = prepare(payload(), roster({"user_key": "w", "slack_user_id": "WORKER"}, {"user_key": "later", "discord_user_id": 3}), ["w"])
    assert excluded == ["later"]
    assert updated["slack"]["work_intake_beta_slack_user_ids"] == ["ERIK", "WORKER"]

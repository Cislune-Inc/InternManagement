import pytest

from ops.configure_work_checkins import prepare


def config():
    return {"admin_discord_user_id": "1", "clickup": {"workspace_id": "unused"},
            "slack": {"work_intake_beta_slack_user_ids": ["OWNER"]}}


def test_interaction_rollout_never_enrolls_or_changes_original():
    source = config()
    result = prepare(source, ["grasp=C123456789"], True)
    assert result["slack"]["progress_checkins_enabled"]
    assert result["slack"]["work_summary_channels"] == {"grasp": "C123456789"}
    assert result["slack"]["work_intake_beta_slack_user_ids"] == ["OWNER"]
    assert source == config()


@pytest.mark.parametrize("route", ["unknown=C123456789", "grasp=U123456789", "grasp=#general", "grasp="])
def test_invalid_or_unreviewed_routes_fail_closed(route):
    with pytest.raises(ValueError):
        prepare(config(), [route], True)


def test_existing_route_cannot_be_silently_redirected():
    source = prepare(config(), ["grasp=C123456789"], True)
    with pytest.raises(ValueError, match="Destination changes"):
        prepare(source, ["grasp=C987654321"], True)

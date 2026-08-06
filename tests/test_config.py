from agent.config import parse_agent_config, parse_roster_bytes
import pytest


def test_parse_agent_config_defaults() -> None:
    config = parse_agent_config(
        {
            "admin_discord_user_id": "123",
            "clickup": {"workspace_id": "456", "mission_board_list_id": "789"},
            "prompts": {},
        },
        "America/Los_Angeles",
    )
    assert config.timezone == "America/Los_Angeles"
    assert config.schedule.clock_in_hour == 9
    assert config.schedule.follow_up_interval_minutes == 120
    assert config.schedule.task_onboarding_interval_minutes == 5
    assert config.schedule.auto_clock_out_after_hours == 6
    assert config.schedule.workday_rollover_time == "03:30"
    assert config.labor.short_rest_break_minutes == 10
    assert config.admins[0].discord_user_id == 123
    assert config.admin_console.enable_ai_fallback is True
    assert config.admin_console.menu_timeout_minutes == 10
    assert config.clickup.workspace_id == "456"
    assert config.clickup.mission_board_list_id == "789"
    assert config.clickup.auto_status_updates is True
    assert config.clickup.create_time_entries is True
    assert config.slack.enabled is False
    assert config.prompts.lunch_break_check_in == "Are you done with lunch break yet?"


def test_parse_agent_config_accepts_follow_up_minutes() -> None:
    config = parse_agent_config(
        {
            "admin_discord_user_id": "123",
            "schedule": {"follow_up_interval_minutes": 30},
            "clickup": {"workspace_id": "456"},
            "prompts": {},
        },
        "America/Los_Angeles",
    )
    assert config.schedule.follow_up_interval_minutes == 30


def test_parse_agent_config_accepts_workday_rollover_time() -> None:
    config = parse_agent_config(
        {
            "admin_discord_user_id": "123",
            "schedule": {"workday_rollover_time": "04:15"},
            "clickup": {"workspace_id": "456"},
            "prompts": {},
        },
        "America/Los_Angeles",
    )
    assert config.schedule.workday_rollover_time == "04:15"


def test_parse_agent_config_accepts_named_admins() -> None:
    config = parse_agent_config(
        {
            "admin_discord_user_id": "123",
            "admins": [
                {"name": "George", "discord_user_id": "123"},
                {"name": "Casey", "discord_user_id": "456", "clickup_user_id": "77"},
            ],
            "clickup": {"workspace_id": "456"},
            "prompts": {},
        },
        "America/Los_Angeles",
    )
    assert [admin.name for admin in config.admins] == ["George", "Casey"]
    assert config.admins[1].clickup_user_id == "77"


def test_parse_agent_config_accepts_admin_console_settings() -> None:
    config = parse_agent_config(
        {
            "admin_discord_user_id": "123",
            "admin_console": {"enable_ai_fallback": True, "menu_timeout_minutes": 15},
            "clickup": {"workspace_id": "456"},
            "prompts": {},
        },
        "America/Los_Angeles",
    )
    assert config.admin_console.enable_ai_fallback is True
    assert config.admin_console.menu_timeout_minutes == 15


def test_parse_agent_config_accepts_slack_settings() -> None:
    config = parse_agent_config(
        {
            "admin_discord_user_id": "123",
            "clickup": {"workspace_id": "456"},
            "prompts": {},
            "slack": {
                "enabled": True,
                "practice_channel_id": "CTEST",
                "default_channel_id": "CDEFAULT",
                "unmapped_channel_id": "CUNMAPPED",
                "post_start_hour": 11,
                "post_end_hour": 16,
                "min_post_interval_minutes": 30,
                "project_routes": [
                    {
                        "label": "PERDEX",
                        "channel_id": "CPERDEX",
                        "clickup_task_ids": ["task-1"],
                        "clickup_list_ids": ["list-1"],
                        "clickup_folder_ids": ["folder-1"],
                        "content_patterns": ["fermentation"],
                        "task_name_patterns": ["solar"],
                        "list_name_patterns": ["perdex"],
                        "folder_name_patterns": ["contract"],
                    }
                ],
            },
        },
        "America/Los_Angeles",
    )
    assert config.slack.enabled is True
    assert config.slack.practice_channel_id == "CTEST"
    assert config.slack.default_channel_id == "CDEFAULT"
    assert config.slack.unmapped_channel_id == "CUNMAPPED"
    assert config.slack.post_start_hour == 11
    assert config.slack.post_end_hour == 16
    assert config.slack.min_post_interval_minutes == 30
    route = config.slack.project_routes[0]
    assert route.channel_id == "CPERDEX"
    assert route.clickup_task_ids == ["task-1"]
    assert route.clickup_list_ids == ["list-1"]
    assert route.clickup_folder_ids == ["folder-1"]
    assert route.content_patterns == ["fermentation"]
    assert route.task_name_patterns == ["solar"]
    assert route.list_name_patterns == ["perdex"]
    assert route.folder_name_patterns == ["contract"]


def test_parse_agent_config_accepts_lunch_prompt() -> None:
    config = parse_agent_config(
        {
            "admin_discord_user_id": "123",
            "prompts": {"lunch_break_check_in": "You back from lunch yet?"},
            "clickup": {"workspace_id": "456"},
        },
        "America/Los_Angeles",
    )
    assert config.prompts.lunch_break_check_in == "You back from lunch yet?"


def test_parse_agent_config_rejects_empty_follow_up_questions() -> None:
    with pytest.raises(ValueError, match="follow_up_questions"):
        parse_agent_config(
            {
                "admin_discord_user_id": "123",
                "prompts": {"follow_up_questions": []},
                "clickup": {"workspace_id": "456"},
            },
            "America/Los_Angeles",
        )


def test_parse_agent_config_rejects_blank_only_follow_up_questions() -> None:
    with pytest.raises(ValueError, match="follow_up_questions"):
        parse_agent_config(
            {
                "admin_discord_user_id": "123",
                "prompts": {"follow_up_questions": [" ", "\t"]},
                "clickup": {"workspace_id": "456"},
            },
            "America/Los_Angeles",
        )


def test_parse_agent_config_accepts_custom_follow_up_questions() -> None:
    config = parse_agent_config(
        {
            "admin_discord_user_id": "123",
            "prompts": {"follow_up_questions": ["  What changed?  ", "Any blockers?"]},
            "clickup": {"workspace_id": "456"},
        },
        "America/Los_Angeles",
    )
    assert config.prompts.follow_up_questions == ["What changed?", "Any blockers?"]


def test_parse_roster_csv() -> None:
    roster = parse_roster_bytes(
        "roster.csv",
        (
            "user_key,display_name,discord_user_id,discord_username,storage_folder_name,timezone,clickup_user_id,clickup_user_email,slack_user_id,active\n"
            "alex,Alex,123,alexuser,Alex Folder,America/Denver,42,alex@example.com,U123456,true\n"
        ).encode("utf-8"),
    )
    assert len(roster) == 1
    assert roster[0].discord_user_id == 123
    assert roster[0].timezone == "America/Denver"
    assert roster[0].clickup_user_id == "42"
    assert roster[0].clickup_user_email == "alex@example.com"
    assert roster[0].slack_user_id == "U123456"
    assert roster[0].storage_folder_name == "Alex Folder"


def test_parse_roster_csv_accepts_utf8_bom_header() -> None:
    roster = parse_roster_bytes(
        "roster.csv",
        (
            "\ufeffuser_key,display_name,discord_user_id,discord_username,storage_folder_name,timezone,clickup_user_id,clickup_user_email,active\n"
            "alex,Alex,123,alexuser,Alex Folder,America/Denver,42,alex@example.com,true\n"
        ).encode("utf-8"),
    )
    assert len(roster) == 1
    assert roster[0].user_key == "alex"
    assert roster[0].overtime_approval_required is True


def test_parse_roster_csv_rejects_username_in_id_column() -> None:
    with pytest.raises(ValueError, match="Invalid discord_user_id"):
        parse_roster_bytes(
            "roster.csv",
            (
                "user_key,display_name,discord_user_id,discord_username,storage_folder_name,timezone,clickup_user_id,clickup_user_email,active\n"
                "alex,Alex,not-a-user-id,alexuser,Alex Folder,America/Los_Angeles,42,alex@example.com,true\n"
            ).encode("utf-8"),
        )


def test_parse_roster_csv_accepts_slack_only_worker_policy() -> None:
    roster = parse_roster_bytes(
        "roster.csv",
        (
            "user_key,display_name,discord_user_id,discord_username,storage_folder_name,"
            "timezone,clickup_user_id,clickup_user_email,slack_user_id,active,"
            "preferred_transport,worker_type,compensation_plan,time_tracking_required,meal_tracking_required,"
            "overtime_approval_required,expected_daily_hours,check_in_interval_minutes,"
            "gusto_entity_uuid,labor_cost_rate\n"
            "sam,Sam,,,Sam,America/Los_Angeles,42,sam@example.com,U123,true,"
            "slack,employee,cislune_hourly,true,true,true,8,90,gusto-1,55.5\n"
        ).encode("utf-8"),
    )

    assert roster[0].discord_user_id is None
    assert roster[0].slack_user_id == "U123"
    assert roster[0].preferred_transport == "slack"
    assert roster[0].worker_type == "employee"
    assert roster[0].compensation_plan == "cislune_hourly"
    assert roster[0].check_in_interval_minutes == 90
    assert roster[0].gusto_entity_uuid == "gusto-1"
    assert roster[0].labor_cost_rate == 55.5


def test_parse_roster_csv_rejects_invalid_compensation_plan() -> None:
    with pytest.raises(ValueError, match="Invalid compensation_plan"):
        parse_roster_bytes(
            "roster.csv",
            (
                "user_key,display_name,discord_user_id,slack_user_id,compensation_plan,active\n"
                "sam,Sam,123,,cash_maybe,true\n"
            ).encode("utf-8"),
        )


def test_parse_roster_csv_does_not_infer_nasa_stipend_from_intern_label() -> None:
    roster = parse_roster_bytes(
        "roster.csv",
        (
            "user_key,display_name,discord_user_id,slack_user_id,worker_type,active\n"
            "sam,Sam,123,,intern,true\n"
        ).encode("utf-8"),
    )

    assert roster[0].compensation_plan == "needs_review"


def test_parse_roster_csv_requires_one_message_transport() -> None:
    with pytest.raises(ValueError, match="either a numeric discord_user_id or a Slack user ID"):
        parse_roster_bytes(
            "roster.csv",
            (
                "user_key,display_name,discord_user_id,discord_username,storage_folder_name,"
                "timezone,clickup_user_id,clickup_user_email,slack_user_id,active\n"
                "sam,Sam,,,Sam,America/Los_Angeles,42,sam@example.com,,true\n"
            ).encode("utf-8"),
        )


def test_parse_roster_csv_rejects_unsupported_clickup_override_columns() -> None:
    with pytest.raises(ValueError, match="Unsupported roster columns"):
        parse_roster_bytes(
            "roster.csv",
            (
                "user_key,display_name,discord_user_id,discord_username,storage_folder_name,clickup_user_id,clickup_user_email,clickup_task_id,clickup_list_id,active\n"
                "alex,Alex,123,alexuser,Alex Folder,42,alex@example.com,task-1,987654,true\n"
            ).encode("utf-8"),
        )


def test_parse_roster_csv_parses_active_false() -> None:
    roster = parse_roster_bytes(
        "roster.csv",
        (
            "user_key,display_name,discord_user_id,discord_username,storage_folder_name,timezone,clickup_user_id,clickup_user_email,active\n"
            "alex,Alex,123,alexuser,Alex Folder,,42,alex@example.com,false\n"
        ).encode("utf-8"),
    )
    assert roster[0].active is False


def test_parse_roster_csv_rejects_invalid_timezone() -> None:
    with pytest.raises(RuntimeError, match="Timezone"):
        parse_roster_bytes(
            "roster.csv",
            (
                "user_key,display_name,discord_user_id,discord_username,storage_folder_name,timezone,clickup_user_id,clickup_user_email,active\n"
                "alex,Alex,123,alexuser,Alex Folder,Not/A_Zone,42,alex@example.com,true\n"
            ).encode("utf-8"),
        )

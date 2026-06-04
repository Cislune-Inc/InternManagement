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
    assert config.admins[0].discord_user_id == 123
    assert config.admin_console.enable_ai_fallback is True
    assert config.admin_console.menu_timeout_minutes == 10
    assert config.clickup.workspace_id == "456"
    assert config.clickup.mission_board_list_id == "789"
    assert config.clickup.auto_status_updates is True
    assert config.clickup.create_time_entries is True
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


def test_parse_roster_csv() -> None:
    roster = parse_roster_bytes(
        "roster.csv",
        (
            "user_key,display_name,discord_user_id,discord_username,storage_folder_name,timezone,clickup_user_id,clickup_user_email,active\n"
            "alex,Alex,123,alexuser,Alex Folder,America/Denver,42,alex@example.com,true\n"
        ).encode("utf-8"),
    )
    assert len(roster) == 1
    assert roster[0].discord_user_id == 123
    assert roster[0].timezone == "America/Denver"
    assert roster[0].clickup_user_id == "42"
    assert roster[0].clickup_user_email == "alex@example.com"
    assert roster[0].storage_folder_name == "Alex Folder"


def test_parse_roster_csv_rejects_username_in_id_column() -> None:
    with pytest.raises(ValueError, match="Invalid discord_user_id"):
        parse_roster_bytes(
            "roster.csv",
            (
                "user_key,display_name,discord_user_id,discord_username,storage_folder_name,timezone,clickup_user_id,clickup_user_email,active\n"
                "alex,Alex,not-a-user-id,alexuser,Alex Folder,America/Los_Angeles,42,alex@example.com,true\n"
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

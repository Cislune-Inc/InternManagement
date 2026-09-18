from datetime import datetime

from agent.models import UserProfile
from agent.work_schedule import daily_limit_hours, regular_workdays


def test_effective_dated_alternative_schedule_policy() -> None:
    user = UserProfile(
        user_key="mac",
        display_name="Mac",
        slack_user_id="MAC",
        regular_workdays=["monday", "tuesday", "wednesday", "thursday", "friday"],
        alternative_workweek_effective_date="2026-10-19",
        alternative_workweek_daily_limit_hours=10,
        alternative_workweek_regular_workdays=["monday", "tuesday", "wednesday", "thursday"],
    )
    before = datetime.fromisoformat("2026-10-16T09:00:00-07:00")
    monday = datetime.fromisoformat("2026-10-19T09:00:00-07:00")
    friday = datetime.fromisoformat("2026-10-23T09:00:00-07:00")

    assert regular_workdays(user, before.date())[-1] == "friday"
    assert daily_limit_hours(user, before, default_hours=8) == 8
    assert regular_workdays(user, monday.date()) == ["monday", "tuesday", "wednesday", "thursday"]
    assert daily_limit_hours(user, monday, default_hours=8) == 10
    assert daily_limit_hours(user, friday, default_hours=8) == 8

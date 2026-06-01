from datetime import datetime

from agent.time_utils import effective_workday_date, localize_datetime


def test_effective_workday_date_uses_previous_day_before_rollover() -> None:
    moment = datetime.fromisoformat("2026-06-02T02:00:00-07:00")
    assert effective_workday_date(moment, "America/Los_Angeles", "03:30") == "2026-06-01"


def test_effective_workday_date_uses_current_day_after_rollover() -> None:
    moment = datetime.fromisoformat("2026-06-02T03:31:00-07:00")
    assert effective_workday_date(moment, "America/Los_Angeles", "03:30") == "2026-06-02"


def test_effective_workday_date_can_differ_by_user_timezone_for_same_utc_instant() -> None:
    utc_moment = datetime.fromisoformat("2026-06-02T08:00:00+00:00")
    pacific_date = effective_workday_date(utc_moment, "America/Los_Angeles", "03:30")
    tokyo_date = effective_workday_date(utc_moment, "Asia/Tokyo", "03:30")
    assert pacific_date == "2026-06-01"
    assert tokyo_date == "2026-06-02"
    assert localize_datetime(utc_moment, "America/Los_Angeles").isoformat().startswith("2026-06-02T01:00:00")

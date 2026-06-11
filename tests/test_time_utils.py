from datetime import datetime

from agent.time_utils import (
    effective_workday_date,
    format_admin_datetime,
    format_transcript_datetime,
    format_user_datetime,
    localize_datetime,
)


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


def test_format_admin_datetime_uses_today_and_relative_text() -> None:
    rendered = format_admin_datetime(
        "2026-06-04T13:14:38-07:00",
        reference="2026-06-04T13:36:38-07:00",
    )
    assert rendered == "today at 1:14 PM PDT (about 22 minutes ago)"


def test_format_admin_datetime_uses_yesterday_for_previous_local_day() -> None:
    rendered = format_admin_datetime(
        "2026-06-03T22:05:00-07:00",
        reference="2026-06-04T09:00:00-07:00",
        include_relative=False,
    )
    assert rendered == "yesterday at 10:05 PM PDT"


def test_format_admin_datetime_uses_weekday_month_day_for_older_dates() -> None:
    rendered = format_admin_datetime(
        "2026-05-28T09:00:00-07:00",
        reference="2026-06-04T09:00:00-07:00",
        include_relative=False,
    )
    assert rendered == "Thu, May 28 at 9:00 AM PDT"


def test_format_user_datetime_uses_target_user_timezone() -> None:
    rendered = format_user_datetime(
        "2026-06-04T20:00:00+00:00",
        "America/New_York",
        reference="2026-06-04T20:30:00+00:00",
    )
    assert rendered == "today at 4:00 PM EDT (about 30 minutes ago)"


def test_format_transcript_datetime_converts_aware_timestamp_to_pacific() -> None:
    rendered = format_transcript_datetime("2026-06-08T16:09:32+00:00")
    assert rendered == "2026-06-08 09:09:32"


def test_format_transcript_datetime_handles_pacific_date_boundary() -> None:
    rendered = format_transcript_datetime("2026-06-09T00:09:32+00:00")
    assert rendered == "2026-06-08 17:09:32"


def test_format_transcript_datetime_preserves_naive_wall_clock_value() -> None:
    rendered = format_transcript_datetime(datetime.fromisoformat("2026-06-08T09:09:32"))
    assert rendered == "2026-06-08 09:09:32"

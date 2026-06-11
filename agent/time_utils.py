from __future__ import annotations

from datetime import datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ADMIN_DISPLAY_TIMEZONE = "America/Los_Angeles"


@lru_cache(maxsize=16)
def resolve_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            f"Timezone {name!r} is unavailable on this machine. "
            "Install the 'tzdata' package or choose a valid IANA timezone name."
        ) from exc


@lru_cache(maxsize=32)
def parse_local_clock_time(value: str) -> time:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise ValueError(
            f"Invalid local clock time {text!r}. Use 24-hour HH:MM format, for example '03:30'."
        ) from exc
    return time(hour=parsed.hour, minute=parsed.minute)


def localize_datetime(moment: datetime, timezone_name: str) -> datetime:
    tz = resolve_timezone(timezone_name)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


def effective_workday_date(moment: datetime, timezone_name: str, rollover_time: str) -> str:
    local_moment = localize_datetime(moment, timezone_name)
    rollover = parse_local_clock_time(rollover_time)
    local_date = local_moment.date()
    current_minutes = (local_moment.hour * 60) + local_moment.minute
    rollover_minutes = (rollover.hour * 60) + rollover.minute
    if current_minutes < rollover_minutes:
        local_date -= timedelta(days=1)
    return local_date.isoformat()


def coerce_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_human_datetime(
    value: datetime | str | None,
    timezone_name: str,
    *,
    reference: datetime | str | None = None,
    unknown: str = "unknown",
    include_relative: bool = True,
) -> str:
    target = coerce_datetime(value)
    if target is None:
        return unknown
    reference_value = coerce_datetime(reference)
    local_target = localize_datetime(target, timezone_name)
    local_reference = localize_datetime(
        reference_value or datetime.now(tz=resolve_timezone(timezone_name)),
        timezone_name,
    )
    absolute = _format_absolute_local_datetime(local_target, local_reference)
    if not include_relative or reference_value is None:
        return absolute
    relative = _format_relative_phrase(local_target, local_reference)
    if not relative:
        return absolute
    return f"{absolute} ({relative})"


def format_admin_datetime(
    value: datetime | str | None,
    *,
    reference: datetime | str | None = None,
    unknown: str = "unknown",
    include_relative: bool = True,
) -> str:
    return format_human_datetime(
        value,
        ADMIN_DISPLAY_TIMEZONE,
        reference=reference,
        unknown=unknown,
        include_relative=include_relative,
    )


def format_user_datetime(
    value: datetime | str | None,
    timezone_name: str,
    *,
    reference: datetime | str | None = None,
    unknown: str = "unknown",
    include_relative: bool = True,
) -> str:
    return format_human_datetime(
        value,
        timezone_name,
        reference=reference,
        unknown=unknown,
        include_relative=include_relative,
    )


def format_transcript_datetime(value: datetime | str | None) -> str:
    target = coerce_datetime(value)
    if target is None:
        return "unknown"
    pacific_target = localize_datetime(target, ADMIN_DISPLAY_TIMEZONE)
    return pacific_target.strftime("%Y-%m-%d %H:%M:%S")


def _format_absolute_local_datetime(target: datetime, reference: datetime) -> str:
    time_text = target.strftime("%I:%M %p").lstrip("0")
    tz_text = target.tzname() or ""
    local_date = target.date()
    reference_date = reference.date()
    if local_date == reference_date:
        prefix = "today"
    elif local_date == reference_date - timedelta(days=1):
        prefix = "yesterday"
    else:
        date_text = f"{target.strftime('%a')}, {target.strftime('%b')} {target.day}"
        if target.year != reference.year:
            date_text += f", {target.year}"
        prefix = date_text
    return f"{prefix} at {time_text} {tz_text}".strip()


def _format_relative_phrase(target: datetime, reference: datetime) -> str | None:
    delta_seconds = int(round((reference - target).total_seconds()))
    past = delta_seconds >= 0
    seconds = abs(delta_seconds)
    if seconds >= 7 * 24 * 60 * 60:
        return None
    if seconds < 60:
        return "just now" if past else "in less than a minute"
    if seconds < 90 * 60:
        count = max(1, round(seconds / 60))
        unit = "minute" if count == 1 else "minutes"
    elif seconds < 36 * 60 * 60:
        count = max(1, round(seconds / 3600))
        unit = "hour" if count == 1 else "hours"
    else:
        count = max(1, round(seconds / 86400))
        unit = "day" if count == 1 else "days"
    if past:
        return f"about {count} {unit} ago"
    return f"in about {count} {unit}"

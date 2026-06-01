from __future__ import annotations

from datetime import datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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

"""Effective-dated worker schedule policy.

The roster keeps the current schedule intact until the configured effective
date.  Alternative-workweek straight-time treatment applies only on the
worker's selected, regularly scheduled days; other days retain the ordinary
daily threshold.
"""
from __future__ import annotations

from datetime import date, datetime

from .models import UserProfile


_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def alternative_workweek_active(user: UserProfile, local_day: date) -> bool:
    raw = str(user.alternative_workweek_effective_date or "").strip()
    if not raw:
        return False
    try:
        effective = date.fromisoformat(raw)
    except ValueError:
        return False
    return local_day >= effective


def regular_workdays(user: UserProfile, local_day: date) -> list[str]:
    if alternative_workweek_active(user, local_day) and user.alternative_workweek_regular_workdays:
        return list(user.alternative_workweek_regular_workdays)
    return list(user.regular_workdays)


def daily_limit_hours(
    user: UserProfile,
    local_now: datetime,
    *,
    default_hours: float,
) -> float:
    configured = user.alternative_workweek_daily_limit_hours
    if not configured or not alternative_workweek_active(user, local_now.date()):
        return float(default_hours)
    scheduled = {
        _WEEKDAYS[item.strip().lower()]
        for item in user.alternative_workweek_regular_workdays
        if item.strip().lower() in _WEEKDAYS
    }
    if local_now.weekday() not in scheduled:
        return float(default_hours)
    return float(configured)

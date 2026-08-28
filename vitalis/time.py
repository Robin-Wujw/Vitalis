"""Application timezone helpers for UTC storage and local-day analysis."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from vitalis.config import settings


def local_timezone() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def local_today() -> date:
    return datetime.now(timezone.utc).astimezone(local_timezone()).date()


def utc_to_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(local_timezone())


def local_day(value: datetime) -> date:
    return utc_to_local(value).date()


def local_day_utc_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=local_timezone())
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=local_timezone())
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

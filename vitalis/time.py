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


def local_sleep_window(
    sleep_day: date, bedtime: time | str | None, wake_time: time | str | None
) -> tuple[datetime, datetime] | None:
    """Return a validated local-time sleep window for a wake-date record."""
    parsed_bedtime = _clock_time(bedtime)
    parsed_wake_time = _clock_time(wake_time)
    if parsed_bedtime is None or parsed_wake_time is None:
        return None
    wake = datetime.combine(sleep_day, parsed_wake_time, tzinfo=local_timezone())
    start_day = (
        sleep_day - timedelta(days=1)
        if parsed_bedtime >= parsed_wake_time
        else sleep_day
    )
    start = datetime.combine(start_day, parsed_bedtime, tzinfo=local_timezone())
    if not 120 <= (wake - start).total_seconds() / 60 <= 960:
        return None
    return start, wake


def _clock_time(value: time | str | None) -> time | None:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        for pattern in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(value, pattern).time()
            except ValueError:
                continue
    return None

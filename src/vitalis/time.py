"""Application timezone helpers for UTC storage and local-day analysis."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def local_timezone(timezone_name: str | None = None) -> ZoneInfo:
    """Resolve an explicit zone without importing process configuration."""
    if timezone_name:
        return ZoneInfo(timezone_name)
    # Legacy callers may still use the configured zone; defer that import so
    # pure analysis modules can be imported in an unconfigured process.
    from vitalis.config import settings

    return ZoneInfo(settings.timezone)


def local_today() -> date:
    return datetime.now(timezone.utc).astimezone(local_timezone()).date()


def utc_to_local(value: datetime, timezone_name: str | None = None) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    zone = ZoneInfo(timezone_name) if timezone_name else local_timezone()
    return value.astimezone(zone)


def local_day(value: datetime, timezone_name: str | None = None) -> date:
    return utc_to_local(value, timezone_name).date()


def local_day_utc_bounds(
    day: date, timezone_name: str | None = None
) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name) if timezone_name else local_timezone()
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def local_sleep_window(
    sleep_day: date, bedtime: time | str | None, wake_time: time | str | None,
    timezone_name: str | None = None,
) -> tuple[datetime, datetime] | None:
    """Return a validated local-time sleep window for a wake-date record."""
    parsed_bedtime = _clock_time(bedtime)
    parsed_wake_time = _clock_time(wake_time)
    if parsed_bedtime is None or parsed_wake_time is None:
        return None
    zone = ZoneInfo(timezone_name) if timezone_name else local_timezone()
    wake = datetime.combine(sleep_day, parsed_wake_time, tzinfo=zone)
    start_day = (
        sleep_day - timedelta(days=1)
        if parsed_bedtime >= parsed_wake_time
        else sleep_day
    )
    start = datetime.combine(start_day, parsed_bedtime, tzinfo=zone)
    elapsed_minutes = (
        wake.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    ).total_seconds() / 60
    if not 120 <= elapsed_minutes <= 960:
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

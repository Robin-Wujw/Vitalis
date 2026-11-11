"""Validate inclusive local dates before executing a public query."""

from datetime import date, timedelta

from fastapi import HTTPException

from vitalis.time import local_day_utc_bounds, local_today


def date_window(
    start: date | None, end: date | None, *, default_days: int,
) -> tuple[date, date]:
    """Resolve defaults and require an ordered, UTC-representable window."""
    try:
        end = end or local_today()
        start = start or end - timedelta(days=default_days - 1)
        if start > end:
            raise ValueError("from must not be after to")
        local_day_utc_bounds(start)
        local_day_utc_bounds(end)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, "invalid local date window") from exc
    return start, end

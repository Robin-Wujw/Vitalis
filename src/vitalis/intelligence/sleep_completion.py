"""Require a fresh device upload before treating a provisional wake as settled.

Heart rate continues during sleep. Neither a usual-length night nor half an
hour of later heart rate proves that a partial sleep has finished. The same
successful band response must contain the sleep and device data reaching an
hour beyond its wake time. Re-reading an unchanged cloud upload is insufficient.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from statistics import median

MIN_MAIN_SLEEP_MINUTES = 180
USUAL_SLEEP_NIGHTS = 14
USUAL_SLEEP_MIN_NIGHTS = 5
SETTLED_WAKE_MINUTES = 60


def usual_sleep_minutes(durations: Mapping[date, object], day: date) -> float | None:
    """Median asleep minutes of the 14 nights before ``day``; unknown below 5 nights."""
    values = [
        float(minutes)
        for night, minutes in durations.items()
        if day - timedelta(days=USUAL_SLEEP_NIGHTS) <= night < day
        and isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes > 0
    ]
    return median(values) if len(values) >= USUAL_SLEEP_MIN_NIGHTS else None


def minutes_after(wake_at: datetime | None, later: datetime | None) -> int | None:
    """Whole minutes from the wake time to a later instant; unknown without either."""
    if wake_at is None or later is None:
        return None
    return max(0, int((later - wake_at).total_seconds() // 60))


def main_sleep_finished(
    sleep_minutes: float | None,
    recorded_after_wake_minutes: int | None,
    checked_after_wake_minutes: int | None,
) -> bool:
    if sleep_minutes is None or sleep_minutes < MIN_MAIN_SLEEP_MINUTES:
        return False
    if checked_after_wake_minutes is None or checked_after_wake_minutes < SETTLED_WAKE_MINUTES:
        return False
    return (
        recorded_after_wake_minutes is not None
        and recorded_after_wake_minutes >= SETTLED_WAKE_MINUTES
    )

"""Whether last night's stored main sleep has ended.

Zepp ends a main sleep at the last minute it had when the phone uploaded, so an
upload during a brief awakening looks like a wake-up: on 2026-10-17 the record
read 02:26–07:04 (275 minutes) while the night went on past 09:00. A main sleep
counts as finished when it is close to the user's usual night, or when the
watch has uploaded at least half an hour past the wake time without the sleep
being extended (a sleep that went on would have moved the wake time).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from statistics import median

MIN_MAIN_SLEEP_MINUTES = 180
USUAL_SLEEP_NIGHTS = 14
USUAL_SLEEP_MIN_NIGHTS = 5
USUAL_SLEEP_SHARE = 0.75
AWAKE_EVIDENCE_MINUTES = 30


def usual_sleep_minutes(durations: Mapping[date, object], day: date) -> float | None:
    """Median asleep minutes of the 14 nights before ``day``; unknown below 5 nights."""
    values = [
        float(minutes)
        for night, minutes in durations.items()
        if day - timedelta(days=USUAL_SLEEP_NIGHTS) <= night < day
        and isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes > 0
    ]
    return median(values) if len(values) >= USUAL_SLEEP_MIN_NIGHTS else None


def minutes_recorded_after(wake_at: datetime | None, data_until: datetime | None) -> int | None:
    """Whole minutes of uploaded watch data after the wake time; unknown without data."""
    if wake_at is None or data_until is None:
        return None
    return max(0, int((data_until - wake_at).total_seconds() // 60))


def main_sleep_finished(
    sleep_minutes: float | None,
    usual_minutes: float | None,
    recorded_after_wake_minutes: int | None,
) -> bool:
    if sleep_minutes is None or sleep_minutes < MIN_MAIN_SLEEP_MINUTES:
        return False
    if usual_minutes is not None and sleep_minutes >= USUAL_SLEEP_SHARE * usual_minutes:
        return True
    return (
        recorded_after_wake_minutes is not None
        and recorded_after_wake_minutes >= AWAKE_EVIDENCE_MINUTES
    )

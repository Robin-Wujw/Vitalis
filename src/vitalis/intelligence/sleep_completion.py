"""Whether last night's stored main sleep has ended.

Zepp ends a main sleep at the last minute it had when the phone uploaded, and
the watch uploads as soon as it notices waking, even briefly: on 2026-10-17 the
record read 02:26–07:04 (275 minutes) while the night went on past 10:00, and on
2026-10-18 a 08:27 wake read two minutes later started the report at 08:40,
while the user slept on until 09:35. A main sleep counts as finished once the
source, read at least an hour after the wake time, still shows it (user decision
2026-10-18; a sleep that went on would have moved the wake time). While asleep
the phone only uploads at the next brief wake, which can be more than an hour
away, so a night under 90% of the usual one also needs half an hour of uploaded
watch data after the wake time: the two cut-short records read 63% and 83% of
it, while 26 of the 29 nights to 10-18 reached 90%. On ordinary mornings the
phone uploads again 20 to 80 minutes after waking.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from statistics import median

MIN_MAIN_SLEEP_MINUTES = 180
USUAL_SLEEP_NIGHTS = 14
USUAL_SLEEP_MIN_NIGHTS = 5
USUAL_SLEEP_SHARE = 0.9
AWAKE_EVIDENCE_MINUTES = 30
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
    usual_minutes: float | None,
    recorded_after_wake_minutes: int | None,
    checked_after_wake_minutes: int | None,
) -> bool:
    if sleep_minutes is None or sleep_minutes < MIN_MAIN_SLEEP_MINUTES:
        return False
    if checked_after_wake_minutes is None or checked_after_wake_minutes < SETTLED_WAKE_MINUTES:
        return False
    if usual_minutes is not None and sleep_minutes >= USUAL_SLEEP_SHARE * usual_minutes:
        return True
    return (
        recorded_after_wake_minutes is not None
        and recorded_after_wake_minutes >= AWAKE_EVIDENCE_MINUTES
    )

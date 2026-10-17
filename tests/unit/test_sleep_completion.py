"""When a stored main sleep counts as finished."""

from datetime import date, datetime, timedelta, timezone

import pytest

from vitalis.intelligence.sleep_completion import (
    main_sleep_finished,
    minutes_recorded_after,
    usual_sleep_minutes,
)

DAY = date(2026, 10, 17)


def nights(*minutes):
    return {DAY - timedelta(days=offset): value for offset, value in enumerate(minutes, 1)}


def test_usual_night_is_the_median_of_the_previous_fourteen():
    durations = nights(443, 421, 427, 442, 427, 472, 450, 466, 437, 381, 323, 471, 412, 511)
    durations[DAY] = 275
    durations[DAY - timedelta(days=15)] = 100
    assert usual_sleep_minutes(durations, DAY) == 439.5


def test_usual_night_is_unknown_below_five_recorded_nights():
    assert usual_sleep_minutes(nights(440, 440, 440, 440), DAY) is None
    assert usual_sleep_minutes(nights(440, 440, 440, 440, 0, None, True, "x"), DAY) is None
    assert usual_sleep_minutes(nights(440, 440, 440, 440, 400), DAY) == 440


def test_recorded_minutes_after_the_wake_never_go_negative():
    wake = datetime(2026, 10, 16, 23, 4, tzinfo=timezone.utc)
    assert minutes_recorded_after(wake, wake + timedelta(minutes=4, seconds=59)) == 4
    assert minutes_recorded_after(wake, wake - timedelta(minutes=3)) == 0
    assert minutes_recorded_after(wake, None) is None
    assert minutes_recorded_after(None, wake) is None


@pytest.mark.parametrize(("minutes", "usual", "recorded", "finished"), [
    (275, 439.5, 4, False),
    (275, 439.5, 30, True),
    (330, 440, None, True),
    (329, 440, 29, False),
    (455, None, 29, False),
    (455, None, 30, True),
    (179, 200, 120, False),
    (None, 440, 120, False),
])
def test_a_sleep_has_ended_near_the_usual_night_or_after_half_an_hour_awake(
    minutes, usual, recorded, finished,
):
    assert main_sleep_finished(minutes, usual, recorded) is finished

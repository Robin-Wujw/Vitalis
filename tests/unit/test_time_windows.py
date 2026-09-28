from datetime import date, timezone

from vitalis.time import local_day_utc_bounds, local_sleep_window


def test_dst_spring_day_is_23_hours_and_half_open():
    start, end = local_day_utc_bounds(date(2026, 3, 8), "America/New_York")
    assert (end - start).total_seconds() == 23 * 60 * 60
    assert start < end


def test_dst_fall_day_is_25_hours_and_midnight_boundaries():
    start, end = local_day_utc_bounds(date(2026, 11, 1), "America/New_York")
    assert (end - start).total_seconds() == 25 * 60 * 60
    previous_end = local_day_utc_bounds(date(2026, 10, 31), "America/New_York")[1]
    assert previous_end == start


def test_sleep_window_accepts_explicit_timezone_and_crosses_midnight():
    start, end = local_sleep_window(
        date(2026, 3, 8), "23:30", "07:30", "America/New_York"
    )
    assert end > start
    # Local wall time is eight hours, but the UTC elapsed duration is seven
    # hours because the spring-forward transition removes one hour.
    assert (end - start).total_seconds() == 8 * 60 * 60
    assert (
        end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    ).total_seconds() == 7 * 60 * 60

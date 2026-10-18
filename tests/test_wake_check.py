"""One-week review of wake-triggered morning reports."""

from datetime import date, datetime, time

from vitalis.application.wake_check import WakeDay, summarize


def _day(day, wake, synced, created, probes=2):
    return WakeDay(
        day=day, wake_time=wake, probes=probes,
        morning_sync_at=datetime.fromisoformat(f"{day}T{synced}:00") if synced else None,
        report_created_at=datetime.fromisoformat(f"{day}T{created}:00") if created else None,
        report_updated_at=None, report_status="delivered",
    )


def _summary(days):
    return summarize(days, timezone_name="UTC", poll_interval_seconds=900, window_end=time(13, 0))


def test_review_passes_when_reports_start_an_hour_after_waking():
    result = _summary([
        _day(date(2026, 10, 14), "08:26", "09:27", "09:33"),
        _day(date(2026, 10, 15), "08:36", "09:37", "09:42"),
    ])
    assert (result["partial_sleep_reports"], result["early_reports"]) == (0, 0)
    assert result["limit_minutes"] == 85
    assert result["median_minutes_after_wake"] == 66.5
    assert result["passes"] is True


def test_report_started_before_the_stored_wake_time_counts_as_partial_sleep():
    result = _summary([_day(date(2026, 10, 11), "07:30", "02:10", "02:20")])
    assert result["partial_sleep_reports"] == 1 and result["passes"] is False
    assert result["days"][0]["started_before_wake"] is True


def test_report_started_within_an_hour_of_the_final_wake_is_early():
    # 2026-10-18: a read two minutes after the 08:27 wake started the report.
    result = _summary([_day(date(2026, 10, 18), "08:27", "08:35", "08:40")])
    assert (result["partial_sleep_reports"], result["early_reports"]) == (0, 1)
    assert result["days"][0]["started_within_hour"] is True
    assert result["passes"] is False


def test_the_window_end_fallback_is_not_an_early_start():
    result = _summary([_day(date(2026, 10, 19), "12:30", "13:00", "13:05")])
    assert result["early_reports"] == 0
    assert result["days"][0]["waited_for_window_end"] is True


def test_median_delay_is_checked_against_the_hour_plus_one_poll_and_ten_minutes():
    result = _summary([
        _day(date(2026, 10, 12), "07:00", "08:05", "08:20"),
        _day(date(2026, 10, 13), "07:00", "08:40", "08:50"),
    ])
    assert result["median_minutes_after_wake"] == 95 and result["passes"] is False
